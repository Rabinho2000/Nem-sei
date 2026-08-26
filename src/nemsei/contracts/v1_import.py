"""Import V1's O&M engagements: the answer to "which plants do we operate?".

V1 spread that answer over `assets.maintenance`, `assets.active_contract`,
`assets.start_contract`, `assets.end_contract`, `assets.duration` and the
`om_contracts` table. This importer reduces it to one temporal fact per
installation and *verifies* the rest rather than copying it.

Three rules earn their place, each from the frozen snapshot rather than from
the schema:

`active_contract` is never written. It is derived from the period and compared
against V1's stored value; a mismatch is recorded as an issue. Across 267 V1
installations the derivation reproduces V1 exactly, which is what makes it safe
to stop storing.

Dates come from `om_contracts` first and `assets` second. That is V1's own
`COALESCE(NULLIF(oc.contract_end_date, ''), NULLIF(a.end_contract, ''))`, used
by its renewals screen and by `sync_asset_contract_status`.

`end_contract` is inclusive and `valid_to` is exclusive, so one day is added --
the same correction `v1_reporting_import` already applies to tariffs.

Reads V1 strictly read-only. Writes nothing to V1, ever.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.v1_import import open_v1_readonly, source_sha256
from nemsei.config import Settings
from nemsei.contracts.models import AssetServiceContract
from nemsei.contracts.service import set_service_contract
from nemsei.db.engine import build_engine
from nemsei.db.session import build_session_factory
from nemsei.providers.models import LegacyImportRecord

IMPORTER_VERSION = "contracts-v1-importer/1.0"
# V1 writes "yes"/"no" but its own reader accepts more, and a snapshot taken
# after a manual edit could carry any of them.
TRUTHY = {"yes", "true", "1", "ativo", "active", "sim"}
# `assets.duration` is years, and 132 of its 237 populated values are the
# string "0". It is kept as provenance, never as a bound.
EMPTY_DATES = {"", "-", "none", "null"}


@dataclass
class ContractImportManifest:
    source_database_sha256: str = ""
    dry_run: bool = True
    as_of: str = ""
    contracts_created: int = 0
    contracts_existing: int = 0
    assets_out_of_scope: int = 0
    assets_without_v2_match: int = 0
    needs_review: int = 0
    derivation_checked: int = 0
    derivation_mismatches: int = 0
    issues: list[dict[str, str]] = field(default_factory=list)

    def issue(self, legacy_id: Any, reason: str, detail: str = "") -> None:
        self.issues.append({"legacy_id": str(legacy_id), "reason": reason, "detail": detail})

    def as_dict(self) -> dict[str, Any]:
        return {
            "importer_version": IMPORTER_VERSION,
            "source_database_sha256": self.source_database_sha256,
            "dry_run": self.dry_run,
            "as_of": self.as_of,
            "counts": {
                "contracts_created": self.contracts_created,
                "contracts_existing": self.contracts_existing,
                "assets_out_of_scope": self.assets_out_of_scope,
                "assets_without_v2_match": self.assets_without_v2_match,
                "needs_review": self.needs_review,
                "derivation_checked": self.derivation_checked,
                "derivation_mismatches": self.derivation_mismatches,
            },
            "issues": self.issues,
        }


def _clean_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if text.lower() in EMPTY_DATES:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _clean_decimal(value: Any) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _is_true(value: Any) -> bool:
    return str(value or "").strip().lower() in TRUTHY


def _asset_ids_by_legacy_id(session: Session) -> dict[str, int]:
    """V1 asset id to V2 asset id, from the identity import's own record."""
    rows = session.execute(
        select(LegacyImportRecord.legacy_id, LegacyImportRecord.target_asset_id).where(
            LegacyImportRecord.legacy_table == "assets",
            LegacyImportRecord.target_asset_id.is_not(None),
        )
    ).all()
    return {str(legacy_id): asset_id for legacy_id, asset_id in rows}


def _already_imported(session: Session, *, asset_id: int, legacy_asset_id: Any) -> bool:
    """Whether this V1 row already produced a contract here.

    Keyed on the V1 row id inside the provenance, which is what makes a second
    run a no-op instead of a duplicate period -- the same guard
    `v1_reporting_import` uses for tariffs.
    """
    for contract in session.scalars(
        select(AssetServiceContract).where(AssetServiceContract.asset_id == asset_id)
    ):
        if str(contract.provenance_json.get("v1_asset_id", "")) == str(legacy_asset_id):
            return True
    return False


