"""What Solcor is going to do about an installation, and when it went there.

Two different questions, kept as two different entities:

    Incident    -- what is wrong (`diagnostics.models.DiagnosticIncident`)
    WorkOrder   -- what we are going to do about it
    Visit       -- when and how we physically went

`WorkOrder` and `DiagnosticIncident` are many-to-many
(`work_order_incidents`): one work order can close several incidents in one
visit -- three inverters offline on the same string fault is one job -- and
one incident can spawn more than one work order over its life, if the first
attempt did not fix it. Neither table owns the other.

Scoped to `Installation`, not `Asset`. A work order is dispatched to a site
-- "go to DIACO and fix the string fault" -- and may end up touching more
than one technical plant there once an installation legitimately holds
several. `DiagnosticIncident` stays scoped to `Asset`/`Device`, deliberately:
the technical origin of a fault matters for diagnosis, and this module does
not touch that table's foreign keys.

V1 held this concept as `tickets`/`ticket_visits`, and real production
history said not to port its shape. 13 tickets, 7 visits: `work_type` set on
one row of thirteen, `assigned_to` and `material_status` populated on none,
`field_route_plans` (route costing, kilometre pricing, margin calculation)
holding zero rows ever. The workflow V1's schema was built for -- nine
material-blocking states, cost modelling, route planning -- was never used;
what was used was recording that something happened and when, in V1's own
`TICKET_STATUSES`/`TICKET_WORK_TYPES` vocabulary trimmed to what real rows
actually needed. Material tracking and technician assignment stay free text
here, without a workflow of their own, until real use argues for one.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nemsei.db.base import Base


def public_id() -> str:
    return str(uuid.uuid4())


# Corrective, preventive and cleaning share one module and one table, per
# GOAL.md: "não cries três subsistemas". A cleaning visit and a corrective
# repair are the same shape of record -- who went, when, what happened --
# and splitting them would only duplicate that shape three times.
WORK_TYPES = ("corrective", "preventive", "cleaning")

# V1's real usage, not its schema: of the six declared `TICKET_STATUSES`,
# thirteen real rows used four (`Aberto`, `Em analise`, `Resolvido`,
# `Fechado`); `Agendado` and `Em visita` were declared but never reached.
# This vocabulary is V2's own, sized to that evidence plus the two states a
# planning screen needs (`planned` for "has a date", `cancelled` for "will
# not happen") rather than V1's nine-state material-blocking pipeline, which
# `field_route_plans`/`material_status` show was designed for a workflow
# that never actually ran.
WORK_ORDER_STATUSES = ("open", "planned", "in_progress", "completed", "cancelled")
# Free text until material tracking earns a workflow of its own -- see the
# module docstring. Kept as a distinct, small vocabulary so a value is at
# least consistent across work orders, without pretending to model blocking.
MATERIAL_STATUSES = ("not_applicable", "pending", "ordered", "ready")


class WorkOrder(Base):
    """One planned or completed piece of field work at one installation."""

    __tablename__ = "work_orders"
    __table_args__ = (
        CheckConstraint(f"work_type IN {WORK_TYPES!r}", name="ck_work_orders_type"),
        CheckConstraint(f"status IN {WORK_ORDER_STATUSES!r}", name="ck_work_orders_status"),
        CheckConstraint(f"material_status IN {MATERIAL_STATUSES!r}", name="ck_work_orders_material_status"),
        CheckConstraint(
            "due_date IS NULL OR planned_date IS NULL OR due_date >= planned_date",
            name="ck_work_orders_dates",
        ),
        CheckConstraint("(status = 'completed') = (completed_at IS NOT NULL)", name="ck_work_orders_completed_at"),
        CheckConstraint("estimated_cost_eur IS NULL OR estimated_cost_eur >= 0", name="ck_work_orders_cost"),
        Index("ix_work_orders_installation", "installation_id", "status"),
        Index("ix_work_orders_planned_date", "planned_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, default=public_id)
    installation_id: Mapped[int] = mapped_column(ForeignKey("installations.id", ondelete="RESTRICT"), nullable=False)
    work_type: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open")
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    planned_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Free text, deliberately -- see the module docstring.
    assigned_to: Mapped[str | None] = mapped_column(String(120))
    material_status: Mapped[str] = mapped_column(String(24), nullable=False, default="not_applicable")
    material_notes: Mapped[str | None] = mapped_column(Text)
    estimated_cost_eur: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # cascade="all, delete-orphan" matches the database's own ON DELETE
    # CASCADE on `visits.work_order_id`. Without it the ORM's default
    # behaviour is to NULL the foreign key before deleting the parent, which
    # `work_order_id`'s NOT NULL then refuses -- the database constraint and
    # the ORM disagreeing about what "delete a work order" means.
    visits: Mapped[list["Visit"]] = relationship(
        back_populates="work_order", order_by="Visit.visit_date", cascade="all, delete-orphan"
    )


class Visit(Base):
    """One physical visit against one work order."""

    __tablename__ = "visits"
    __table_args__ = (Index("ix_visits_work_order", "work_order_id", "visit_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, default=public_id)
    work_order_id: Mapped[int] = mapped_column(ForeignKey("work_orders.id", ondelete="CASCADE"), nullable=False)
    visit_date: Mapped[date] = mapped_column(Date, nullable=False)
    technician: Mapped[str | None] = mapped_column(String(120))
    outcome: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    work_order: Mapped[WorkOrder] = relationship(back_populates="visits")


class WorkOrderIncident(Base):
    """One link in the many-to-many between work orders and the incidents
    they address. A surrogate id, not a composite key, matching the join
    tables elsewhere in this schema (`portfolio_dataset_members` and
    others)."""

    __tablename__ = "work_order_incidents"
    __table_args__ = (
        UniqueConstraint("work_order_id", "incident_id", name="uq_work_order_incidents_pair"),
        Index("ix_work_order_incidents_incident", "incident_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_order_id: Mapped[int] = mapped_column(ForeignKey("work_orders.id", ondelete="CASCADE"), nullable=False)
    incident_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_incidents.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
