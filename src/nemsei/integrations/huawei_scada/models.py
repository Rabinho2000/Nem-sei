"""Tables for the inbound Huawei SDongle session: sessions, samples, quarantine.

These live in the integration package rather than in a canonical domain, and
that is deliberate. A row here is a *Modbus register block read off a dongle*:
it carries a dongle serial, five register addresses and a session identity.
There is no provider-neutral "instantaneous power sample" concept in V2 to put
it in, and inventing one to hold a shape only this provider produces would be
the schema debt `DECISIONS.md` keeps warning about. What *is* canonical --
current condition and daily energy -- is written to `monitoring_observations`
and `production_facts` from here, in their own vocabulary.

Three tables, each answering a different question:

* `huawei_scada_power_samples` -- what did the plant measure, append-only with
  the same revision/supersession rule as `production_facts`. A reconnect that
  re-reads the same instant supersedes, it never duplicates.
* `huawei_scada_sessions` -- who connected, when, for how long, and how it
  ended. This is what makes a reconnect visible as a reconnect rather than as
  a gap in the samples.
* `huawei_scada_pending_dongles` -- a serial nobody has claimed yet. It exists
  so that an unknown dongle has somewhere to go that is *not* an asset. There
  is deliberately no code path that maps a dongle by the address it dialled in
  from: a NAT rule, a DHCP lease or a customer changing ISP would silently
  rebind someone else's plant.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from nemsei.db.base import Base
from nemsei.monitoring.models import QUALITY_STATES

# One vocabulary shared by the session row and by every sample it produced, so
# "what state was the session in when this number was measured" is answerable
# from the sample alone, without a join back to a session that may since have
# closed.
SESSION_STATES = ("connected", "polling", "degraded", "quarantined", "closed")
# How a session ended. `idle_timeout` and `peer_closed` are ordinary; the rest
# are worth alerting on.
CLOSE_REASONS = (
    "peer_closed",
    "idle_timeout",
    "protocol_error",
    "unmapped_dongle",
    "listener_shutdown",
    "read_error",
    "session_limit",
)
PENDING_DONGLE_STATUSES = ("pending", "rejected", "mapped")


class HuaweiScadaSession(Base):
    """One inbound TCP conversation with one dongle."""

    __tablename__ = "huawei_scada_sessions"
    __table_args__ = (
        CheckConstraint(f"session_state IN {SESSION_STATES!r}", name="ck_huawei_scada_sessions_state"),
        CheckConstraint(
            f"close_reason IS NULL OR close_reason IN {CLOSE_REASONS!r}",
            name="ck_huawei_scada_sessions_close_reason",
        ),
        Index("ix_huawei_scada_sessions_dongle_time", "dongle_serial", "opened_at"),
        Index("ix_huawei_scada_sessions_open", "session_state", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Null until the banner arrives: a socket can be accepted, and can even
    # time out, before the dongle has said who it is.
    dongle_serial: Mapped[str | None] = mapped_column(String(120))
    provider_connection_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_connections.id", ondelete="SET NULL")
    )
    provider_mapping_id: Mapped[int | None] = mapped_column(
        ForeignKey("asset_provider_mappings.id", ondelete="SET NULL")
    )
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"))
    session_state: Mapped[str] = mapped_column(String(32), nullable=False, default="connected")
    close_reason: Mapped[str | None] = mapped_column(String(32))
    # A salted digest of the peer address, never the address itself. It is
    # enough to tell "the same source reconnected" from "a different source
    # appeared", and it is structurally useless for deciding which asset a
    # dongle belongs to -- which is the point.
    peer_fingerprint: Mapped[str | None] = mapped_column(String(64))
    dongle_model: Mapped[str | None] = mapped_column(String(120))
    dongle_software_version: Mapped[str | None] = mapped_column(String(120))
    protocol_version: Mapped[str | None] = mapped_column(String(32))
    aggregate_unit_id: Mapped[int | None] = mapped_column(Integer)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    poll_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    safe_detail: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class HuaweiScadaPowerSample(Base):
    """One aggregate register block, in kW. Append-only, revision-superseded.

    Power, never energy. The five columns are instantaneous measurements at
    `observed_at`; turning them into kWh is `rollup.py`'s job and is an
    explicit, documented integration, not an implicit unit change.
    """

    __tablename__ = "huawei_scada_power_samples"
    __table_args__ = (
        CheckConstraint(f"quality IN {QUALITY_STATES!r}", name="ck_huawei_scada_samples_quality"),
        CheckConstraint(f"completeness IN {QUALITY_STATES!r}", name="ck_huawei_scada_samples_completeness"),
        CheckConstraint(f"session_state IN {SESSION_STATES!r}", name="ck_huawei_scada_samples_session_state"),
        # Same shape as `uq_production_fact_revision`: a re-read of the same
        # instant is a new revision of one sample, not a second sample.
        UniqueConstraint(
            "provider_mapping_id", "source_sample_key", "source_revision", name="uq_huawei_scada_sample_revision"
        ),
        Index("ix_huawei_scada_samples_asset_time", "asset_id", "observed_at"),
        Index("ix_huawei_scada_samples_dongle_time", "dongle_serial", "observed_at"),
        Index("ix_huawei_scada_samples_session", "session_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False)
    provider_mapping_id: Mapped[int] = mapped_column(
        ForeignKey("asset_provider_mappings.id", ondelete="RESTRICT"), nullable=False
    )
    session_id: Mapped[int | None] = mapped_column(ForeignKey("huawei_scada_sessions.id", ondelete="SET NULL"))
    dongle_serial: Mapped[str] = mapped_column(String(120), nullable=False)
    source_sample_key: Mapped[str] = mapped_column(String(255), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    supersedes_sample_id: Mapped[int | None] = mapped_column(
        ForeignKey("huawei_scada_power_samples.id", ondelete="RESTRICT")
    )
    # The protocol carries no timestamp of its own -- confirmed on both pilots
    # -- so this is when the listener read the block. `metadata_json` says so
    # explicitly on every row rather than leaving a reader to assume otherwise.
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    pv_input_power_kw: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    load_power_kw: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    grid_power_kw: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    battery_power_kw: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    total_active_power_kw: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    raw_registers_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    quality: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    completeness: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    session_state: Mapped[str] = mapped_column(String(32), nullable=False, default="polling")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class HuaweiScadaPendingDongle(Base):
    """A serial that announced itself and has no mapping yet.

    Quarantine, not a waiting room with an automatic exit: nothing here becomes
    an `AssetProviderMapping` without an operator creating one. The row exists
    so the operator can see that a dongle is knocking, with the evidence it
    presented, instead of having to read a log.
    """

    __tablename__ = "huawei_scada_pending_dongles"
    __table_args__ = (
        CheckConstraint(f"status IN {PENDING_DONGLE_STATUSES!r}", name="ck_huawei_scada_pending_status"),
        UniqueConstraint("dongle_serial", name="uq_huawei_scada_pending_serial"),
        Index("ix_huawei_scada_pending_status", "status", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dongle_serial: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_connection_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_connections.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    session_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    peer_fingerprint: Mapped[str | None] = mapped_column(String(64))
    advertisement_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    notes: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