def import_v1_service_contracts(
    session: Session,
    source_path: Path,
    *,
    operator: str,
    dry_run: bool = True,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Create one O&M engagement per V1 installation that carries one."""
    moment = as_of or date.today()
    manifest = ContractImportManifest(
        dry_run=dry_run,
        as_of=moment.isoformat(),
        # Which snapshot this manifest describes. Without it a run report says
        # what was imported but not from what, and two runs against different
        # exports are indistinguishable afterwards.
        source_database_sha256=source_sha256(source_path),
    )
    legacy_to_v2 = _asset_ids_by_legacy_id(session)
    connection = open_v1_readonly(source_path)
    try:
        om_rows = {
            row["asset_id"]: row
            for row in connection.execute(
                "SELECT asset_id, contract_start_date, contract_end_date, annual_value, notes,"
                " pdf_path, original_filename, renewal_status, last_contact_date, renewal_notes"
                " FROM om_contracts"
            )
        }
        assets = connection.execute(
            "SELECT id, project_name, maintenance, active_contract, start_contract,"
            " end_contract, duration FROM assets ORDER BY id"
        )
        for row in assets:
            legacy_id = row["id"]
            om_row = om_rows.get(legacy_id)
            in_scope = _is_true(row["maintenance"]) or om_row is not None
            if not in_scope:
                manifest.assets_out_of_scope += 1
                continue

            v2_asset_id = legacy_to_v2.get(str(legacy_id))
            if v2_asset_id is None:
                manifest.assets_without_v2_match += 1
                manifest.issue(legacy_id, "asset_not_imported", str(row["project_name"] or ""))
                continue

            # V1's own precedence: the contracts table wins over the columns.
            start = _clean_date(om_row["contract_start_date"] if om_row else None) or _clean_date(
                row["start_contract"]
            )
            end_inclusive = _clean_date(om_row["contract_end_date"] if om_row else None) or _clean_date(
                row["end_contract"]
            )
            valid_to = end_inclusive + timedelta(days=1) if end_inclusive else None

            _verify_derivation(manifest, row=row, end_inclusive=end_inclusive, as_of=moment)

            review_status, review_note = _review_for(
                start=start, end_inclusive=end_inclusive, om_row=om_row, maintenance=row["maintenance"]
            )
            if start is not None and end_inclusive is not None and end_inclusive < start:
                # Not representable and not guessable. No row in the frozen
                # snapshot does this, but a later export could, and silently
                # swapping the bounds would be inventing a contract.
                manifest.issue(
                    legacy_id,
                    "end_before_start",
                    f"{start.isoformat()} -> {end_inclusive.isoformat()}",
                )
                start, valid_to = None, None
                review_status, review_note = "needs_review", "fim anterior ao início no V1"
            elif start is not None and valid_to is not None and (valid_to - start).days <= 1:
                # V1 holds two engagements that start and end on the same day
                # (Vatel 2023-03-01, Banco BIG 2024-03-07). Because V1's end is
                # inclusive, that is a one-day contract -- representable, and
                # imported exactly as stated. It is also implausible for an O&M
                # engagement, so it is flagged rather than quietly kept.
                manifest.issue(
                    legacy_id,
                    "one_day_window",
                    f"{start.isoformat()} -> {end_inclusive.isoformat() if end_inclusive else ''}",
                )
                review_status = "needs_review"
                review_note = "; ".join(filter(None, [review_note, "período de um dia no V1"]))

            if review_status == "needs_review":
                manifest.needs_review += 1

            if _already_imported(session, asset_id=v2_asset_id, legacy_asset_id=legacy_id):
                manifest.contracts_existing += 1
                continue

            manifest.contracts_created += 1
            if dry_run:
                continue

            provenance = {
                "v1_asset_id": legacy_id,
                "v1_maintenance": row["maintenance"],
                "v1_active_contract": row["active_contract"],
                "v1_start_contract": row["start_contract"],
                "v1_end_contract": row["end_contract"],
                "v1_duration_years": row["duration"],
                "v1_om_contract": om_row is not None,
                # The end date as V1 stated it, before the inclusive-to-
                # exclusive correction, so the +1 day stays auditable.
                "v1_end_inclusive": end_inclusive.isoformat() if end_inclusive else None,
            }
            set_service_contract(
                session,
                asset_id=v2_asset_id,
                created_by=operator,
                valid_from=start,
                valid_to=valid_to,
                annual_value_eur=_clean_decimal(om_row["annual_value"]) if om_row else None,
                notes=(om_row["notes"] if om_row else None),
                source_kind="v1_import",
                provenance=provenance,
                review_status=review_status,
                review_note=review_note,
            )
    finally:
        connection.close()
    return manifest.as_dict()


def _verify_derivation(
    manifest: ContractImportManifest, *, row: Any, end_inclusive: date | None, as_of: date
) -> None:
    """Check V1's stored `active_contract` against what V2 will derive.

    Nothing is written from this. It is the migration's own proof that the
    stored flag was redundant -- if it ever stops reconciling, the assumption
    behind not storing it has broken and the manifest says so.
    """
    if end_inclusive is None:
        # V1's `derive_active_contract` keeps the stored value when it has no
        # end date to judge by, so there is nothing to reconcile here.
        return
    manifest.derivation_checked += 1
    derived = end_inclusive >= as_of
    if derived != _is_true(row["active_contract"]):
        manifest.derivation_mismatches += 1
        manifest.issue(
            row["id"],
            "active_contract_mismatch",
            f"v1={row['active_contract']} derived={'yes' if derived else 'no'} end={end_inclusive.isoformat()}",
        )


def _review_for(
    *, start: date | None, end_inclusive: date | None, om_row: Any, maintenance: Any
) -> tuple[str, str | None]:
    reasons: list[str] = []
    if start is None:
        reasons.append("sem data de início no V1")
    if end_inclusive is None:
        reasons.append("sem data de fim no V1")
    if om_row is not None and not _is_true(maintenance):
        # V1 held no such row at the time of writing, but the combination is
        # representable and would mean the two sources disagree about scope.
        reasons.append("om_contracts existe mas maintenance não é 'yes'")
    if not reasons:
        return "clear", None
    return "needs_review", "; ".join(reasons)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import V1 O&M contracts into V2.")
    parser.add_argument("--source", required=True, type=Path, help="Path to the frozen V1 SQLite file")
    parser.add_argument("--operator", required=True, help="Who is running this import")
    parser.add_argument("--apply", action="store_true", help="Write. Without it the run is a dry run.")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None, help="Date used to verify V1's derived flag")
    arguments = parser.parse_args(argv)

    settings = Settings.from_environment()
    session_factory = build_session_factory(build_engine(settings))
    with session_factory() as session:
        manifest = import_v1_service_contracts(
            session,
            arguments.source,
            operator=arguments.operator,
            dry_run=not arguments.apply,
            as_of=arguments.as_of,
        )
        if arguments.apply:
            session.commit()
        else:
            session.rollback()
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
