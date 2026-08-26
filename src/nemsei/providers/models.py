from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from nemsei.db.base import Base


PROVIDER_CODES = ("fusionsolar", "sigenergy", "sma")
RESOURCE_KINDS = ("plant", "device")
CONNECTION_STATUSES = ("not_configured", "configured", "disabled")
MAPPING_STATUSES = ("active", "superseded", "invalid", "pending_review")
IMPORT_OUTCOMES = ("created", "reused", "quarantined", "changed_source", "conflict", "excluded", "unresolved")
IDENTITY_DECISIONS = ("canonical", "discard")
OPERATOR_AUDIT_ACTIONS = (
    "identity_decision_recorded",
    "mapping_approved",
    "mapping_rejected",
    "source_policy_created",
    "source_policy_changed",
    "connection_configured",
    "connection_enabled",
    "connection_disabled",
    "validation_requested",
    # Bloco A: identity edits made from the browser. Until now the asset form
    # had no UI at all, so nothing an operator did to a plant left a trace.
    "asset_updated",
    "asset_reviewed",
    # Bloco E: the notification channel and its policies are the only
    # automations the interface can switch; the schedulers are environment.
    "automation_enabled",
    "automation_disabled",
    # O&M contracts: creating a period, closing one, and recording renewal
    # follow-up are the three writes the contract panel and the renewals
    # screen can make.
    "service_contract_created",
    "service_contract_closed",
    "service_contract_renewal_updated",
    # Which slice of the fleet a notification policy speaks for.
    "automation_scope_changed",
)


class ProviderConnection(Base):
    __tablename__ = "provider_connections"
    __table_args__ = (
        CheckConstraint(f"provider_code IN {PROVIDER_CODES!r}", name="ck_provider_connections_provider_code"),
        CheckConstraint(f"configuration_status IN {CONNECTION_STATUSES!r}", name="ck_provider_connections_configuration_status"),
        UniqueConstraint("provider_code", "connection_key", name="uq_provider_connections_provider_key"),
        Index("ix_provider_connections_provider", "provider_code", "enabled"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_code: Mapped[str] = mapped_column(String(32), nullable=False)
    connection_key: Mapped[str] = mapped_column(String(120), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    account_reference: Mapped[str | None] = mapped_column(String(255))
    region: Mapped[str | None] = mapped_column(String(64))
    credential_reference: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    configuration_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_configured")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AssetProviderMapping(Base):
    __tablename__ = "asset_provider_mappings"
    __table_args__ = (
        CheckConstraint(f"resource_kind IN {RESOURCE_KINDS!r}", name="ck_asset_provider_mappings_resource_kind"),
        CheckConstraint(f"mapping_status IN {MAPPING_STATUSES!r}", name="ck_asset_provider_mappings_status"),
        # A device claim carries its device; a plant claim never does.
        CheckConstraint(
            "(resource_kind = 'device') = (device_id IS NOT NULL)",
            name="ck_asset_provider_mappings_device_link",
        ),
        UniqueConstraint(
            "asset_id",
            "provider_connection_id",
            "resource_kind",
            "normalized_external_id",
            "valid_from",
            name="uq_asset_provider_mappings_history",
        ),
        Index(
            "uq_asset_provider_mappings_active_external",
            "provider_connection_id",
            "resource_kind",
            "normalized_external_id",
            unique=True,
            postgresql_where=text("mapping_status = 'active' AND valid_to IS NULL"),
        ),
        Index("ix_asset_provider_mappings_asset", "asset_id", "mapping_status"),
        Index("ix_asset_provider_mappings_device", "device_id", "mapping_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="RESTRICT"))
    provider_connection_id: Mapped[int] = mapped_column(ForeignKey("provider_connections.id", ondelete="RESTRICT"), nullable=False)
    resource_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="plant")
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    external_name: Mapped[str | None] = mapped_column(String(255))
    mapping_status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)
    # Read only for migration compatibility. New source selection belongs in
    # temporal AssetSourcePolicy rows, not on the provider mapping itself.
    monitoring_priority: Mapped[int | None] = mapped_column(Integer)
    production_priority: Mapped[int | None] = mapped_column(Integer)
    replaced_by_mapping_id: Mapped[int | None] = mapped_column(ForeignKey("asset_provider_mappings.id", ondelete="SET NULL"))
    # The plant claim a device claim was discovered under, so device provenance
    # survives a plant remapping and reconciliation can tell an orphaned claim
    # from a legitimately re-parented one.
    parent_mapping_id: Mapped[int | None] = mapped_column(ForeignKey("asset_provider_mappings.id", ondelete="SET NULL"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LegacyImportRun(Base):
    __tablename__ = "legacy_import_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_database_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_locator_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    importer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    manifest_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class LegacyImportRecord(Base):
    __tablename__ = "legacy_import_records"
    __table_args__ = (
        CheckConstraint(f"outcome IN {IMPORT_OUTCOMES!r}", name="ck_legacy_import_records_outcome"),
        # `outcome` is part of the key so an operator decision can move a source
        # row from quarantined to created without erasing the earlier evidence,
        # while a rerun that reaches the same outcome still cannot duplicate it.
        UniqueConstraint("source_database_sha256", "legacy_table", "legacy_id", "source_hash", "outcome", name="uq_legacy_import_records_source_version"),
        Index("ix_legacy_import_records_outcome", "outcome"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_run_id: Mapped[int] = mapped_column(ForeignKey("legacy_import_runs.id", ondelete="RESTRICT"), nullable=False)
    source_database_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_locator_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    legacy_table: Mapped[str] = mapped_column(String(120), nullable=False)
    legacy_id: Mapped[str] = mapped_column(String(120), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    target_organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id", ondelete="SET NULL"))
    target_asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"))
    target_device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"))
    target_mapping_id: Mapped[int | None] = mapped_column(ForeignKey("asset_provider_mappings.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LegacyIdentityDecision(Base):
    """One operator ruling on ambiguous V1 identity evidence.

    The importer never merges installations by name similarity. When a single
    normalized V1 name covers several rows, a human designates the canonical
    row and discards the rest; recording that here keeps the import repeatable
    instead of turning it into a manual database edit.
    """

    __tablename__ = "legacy_identity_decisions"
    __table_args__ = (
        CheckConstraint(f"decision IN {IDENTITY_DECISIONS!r}", name="ck_legacy_identity_decisions_decision"),
        UniqueConstraint("legacy_table", "legacy_id", name="uq_legacy_identity_decisions_row"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    legacy_table: Mapped[str] = mapped_column(String(120), nullable=False)
    legacy_id: Mapped[str] = mapped_column(String(120), nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    actor_username: Mapped[str] = mapped_column(String(120), nullable=False)
    source_database_sha256: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OperatorAuditEvent(Base):
    """Append-only, sanitized record of consequential operator actions."""

    __tablename__ = "operator_audit_events"
    __table_args__ = (
        CheckConstraint(f"action IN {OPERATOR_AUDIT_ACTIONS!r}", name="ck_operator_audit_events_action"),
        Index("ix_operator_audit_events_entity", "entity_type", "entity_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_username: Mapped[str] = mapped_column(String(120), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
