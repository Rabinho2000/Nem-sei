"""Import V1's portfolios as data, not as a design.

V1 is read strictly read-only and its screens are not consulted. What comes
across is what its tables hold: two portfolios, 80 members with their
sub-accounts and NIFs, and the manual mapping decisions its operators made.

Two rules govern this import.

**Nothing is guessed.** 23 of V1's members have no installation. They arrive as
members with `resolution_state` `unresolved` (a real company, NIF and all) or
`placeholder` (a slot V1 named "importar mais tarde"), carrying their evidence.
A NIF that exactly matches a V2 organization is reported as a *candidate* for an
operator to confirm — it names a customer, not an installation, and turning it
into membership would be exactly the destructive matching this avoids.

**Running twice changes nothing.** Portfolios and members are keyed by their V1
ids in provenance, so a second run reports what it skipped and writes nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.assets.models import Organization
from nemsei.assets.v1_import import open_v1_readonly
from nemsei.portfolios.models import Portfolio, PortfolioMembership
from nemsei.portfolios.service import add_member, create_portfolio
from nemsei.providers.models import LegacyImportRecord


# V1 named these members explicitly as slots to fill later. They are imported so
# the portfolio's shape is preserved, but they are not pretending to be
# installations.
PLACEHOLDER_STATUS = "missing_source"


@dataclass
class PortfolioImportManifest:
    portfolios_created: int = 0
    portfolios_skipped: int = 0
    members_created: int = 0
    members_skipped: int = 0
    members_resolved: int = 0
    members_unresolved: int = 0
    members_placeholder: int = 0
    unmapped_without_v2_asset: int = 0
    nif_candidates: list[dict[str, Any]] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "portfolios_created": self.portfolios_created,
            "portfolios_skipped": self.portfolios_skipped,
            "members_created": self.members_created,
            "members_skipped": self.members_skipped,
            "members_resolved": self.members_resolved,
            "members_unresolved": self.members_unresolved,
            "members_placeholder": self.members_placeholder,
            "unmapped_without_v2_asset": self.unmapped_without_v2_asset,
            "nif_candidates": self.nif_candidates,
            "notes": self.notes[:50],
        }


def _asset_ids_by_legacy_id(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(LegacyImportRecord.legacy_id, LegacyImportRecord.target_asset_id).where(
            LegacyImportRecord.legacy_table == "assets",
            LegacyImportRecord.target_asset_id.is_not(None),
        )
    ).all()
    return {str(legacy_id): asset_id for legacy_id, asset_id in rows}


def _portfolio_for_v1(session: Session, v1_id: int) -> Portfolio | None:
    for portfolio in session.scalars(select(Portfolio).where(Portfolio.source_kind == "v1_import")):
        if (portfolio.provenance_json or {}).get("v1_portfolio_id") == v1_id:
            return portfolio
    return None


def _membership_for_v1(session: Session, portfolio_id: int, v1_member_id: int) -> PortfolioMembership | None:
    for membership in session.scalars(
        select(PortfolioMembership).where(PortfolioMembership.portfolio_id == portfolio_id)
    ):
        if (membership.provenance_json or {}).get("v1_portfolio_asset_id") == v1_member_id:
            return membership
    return None


def _resolution_state(row: Any, asset_id: int | None) -> str:
    if asset_id is not None:
        return "resolved"
    if str(row["mapping_status"] or "") == PLACEHOLDER_STATUS:
        return "placeholder"
    return "unresolved"


def import_v1_portfolios(
    session: Session,
    source_path: Path,
    *,
    operator: str,
    valid_from: date | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Import V1's portfolios and their members, idempotently."""
    actor = operator.strip()
    if not actor:
        raise ValueError("An importing operator is required.")
    manifest = PortfolioImportManifest()
    legacy_to_v2 = _asset_ids_by_legacy_id(session)
    connection = open_v1_readonly(source_path)
    try:
        for group in connection.execute(
            "SELECT id, name, description, notes, active, created_at, archived_at FROM portfolio_groups ORDER BY id"
        ):
            existing = _portfolio_for_v1(session, group["id"])
            if existing is not None:
                manifest.portfolios_skipped += 1
                portfolio = existing
            elif dry_run:
                manifest.portfolios_created += 1
                portfolio = None
            else:
                portfolio = create_portfolio(
                    session,
                    name=str(group["name"]),
                    description=group["description"] or group["notes"],
                    created_by=actor,
                    source_kind="v1_import",
                    provenance={"v1_portfolio_id": group["id"], "v1_created_at": group["created_at"]},
                )
                manifest.portfolios_created += 1

            # A membership needs a start date. V1 records none, so the
            # portfolio's own creation date is used rather than today: the
            # members were in it from the beginning, and dating them from the
            # import would make every historical snapshot empty.
            started = valid_from or _date_or(group["created_at"], date(2026, 6, 26))

            for row in connection.execute(
                "SELECT * FROM portfolio_assets WHERE portfolio_id = ? ORDER BY display_order, id",
                (group["id"],),
            ):
                if portfolio is not None and _membership_for_v1(session, portfolio.id, row["id"]) is not None:
                    manifest.members_skipped += 1
                    continue

                v2_asset_id = legacy_to_v2.get(str(row["asset_id"])) if row["asset_id"] is not None else None
                if row["asset_id"] is not None and v2_asset_id is None:
                    # A mapped V1 member whose asset never reached V2 would be
                    # silently dropped otherwise.
                    manifest.unmapped_without_v2_asset += 1
                    manifest.notes.append(
                        {"member": row["id"], "reason": "v1_asset_not_in_v2", "v1_asset_id": row["asset_id"]}
                    )
                state = _resolution_state(row, v2_asset_id)
                if state == "resolved":
                    manifest.members_resolved += 1
                elif state == "placeholder":
                    manifest.members_placeholder += 1
                else:
                    manifest.members_unresolved += 1
                    candidate = _nif_candidate(session, row["nif"])
                    if candidate is not None:
                        manifest.nif_candidates.append(
                            {
                                "member": row["id"],
                                "external_name": row["external_name"],
                                "nif": row["nif"],
                                "organization_id": candidate.id,
                                "organization_name": candidate.display_name,
                                "note": "exact NIF match — names a customer, not an installation; confirm manually",
                            }
                        )

                if dry_run or portfolio is None:
                    manifest.members_created += 1
                    continue

                add_member(
                    session,
                    portfolio_id=portfolio.id,
                    asset_id=v2_asset_id,
                    sub_account=(row["sub_account"] or None),
                    external_name=(row["external_name"] or None),
                    tax_id=(row["nif"] or None),
                    resolution_state=state,
                    valid_from=started,
                    valid_to=None if row["active"] else started,
                    created_by=actor,
                    source_kind="v1_import",
                    provenance={
                        "v1_portfolio_asset_id": row["id"],
                        "v1_asset_id": row["asset_id"],
                        "v1_mapping_status": row["mapping_status"],
                        "v1_mapping_method": row["mapping_method"],
                        "v1_mapping_confidence": row["mapping_confidence"],
                        "v1_mapped_at": row["mapped_at"],
                    },
                    notes=row["notes"] or None,
                )
                manifest.members_created += 1
        if not dry_run:
            session.flush()
    finally:
        connection.close()
    return manifest.as_dict()


def _nif_candidate(session: Session, nif: Any) -> Organization | None:
    """An exact tax-id match, offered as evidence and never applied."""
    value = str(nif or "").strip()
    if not value:
        return None
    return session.scalar(select(Organization).where(Organization.normalized_tax_id == value))


def _date_or(value: Any, fallback: date) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return fallback
