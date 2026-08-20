"""Device status facts: point-in-time diagnostic evidence, per device.

Separate from `reporting/`, deliberately. Diagnosis is equipment state — is
this inverter communicating, is it producing, is it available — and reporting
is money and contracted energy. `production_facts` already keeps these apart
by `metric_kind`; this module keeps them apart by living in its own package,
so a future field never has to choose which layer it "also" belongs to.

Anchored on `device_id`, not `provider_mapping_id`. Every device-scoped
provider mapping V1 left behind is `pending_review` on a disabled,
credential-free legacy connection — none is active — so a table requiring a
usable mapping could not accept a single row today. `device_id` is what the M1
identity import already resolved for every device, independent of whether any
provider connection is currently usable.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nemsei.db.base import Base


# V1's own vocabulary (`services/fusionsolar.py:classify_fusionsolar_inverter_availability`),
# kept verbatim rather than approximated onto `monitoring.OBSERVATION_CONDITIONS`,
# which was designed for provider-connection observations, not physical
# inverter states, and has no honest mapping for "standby".
AVAILABILITY_STATES = ("available", "standby", "unavailable", "unknown")
SOURCE_KINDS = ("v1_import", "live_read")


class DeviceStatusFact(Base):
    """One point-in-time reading of one device's status, power and energy."""

    __tablename__ = "device_status_facts"
    __table_args__ = (
        CheckConstraint(f"availability_status IN {AVAILABILITY_STATES!r}", name="ck_device_status_facts_availability"),
        CheckConstraint(f"source_kind IN {SOURCE_KINDS!r}", name="ck_device_status_facts_source_kind"),
        CheckConstraint("active_power_kw IS NULL OR active_power_kw >= 0", name="ck_device_status_facts_power"),
        CheckConstraint("day_energy_kwh IS NULL OR day_energy_kwh >= 0", name="ck_device_status_facts_energy"),
        UniqueConstraint("device_id", "source_fact_key", "source_revision", name="uq_device_status_facts_revision"),
        Index("ix_device_status_facts_device_time", "device_id", "observed_at"),
        Index("ix_device_status_facts_asset_time", "asset_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="RESTRICT"), nullable=False)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False)
    source_fact_key: Mapped[str] = mapped_column(String(255), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    supersedes_fact_id: Mapped[int | None] = mapped_column(ForeignKey("device_status_facts.id", ondelete="RESTRICT"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    availability_status: Mapped[str] = mapped_column(String(24), nullable=False)
    active_power_kw: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    day_energy_kwh: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="v1_import")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
