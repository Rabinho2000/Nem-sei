"""Read-only, evidence-preserving import of the limited V1 asset inventory.

This module deliberately speaks SQL to a separately opened SQLite database.  It
does not import any V1 Python code and never writes to the supplied source.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset, AssetAlias, Organization
from nemsei.assets.service import create_asset, create_organization, normalize_name, normalize_tax_id
from nemsei.config import Settings
from nemsei.db.engine import build_engine
from nemsei.db.session import build_session_factory
from nemsei.providers.models import AssetProviderMapping, LegacyImportRecord, LegacyImportRun, ProviderConnection
from nemsei.providers.registry import normalize_external_id
from nemsei.providers.service import create_connection, create_mapping
from nemsei.shared.clock import utc_now


REQUIRED_TABLES = {"customers", "assets", "asset_aliases", "asset_integrations"}
LEGACY_CONNECTIONS = {
    "fusionsolar": ("v1-fusionsolar-legacy", "V1 FusionSolar legacy mappings"),
    "sigenergy": ("v1-sigenergy-legacy", "V1 Sigenergy legacy mappings"),
}
IMPORTER_VERSION = "assets-v1-importer/2.0"
IMPORT_BATCH_SIZE = 100


class LegacyImportError(ValueError):
    """Raised when an import cannot safely account for its source."""


@dataclass
class ImportManifest:
    source_database_sha256: str
    dry_run: bool
    source_locator_sha256: str = ""
    counts: Counter[str] = field(default_factory=Counter)
    issues: list[dict[str, str]] = field(default_factory=list)

    def record(self, table: str, outcome: str, *, legacy_id: int | str | None = None, reason: str | None = None) -> None:
        self.counts[f"{table}.{outcome}"] += 1
        if reason:
            self.issues.append(
                {"table": table, "legacy_id": str(legacy_id) if legacy_id is not None else "", "reason": reason}
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_database_sha256": self.source_database_sha256,
            "dry_run": self.dry_run,
            "counts": dict(sorted(self.counts.items())),
            "issues": self.issues,
        }


def source_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as database:
        for chunk in iter(lambda: database.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_v1_readonly(path: Path) -> sqlite3.Connection:
    """Open a V1 SQLite file read-only, with an additional query-only guard."""
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise LegacyImportError("V1 SQLite source must be an existing regular file.")
    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    missing = REQUIRED_TABLES - tables
    if missing:
        connection.close()
        raise LegacyImportError(f"V1 SQLite source is missing required tables: {', '.join(sorted(missing))}.")
    return connection


def row_hash(row: sqlite3.Row, columns: Iterable[str]) -> str:
    values = {column: row[column] for column in columns}
    encoded = json.dumps(values, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def optional_decimal(value: Any) -> Decimal | None:
    if value is None or not str(value).strip():
        return None
    try:
        parsed = Decimal(str(value).strip().replace(",", "."))
    except InvalidOperation:
        return None
    return parsed if parsed >= 0 else None


def optional_date(value: Any) -> date | None:
    if not value or not str(value).strip():
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def prior_record(session: Session, source: str, table: str, legacy_id: int | str) -> LegacyImportRecord | None:
    exact = session.scalar(
        select(LegacyImportRecord)
        .where(
            LegacyImportRecord.source_database_sha256 == source,
            LegacyImportRecord.legacy_table == table,
            LegacyImportRecord.legacy_id == str(legacy_id),
        )
        .order_by(LegacyImportRecord.id.desc())
    )
    if exact is not None:
        return exact
    return session.scalar(
        select(LegacyImportRecord)
        .where(
            LegacyImportRecord.source_locator_sha256 == session.info.get("legacy_import_locator", ""),
            LegacyImportRecord.legacy_table == table,
            LegacyImportRecord.legacy_id == str(legacy_id),
        )
        .order_by(LegacyImportRecord.id.desc())
    )


def add_record(
    session: Session | None,
    run: LegacyImportRun | None,
    manifest: ImportManifest,
    *,
    table: str,
    legacy_id: int | str,
    source_hash: str,
    source_locator_sha256: str | None = None,
    outcome: str,
    reason: str | None = None,
    organization: Organization | None = None,
    asset: Asset | None = None,
    mapping: AssetProviderMapping | None = None,
    persist: bool = True,
) -> None:
    manifest.record(table, outcome, legacy_id=legacy_id, reason=reason)
    if session is None or run is None or not persist:
        return
    session.add(
        LegacyImportRecord(
            import_run_id=run.id,
            source_database_sha256=manifest.source_database_sha256,
            source_locator_sha256=source_locator_sha256 or manifest.source_locator_sha256,
            legacy_table=table,
            legacy_id=str(legacy_id),
            source_hash=source_hash,
            outcome=outcome,
            reason=reason,
            target_organization_id=organization.id if organization else None,
            target_asset_id=asset.id if asset else None,
            target_mapping_id=mapping.id if mapping else None,
            created_at=utc_now(),
        )
    )
    batch_size = session.info.get("legacy_import_batch_size")
    if batch_size:
        writes = session.info.get("legacy_import_writes", 0) + 1
        session.info["legacy_import_writes"] = writes
        if writes >= batch_size:
            session.commit()
            session.info["legacy_import_writes"] = 0


def legacy_connection(session: Session, provider_code: str) -> ProviderConnection:
    key, label = LEGACY_CONNECTIONS[provider_code]
    existing = session.scalar(
        select(ProviderConnection).where(
            ProviderConnection.provider_code == provider_code,
            ProviderConnection.connection_key == key,
        )
    )
    if existing is not None:
        return existing
    return create_connection(
        session,
        provider_code=provider_code,
        connection_key=key,
        display_name=label,
        enabled=False,
        configuration_status="disabled",
    )


def import_v1_assets(session: Session | None, source_path: Path, *, dry_run: bool = False, batch_size: int | None = None) -> dict[str, Any]:
    """Import only V1 identity/mapping evidence and return a JSON-safe manifest.

    A dry run deliberately does not need, use, or modify a V2 SQLAlchemy session.
    """
    if not dry_run and session is None:
        raise LegacyImportError("A V2 session is required for a real import.")
    source_path = source_path.expanduser().resolve()
    if session is not None:
        bound_database = Path(str(session.get_bind().url.database)).resolve()
        if source_path == bound_database:
            raise LegacyImportError("The V1 source database cannot be the V2 database.")
    source = source_sha256(source_path)
    locator = hashlib.sha256(str(source_path).encode()).hexdigest()
    if session is not None:
        session.info["legacy_import_locator"] = locator
        if batch_size is not None:
            if batch_size <= 0:
                raise LegacyImportError("Import batch size must be positive.")
            session.info["legacy_import_batch_size"] = batch_size
            session.info["legacy_import_writes"] = 0
    manifest = ImportManifest(source_database_sha256=source, dry_run=dry_run, source_locator_sha256=locator)
    source_db = open_v1_readonly(source_path)
    run: LegacyImportRun | None = None
    try:
        if not dry_run:
            run = LegacyImportRun(
                source_database_sha256=source,
                source_locator_sha256=locator,
                importer_version=IMPORTER_VERSION,
                dry_run=False,
                started_at=utc_now(),
                manifest_json={},
            )
            session.add(run)
            session.flush()

        customers = list(source_db.execute("SELECT id, name, nif, normalized_nif, active, review_required, review_notes FROM customers ORDER BY id"))
        organizations: dict[int, Organization] = {}
        for row in customers:
            fingerprint = row_hash(row, row.keys())
            existing = prior_record(session, source, "customers", row["id"]) if session else None
            if existing and existing.source_hash == fingerprint:
                organization = session.get(Organization, existing.target_organization_id) if existing.target_organization_id else None
                if organization:
                    organizations[row["id"]] = organization
                add_record(session, run, manifest, table="customers", legacy_id=row["id"], source_hash=fingerprint, outcome="reused", organization=organization, persist=False)
                continue
            if existing:
                add_record(session, run, manifest, table="customers", legacy_id=row["id"], source_hash=fingerprint, outcome="changed_source", reason="V1 customer changed; existing V2 organization was preserved.", organization=session.get(Organization, existing.target_organization_id) if existing.target_organization_id else None)
                continue
            if dry_run:
                add_record(None, None, manifest, table="customers", legacy_id=row["id"], source_hash=fingerprint, outcome="created")
                continue
            tax_id = normalize_tax_id(row["normalized_nif"] or row["nif"])
            organization = session.scalar(select(Organization).where(Organization.normalized_tax_id == tax_id)) if tax_id else None
            if organization is None:
                organization = create_organization(
                    session,
                    display_name=row["name"],
                    tax_id=tax_id,
                    review_status="needs_review" if row["review_required"] else "clear",
                    review_note=row["review_notes"] or None,
                )
                organization.active = bool(row["active"])
            organizations[row["id"]] = organization
            add_record(session, run, manifest, table="customers", legacy_id=row["id"], source_hash=fingerprint, outcome="created", organization=organization)

        assets = list(source_db.execute("SELECT id, project_name, address, location, kwp, commissioning_date, country, timezone, notes, customer_id FROM assets ORDER BY id"))
        duplicate_names = {name for name, count in Counter(normalize_name(row["project_name"]) for row in assets).items() if count > 1}
        imported_assets: dict[int, Asset] = {}
        eligible_asset_ids: set[int] = set()
        for row in assets:
            fingerprint = row_hash(row, row.keys())
            normalized = normalize_name(row["project_name"])
            existing = prior_record(session, source, "assets", row["id"]) if session else None
            if existing and existing.source_hash == fingerprint:
                asset = session.get(Asset, existing.target_asset_id) if existing.target_asset_id else None
                if asset:
                    imported_assets[row["id"]] = asset
                add_record(session, run, manifest, table="assets", legacy_id=row["id"], source_hash=fingerprint, outcome="reused", asset=asset, persist=False)
                continue
            if existing:
                add_record(session, run, manifest, table="assets", legacy_id=row["id"], source_hash=fingerprint, outcome="changed_source", reason="V1 asset changed; existing V2 asset was preserved.", asset=session.get(Asset, existing.target_asset_id) if existing.target_asset_id else None)
                continue
            if normalized in duplicate_names:
                add_record(session, run, manifest, table="assets", legacy_id=row["id"], source_hash=fingerprint, outcome="quarantined", reason="Duplicate normalized V1 asset name requires identity review.")
                continue
            eligible_asset_ids.add(row["id"])
            if dry_run:
                add_record(None, None, manifest, table="assets", legacy_id=row["id"], source_hash=fingerprint, outcome="created")
                continue
            power = optional_decimal(row["kwp"])
            review_reasons = []
            if power is None:
                review_reasons.append("installed power missing or invalid")
            if not row["location"]:
                review_reasons.append("location missing")
            if not row["timezone"]:
                review_reasons.append("timezone missing")
            timezone = row["timezone"] or None
            timezone_source = "legacy_source" if row["timezone"] else "unknown"
            asset = create_asset(
                session,
                canonical_name=row["project_name"],
                owner_id=organizations.get(row["customer_id"]).id if row["customer_id"] in organizations else None,
                lifecycle_status="unknown",
                country_code=row["country"],
                timezone=timezone,
                timezone_source=timezone_source,
                installed_dc_power_kw=power,
                commissioned_on=optional_date(row["commissioning_date"]),
                address=row["address"],
                locality=row["location"],
                technical_notes=row["notes"],
                review_status="needs_review" if review_reasons else "clear",
                review_note="; ".join(review_reasons) or None,
            )
            imported_assets[row["id"]] = asset
            add_record(session, run, manifest, table="assets", legacy_id=row["id"], source_hash=fingerprint, outcome="created", asset=asset)

        aliases = list(source_db.execute("SELECT id, asset_id, alias_name, normalized_alias, source, active FROM asset_aliases ORDER BY id"))
        for row in aliases:
            fingerprint = row_hash(row, row.keys())
            existing = prior_record(session, source, "asset_aliases", row["id"]) if session else None
            asset = imported_assets.get(row["asset_id"])
            if existing and existing.source_hash == fingerprint:
                add_record(session, run, manifest, table="asset_aliases", legacy_id=row["id"], source_hash=fingerprint, outcome="reused", asset=session.get(Asset, existing.target_asset_id) if session and existing.target_asset_id else None, persist=False)
                continue
            if existing:
                add_record(session, run, manifest, table="asset_aliases", legacy_id=row["id"], source_hash=fingerprint, outcome="changed_source", reason="V1 alias changed; existing V2 alias was preserved.", asset=session.get(Asset, existing.target_asset_id) if session and existing.target_asset_id else None)
                continue
            if asset is None and (not dry_run or row["asset_id"] not in eligible_asset_ids):
                add_record(session if not dry_run else None, run if not dry_run else None, manifest, table="asset_aliases", legacy_id=row["id"], source_hash=fingerprint, outcome="excluded", reason="Parent asset was not imported.")
                continue
            if dry_run:
                add_record(None, None, manifest, table="asset_aliases", legacy_id=row["id"], source_hash=fingerprint, outcome="created")
                continue
            duplicate = session.scalar(select(AssetAlias).where(AssetAlias.asset_id == asset.id, AssetAlias.normalized_alias == normalize_name(row["alias_name"])))
            if duplicate:
                add_record(session, run, manifest, table="asset_aliases", legacy_id=row["id"], source_hash=fingerprint, outcome="conflict", reason="Alias already exists for the V2 asset.", asset=asset)
                continue
            alias = AssetAlias(asset_id=asset.id, alias=row["alias_name"], normalized_alias=normalize_name(row["alias_name"]), alias_kind="legacy", source="v1", valid_from=utc_now().date(), active=bool(row["active"]), created_at=utc_now())
            session.add(alias)
            session.flush()
            add_record(session, run, manifest, table="asset_aliases", legacy_id=row["id"], source_hash=fingerprint, outcome="created", asset=asset)

        mappings = list(source_db.execute("SELECT id, asset_id, provider, external_id, external_name, enabled FROM asset_integrations ORDER BY id"))
        connections: dict[str, ProviderConnection] = {}
        for row in mappings:
            fingerprint = row_hash(row, row.keys())
            existing = prior_record(session, source, "asset_integrations", row["id"]) if session else None
            asset = imported_assets.get(row["asset_id"])
            provider = (row["provider"] or "").strip().lower()
            if existing and existing.source_hash == fingerprint:
                add_record(session, run, manifest, table="asset_integrations", legacy_id=row["id"], source_hash=fingerprint, outcome="reused", asset=session.get(Asset, existing.target_asset_id) if session and existing.target_asset_id else None, persist=False)
                continue
            if existing:
                add_record(session, run, manifest, table="asset_integrations", legacy_id=row["id"], source_hash=fingerprint, outcome="changed_source", reason="V1 mapping changed; existing V2 mapping was preserved.", asset=session.get(Asset, existing.target_asset_id) if session and existing.target_asset_id else None)
                continue
            if (asset is None and (not dry_run or row["asset_id"] not in eligible_asset_ids)) or provider not in LEGACY_CONNECTIONS or not row["external_id"]:
                add_record(session if not dry_run else None, run if not dry_run else None, manifest, table="asset_integrations", legacy_id=row["id"], source_hash=fingerprint, outcome="excluded", reason="Parent asset, supported provider, or external ID is unavailable.")
                continue
            if dry_run:
                add_record(None, None, manifest, table="asset_integrations", legacy_id=row["id"], source_hash=fingerprint, outcome="created")
                continue
            connection = connections.setdefault(provider, legacy_connection(session, provider))
            collision = session.scalar(
                select(AssetProviderMapping).where(
                    AssetProviderMapping.provider_connection_id == connection.id,
                    AssetProviderMapping.resource_kind == "plant",
                    AssetProviderMapping.normalized_external_id == normalize_external_id(provider, row["external_id"]),
                )
            )
            if collision is not None:
                add_record(session, run, manifest, table="asset_integrations", legacy_id=row["id"], source_hash=fingerprint, outcome="conflict", reason="Provider plant is already represented by a legacy mapping.", asset=asset)
                continue
            try:
                mapping = create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id=row["external_id"], external_name=row["external_name"], mapping_status="pending_review", notes="Imported from V1; disabled legacy connection.")
            except ValueError as exc:
                add_record(session, run, manifest, table="asset_integrations", legacy_id=row["id"], source_hash=fingerprint, outcome="conflict", reason=str(exc), asset=asset)
                continue
            add_record(session, run, manifest, table="asset_integrations", legacy_id=row["id"], source_hash=fingerprint, outcome="created", asset=asset, mapping=mapping)

        source_tables = {row[0] for row in source_db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        unresolved = list(source_db.execute("SELECT id, provider, external_id, external_name, normalized_name, external_status, resolution_status FROM integration_unresolved ORDER BY id")) if "integration_unresolved" in source_tables else []
        for row in unresolved:
            fingerprint = row_hash(row, row.keys())
            existing = prior_record(session, source, "integration_unresolved", row["id"]) if session else None
            if existing and existing.source_hash == fingerprint:
                add_record(session, run, manifest, table="integration_unresolved", legacy_id=row["id"], source_hash=fingerprint, outcome="reused", persist=False)
                continue
            if existing:
                add_record(session, run, manifest, table="integration_unresolved", legacy_id=row["id"], source_hash=fingerprint, outcome="changed_source", reason="V1 unresolved integration changed; no automatic mapping was created.")
                continue
            add_record(session if not dry_run else None, run if not dry_run else None, manifest, table="integration_unresolved", legacy_id=row["id"], source_hash=fingerprint, outcome="unresolved", reason=f"{row['provider']} unresolved integration ({row['resolution_status'] or 'unknown'}); no automatic asset or mapping match.")

        if run is not None:
            run.finished_at = utc_now()
            run.manifest_json = manifest.as_dict()
            session.flush()
        if session is not None and batch_size is not None:
            session.commit()
        return manifest.as_dict()
    finally:
        source_db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Import the limited V1 asset inventory into V2.")
    parser.add_argument("--v1-db", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_environment().validate()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    with factory() as session:
        if args.dry_run:
            manifest = import_v1_assets(None, args.v1_db, dry_run=True)
        else:
            manifest = import_v1_assets(session, args.v1_db, batch_size=IMPORT_BATCH_SIZE)
        print(json.dumps(manifest, sort_keys=True))


if __name__ == "__main__":
    main()
