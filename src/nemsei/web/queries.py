"""Focused read models for the authenticated V2 web interface."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from math import ceil
from typing import Any

from sqlalchemy import Select, exists, false, func, select
from sqlalchemy.orm import Session

from nemsei.assets.models import Asset, AssetAlias, Organization
from nemsei.assets.service import asset_search_clause, normalize_name
from nemsei.monitoring.installation_state import current_installation_states
from nemsei.monitoring.models import MonitoringObservation, ProductionFact
from nemsei.providers.models import AssetProviderMapping, LegacyImportRecord, ProviderConnection
from nemsei.providers.registry import descriptor_for
from nemsei.providers.service import mapping_approval_blockers
from nemsei.reporting.commercial import resolve_billing_config, resolve_tariff
from nemsei.reporting.commercial_models import BILLING_ENERGY_BASES, BILLING_MODES, REPORT_TYPES, TARIFF_TYPES
from nemsei.reporting.models import FinancialModel
from nemsei.shared.clock import utc_now
from nemsei.sources.models import AssetSourcePolicy
from nemsei.sync.models import IntegrationHealth, SyncRun
from nemsei.web.contract_queries import commercial_states_for, family_filter_ids, om_filter_ids, om_states_for
from nemsei.web.labels import mapping_state, mapping_summary, review_state


PROVIDER_LABELS = {
    "fusionsolar": "FusionSolar",
    "sigenergy": "Sigenergy",
    "sma": "SMA",
    "huawei_scada": "Huawei SCADA",
}


def provider_label(provider_code: str | None) -> str:
    return PROVIDER_LABELS.get(provider_code or "", provider_code or "—")


def _page(value: str | None, *, default: int = 1) -> int:
    try:
        return max(1, int(value or default))
    except (TypeError, ValueError):
        return default


def _pagination(*, page: int, per_page: int, total: int) -> dict[str, Any]:
    pages = max(1, ceil(total / per_page))
    current_page = min(max(1, page), pages)
    return {
        "page": current_page,
        "per_page": per_page,
        "total": total,
        "pages": pages,
        "has_previous": current_page > 1,
        "has_next": current_page < pages,
        "previous": max(1, current_page - 1),
        "next": min(pages, current_page + 1),
    }


ASSET_LIFECYCLE_LABELS = {"unknown": "Desconhecida", "active": "Ativa", "inactive": "Inativa", "decommissioned": "Desativada"}


def _asset_filters(
    *,
    search: str,
    needs_review: str,
    provider: str,
    mapping: str,
    om_ids: set[int] | None = None,
    family_ids: set[int] | None = None,
) -> list[Any]:
    filters: list[Any] = []
    if family_ids is not None:
        filters.append(Asset.id.in_(family_ids) if family_ids else false())
    if om_ids is not None:
        # Resolved to ids before the query rather than expressed as a join:
        # O&M state is derived from date arithmetic over the contract table,
        # not a column, and pushing it into SQL here would duplicate the
        # derivation `contracts.service` owns. The fleet is 267 rows.
        filters.append(Asset.id.in_(om_ids) if om_ids else false())
    search_clause = asset_search_clause(search)
    if search_clause is not None:
        filters.append(search_clause)
    if needs_review == "yes":
        filters.append(Asset.review_status == "needs_review")
    if provider in PROVIDER_LABELS:
        filters.append(
            exists(
                select(1)
                .select_from(AssetProviderMapping)
                .join(ProviderConnection, ProviderConnection.id == AssetProviderMapping.provider_connection_id)
                .where(
                    AssetProviderMapping.asset_id == Asset.id,
                    AssetProviderMapping.valid_to.is_(None),
                    AssetProviderMapping.provider_connection_id == ProviderConnection.id,
                    ProviderConnection.provider_code == provider,
                )
            )
        )
    if mapping == "present":
        filters.append(
            exists(
                select(1).where(
                    AssetProviderMapping.asset_id == Asset.id,
                    AssetProviderMapping.valid_to.is_(None),
                )
            )
        )
    elif mapping == "absent":
        filters.append(
            ~exists(
                select(1).where(
                    AssetProviderMapping.asset_id == Asset.id,
                    AssetProviderMapping.valid_to.is_(None),
                )
            )
        )
    return filters


def _asset_base_statement(filters: list[Any]) -> Select:
    statement = (
        select(Asset, Organization.display_name.label("organization_name"))
        .outerjoin(Organization, Organization.id == Asset.owner_id)
        .order_by(Asset.canonical_name.asc(), Asset.id.asc())
    )
    return statement.where(*filters) if filters else statement


def _mapping_summaries(session: Session, asset_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not asset_ids:
        return {}
    rows = session.execute(
        select(
            AssetProviderMapping.asset_id,
            ProviderConnection.provider_code,
            ProviderConnection.display_name.label("connection_name"),
            AssetProviderMapping.mapping_status,
        )
        .join(ProviderConnection, ProviderConnection.id == AssetProviderMapping.provider_connection_id)
        .where(
            AssetProviderMapping.asset_id.in_(asset_ids),
            AssetProviderMapping.resource_kind == "plant",
            AssetProviderMapping.valid_to.is_(None),
        )
        .order_by(AssetProviderMapping.asset_id, ProviderConnection.provider_code, AssetProviderMapping.id)
    ).mappings()
    summaries: dict[int, dict[str, Any]] = defaultdict(lambda: {"providers": [], "mappings": [], "statuses": []})
    for row in rows:
        item = summaries[row["asset_id"]]
        provider = provider_label(row["provider_code"])
        if provider not in item["providers"]:
            item["providers"].append(provider)
        item["statuses"].append(row["mapping_status"])
        item["mappings"].append(
            {
                "provider": provider,
                "connection_name": row["connection_name"],
                "mapping_status": row["mapping_status"],
            }
        )
    for asset_id in asset_ids:
        item = summaries[asset_id]
        # Worst-first, from the shared vocabulary. The previous verdict here
        # asked only "are there any mappings at all", so an installation whose
        # single mapping the platform had marked `invalid` reported "Mapeado"
        # in green -- a plant receiving nothing, rendered as healthy.
        item.update(mapping_summary(item["statuses"]))
    return summaries


def list_assets_data(
    session: Session,
    *,
    search: str = "",
    needs_review: str = "",
    provider: str = "",
    mapping: str = "",
    om: str = "",
    family: str = "",
    page_value: str | None = None,
    per_page: int = 25,
) -> dict[str, Any]:
    page = _page(page_value)
    filters = _asset_filters(
        search=search,
        needs_review=needs_review,
        provider=provider,
        mapping=mapping,
        om_ids=om_filter_ids(session, om=om),
        family_ids=family_filter_ids(session, family=family),
    )
    total = session.scalar(
        select(func.count(Asset.id)).outerjoin(Organization, Organization.id == Asset.owner_id).where(*filters)
    ) or 0
    pagination = _pagination(page=page, per_page=per_page, total=total)
    effective_page = pagination["page"]
    rows = session.execute(
        _asset_base_statement(filters).limit(per_page).offset((effective_page - 1) * per_page)
    ).all()
    summaries = _mapping_summaries(session, [asset.id for asset, _ in rows])
    # One page at a time, so this stays two queries regardless of fleet size.
    states = current_installation_states(session, asset_ids=[asset.id for asset, _ in rows])
    om_states = om_states_for(session, asset_ids=[asset.id for asset, _ in rows])
    commercial = commercial_states_for(session, assets={asset.id: asset.contract_type for asset, _ in rows})
    assets: list[dict[str, Any]] = []
    for asset, organization_name in rows:
        summary = summaries[asset.id]
        state = states[asset.id]
        assets.append(
            {
                "id": asset.id,
                "canonical_name": asset.canonical_name,
                "organization_name": organization_name,
                "installed_dc_power_kw": asset.installed_dc_power_kw,
                "country_code": asset.country_code,
                "locality": asset.locality,
                # Shown so the bulk editor tells the operator what they are
                # about to overwrite, instead of asking them to trust it.
                "timezone": asset.timezone,
                "lifecycle_status": asset.lifecycle_status,
                "lifecycle_label": ASSET_LIFECYCLE_LABELS.get(asset.lifecycle_status, asset.lifecycle_status),
                # The operational answer, kept separate from the
                # administrative one above -- they are different questions and
                # were previously both called "Estado".
                "state": state.state,
                "state_label": state.label,
                "state_tone": state.tone,
                "state_detail": state.detail,
                "state_observed_at": state.observed_at,
                # The import review flag, never the operational one. It used
                # to read "OK" in green next to an installation whose actual
                # state was "Sem leitura" -- two different questions answered
                # with one word, and the reassuring one won.
                "review_status": asset.review_status,
                "review_label": review_state(asset.review_status)["label"],
                "review_tone": review_state(asset.review_status)["tone"],
                "providers": summary["providers"],
                "mapping_label": summary["label"],
                "mapping_tone": summary["tone"],
                # Whether Solcor operates this plant, and until when. Derived,
                # never stored -- see nemsei/contracts/models.py.
                "om": om_states[asset.id],
                # What Solcor sells here, and how urgently its problems should
                # be worked -- see nemsei/contracts/priority.py.
                "commercial": commercial[asset.id],
            }
        )
    return {
        "assets": assets,
        "pagination": pagination,
        "filters": {"search": search, "needs_review": needs_review, "provider": provider, "mapping": mapping, "om": om, "family": family},
        "provider_options": [(code, provider_label(code)) for code in PROVIDER_LABELS],
    }


def _legacy_counts(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(LegacyImportRecord.outcome, func.count(LegacyImportRecord.id)).group_by(LegacyImportRecord.outcome)
    ).all()
    return Counter({outcome: count for outcome, count in rows})


def review_summary(session: Session) -> dict[str, int]:
    outcomes = _legacy_counts(session)
    return {
        "assets_needs_review": session.scalar(select(func.count(Asset.id)).where(Asset.review_status == "needs_review")) or 0,
        "organizations_needs_review": session.scalar(select(func.count(Organization.id)).where(Organization.review_status == "needs_review")) or 0,
        "mappings_pending_review": session.scalar(select(func.count(AssetProviderMapping.id)).where(AssetProviderMapping.mapping_status == "pending_review")) or 0,
        "quarantined": outcomes.get("quarantined", 0),
        "unresolved": outcomes.get("unresolved", 0),
        "conflicts": outcomes.get("conflict", 0),
        "excluded": outcomes.get("excluded", 0),
    }


def dashboard_data(session: Session) -> dict[str, Any]:
    counts = session.execute(
        select(
            select(func.count(Asset.id)).scalar_subquery().label("assets"),
            select(func.count(Organization.id)).scalar_subquery().label("organizations"),
            select(func.count(ProviderConnection.id)).scalar_subquery().label("connections"),
            select(func.count(AssetProviderMapping.id)).where(AssetProviderMapping.valid_to.is_(None)).scalar_subquery().label("mappings"),
        )
    ).mappings().one()
    mapping_counts = (
        select(AssetProviderMapping.provider_connection_id, func.count(AssetProviderMapping.id).label("mapping_count"))
        .where(AssetProviderMapping.valid_to.is_(None))
        .group_by(AssetProviderMapping.provider_connection_id)
        .subquery()
    )
    provider_rows = session.execute(
        select(
            ProviderConnection.id.label("connection_id"),
            ProviderConnection.provider_code,
            ProviderConnection.display_name,
            ProviderConnection.enabled,
            ProviderConnection.configuration_status,
            func.coalesce(mapping_counts.c.mapping_count, 0).label("mapping_count"),
        )
        .outerjoin(mapping_counts, mapping_counts.c.provider_connection_id == ProviderConnection.id)
        .order_by(ProviderConnection.provider_code, ProviderConnection.display_name)
    ).mappings()
    connections = []
    for row in provider_rows:
        if not row["enabled"] or row["configuration_status"] == "disabled":
            state_label, state_tone = "Desativada", "muted"
        elif row["configuration_status"] == "not_configured":
            state_label, state_tone = "Por configurar", "warning"
        else:
            state_label, state_tone = "Configurada", "success"
        connections.append(
            {
                "id": row["connection_id"],
                "provider": provider_label(row["provider_code"]),
                "display_name": row["display_name"],
                "state_label": state_label,
                "state_tone": state_tone,
                "mapping_count": row["mapping_count"],
                "credential_label": "Configuradas" if row["configuration_status"] == "configured" else "Não configuradas",
            }
        )
    review = review_summary(session)
    sync_runs = session.scalar(select(func.count(SyncRun.id))) or 0
    observations = session.scalar(select(func.count(MonitoringObservation.id))) or 0
    production_facts = session.scalar(select(func.count(ProductionFact.id))) or 0
    return {
        "counts": dict(counts),
        "review": review,
        "connections": connections,
        "sync_runs": sync_runs,
        "monitoring_observations": observations,
        "production_facts": production_facts,
    }


def organization_list_data(session: Session, *, search: str = "", page_value: str | None = None, per_page: int = 25) -> dict[str, Any]:
    page = _page(page_value)
    filters = []
    if search.strip():
        filters.append(Organization.display_name.ilike(f"%{search.strip()}%"))
    total = session.scalar(select(func.count(Organization.id)).where(*filters)) or 0
    pagination = _pagination(page=page, per_page=per_page, total=total)
    rows = session.execute(
        select(
            Organization.id,
            Organization.display_name,
            Organization.review_status,
            func.count(Asset.id).label("asset_count"),
        )
        .outerjoin(Asset, Asset.owner_id == Organization.id)
        .where(*filters)
        .group_by(Organization.id, Organization.display_name, Organization.review_status)
        .order_by(Organization.display_name, Organization.id)
        .limit(per_page)
        .offset((pagination["page"] - 1) * per_page)
    ).mappings()
    organizations = [
        {
            "id": row["id"],
            "display_name": row["display_name"],
            "asset_count": row["asset_count"],
            "review_label": review_state(row["review_status"])["label"],
            "review_tone": review_state(row["review_status"])["tone"],
        }
        for row in rows
    ]
    return {"organizations": organizations, "pagination": pagination, "search": search}


def provider_connections_data(session: Session, *, settings: Any | None = None) -> list[dict[str, Any]]:
    mapping_counts = (
        select(AssetProviderMapping.provider_connection_id, func.count(AssetProviderMapping.id).label("mapping_count"))
        .where(AssetProviderMapping.valid_to.is_(None))
        .group_by(AssetProviderMapping.provider_connection_id)
        .subquery()
    )
    rows = session.execute(
        select(
            ProviderConnection.id.label("connection_id"),
            ProviderConnection.provider_code,
            ProviderConnection.display_name,
            ProviderConnection.enabled,
            ProviderConnection.configuration_status,
            func.coalesce(mapping_counts.c.mapping_count, 0).label("mapping_count"),
        )
        .outerjoin(mapping_counts, mapping_counts.c.provider_connection_id == ProviderConnection.id)
        .order_by(ProviderConnection.provider_code, ProviderConnection.display_name)
    ).mappings()
    status_rows = session.execute(
        select(
            AssetProviderMapping.provider_connection_id,
            AssetProviderMapping.mapping_status,
            func.count(AssetProviderMapping.id),
        )
        .where(AssetProviderMapping.valid_to.is_(None))
        .group_by(AssetProviderMapping.provider_connection_id, AssetProviderMapping.mapping_status)
    ).all()
    status_counts: dict[int, dict[str, int]] = defaultdict(dict)
    for connection_id, mapping_status, count in status_rows:
        status_counts[connection_id][mapping_status] = count
    result = []
    for row in rows:
        if not row["enabled"] or row["configuration_status"] == "disabled":
            state_label, state_tone = "Desativada", "muted"
        elif row["configuration_status"] == "not_configured":
            state_label, state_tone = "Por configurar", "warning"
        else:
            state_label, state_tone = "Configurada", "success"
        health = session.get(IntegrationHealth, row["connection_id"])
        descriptor = descriptor_for(row["provider_code"])
        supported = sorted(capability.value for capability in descriptor.implemented_capabilities)
        runtime_enabled = []
        if settings is not None and row["enabled"] and row["configuration_status"] == "configured" and settings.capabilities.get("provider_reads", False):
            runtime_enabled = supported
        result.append(
            {
                "id": row["connection_id"],
                "provider": provider_label(row["provider_code"]),
                "display_name": row["display_name"],
                "state_label": state_label,
                "state_tone": state_tone,
                "active_label": "Sim" if row["enabled"] else "Não",
                "mapping_count": row["mapping_count"],
                "pending_count": status_counts[row["connection_id"]].get("pending_review", 0),
                "active_count": status_counts[row["connection_id"]].get("active", 0),
                "credential_label": "Configuradas" if row["configuration_status"] == "configured" else "Não configuradas",
                "supported_capabilities": supported,
                "runtime_capabilities": runtime_enabled,
                "health": {
                    "auth_state": health.auth_state if health else "unknown",
                    "provider_state": health.provider_state if health else "unknown",
                    "discovery_state": health.discovery_state if health else "unknown",
                    "quota_state": health.quota_state if health else "unknown",
                    "last_success_at": health.last_success_at if health else None,
                    "last_failure_at": health.last_failure_at if health else None,
                },
            }
        )
    return result


# The service speaks English to raise with; the screen speaks to an operator.
BLOCKER_LABELS = {
    "not_pending": "Já não está pendente",
    "missing_relation": "Central ou ligação em falta",
    "asset_needs_review": "Central por rever",
    "connection_not_ready": "Ligação não configurada",
    "external_id_missing": "Sem identificador externo",
    "already_claimed": "Identificador já reclamado",
    "quarantined_evidence": "Evidência em quarentena",
}


def mapping_review_data(
    session: Session,
    *,
    provider: str = "",
    connection_id: str = "",
    status: str = "",
    asset_search: str = "",
    organization_search: str = "",
    needs_review: str = "",
) -> dict[str, Any]:
    statement = (
        select(
            AssetProviderMapping,
            Asset,
            Organization.display_name.label("organization_name"),
            ProviderConnection,
        )
        .join(Asset, Asset.id == AssetProviderMapping.asset_id)
        .outerjoin(Organization, Organization.id == Asset.owner_id)
        .join(ProviderConnection, ProviderConnection.id == AssetProviderMapping.provider_connection_id)
        .order_by(ProviderConnection.provider_code, ProviderConnection.display_name, Asset.canonical_name, AssetProviderMapping.id)
    )
    filters = []
    if provider in PROVIDER_LABELS:
        filters.append(ProviderConnection.provider_code == provider)
    if connection_id.isdigit():
        filters.append(ProviderConnection.id == int(connection_id))
    if status in {"active", "superseded", "invalid", "pending_review"}:
        filters.append(AssetProviderMapping.mapping_status == status)
    if asset_search.strip():
        pattern = f"%{normalize_name(asset_search)}%"
        filters.append(Asset.normalized_name.ilike(pattern))
    if organization_search.strip():
        filters.append(Organization.display_name.ilike(f"%{organization_search.strip()}%"))
    if needs_review == "yes":
        filters.append(Asset.review_status == "needs_review")
    rows = session.execute(statement.where(*filters) if filters else statement).all()
    mappings = []
    for mapping, asset, organization_name, connection in rows:
        evidence = list(
            session.scalars(
                select(LegacyImportRecord)
                .where(LegacyImportRecord.target_mapping_id == mapping.id)
                .order_by(LegacyImportRecord.id.desc())
                .limit(5)
            )
        )
        aliases = list(
            session.scalars(
                select(AssetAlias.alias)
                .where(AssetAlias.asset_id == asset.id, AssetAlias.active.is_(True))
                .order_by(AssetAlias.alias)
            )
        )
        mappings.append(
            {
                "id": mapping.id,
                "asset_id": asset.id,
                "asset_name": asset.canonical_name,
                "organization_name": organization_name,
                "provider": provider_label(connection.provider_code),
                "provider_code": connection.provider_code,
                "connection_id": connection.id,
                "connection_name": connection.display_name,
                "external_id": mapping.external_id,
                "external_name": mapping.external_name,
                "mapping_status": mapping.mapping_status,
                "mapping_label": mapping_state(mapping.mapping_status)["label"],
                "mapping_tone": mapping_state(mapping.mapping_status)["tone"],
                "valid_from": mapping.valid_from,
                "valid_to": mapping.valid_to,
                "asset_review_status": asset.review_status,
                "aliases": aliases,
                "evidence": [{"table": item.legacy_table, "legacy_id": item.legacy_id, "outcome": item.outcome, "reason": item.reason} for item in evidence],
                # Computed by the same function `approve_mapping` obeys, so the
                # screen can never promise an approval the service refuses.
                "blockers": [
                    {"code": blocker.code, "label": BLOCKER_LABELS.get(blocker.code, blocker.message)}
                    for blocker in (
                        mapping_approval_blockers(session, mapping=mapping, asset=asset, connection=connection)
                        if mapping.mapping_status == "pending_review"
                        else ()
                    )
                ],
            }
        )
    connections = list(session.scalars(select(ProviderConnection).order_by(ProviderConnection.display_name)))
    pending = [row for row in mappings if row["mapping_status"] == "pending_review"]
    blocker_counts: dict[str, int] = {}
    for row in pending:
        for blocker in row["blockers"]:
            blocker_counts[blocker["code"]] = blocker_counts.get(blocker["code"], 0) + 1
    return {
        "mappings": mappings,
        "connections": connections,
        "provider_options": [(code, provider_label(code)) for code in PROVIDER_LABELS],
        "filters": {"provider": provider, "connection_id": connection_id, "status": status, "asset_search": asset_search, "organization_search": organization_search, "needs_review": needs_review},
        "readiness": {
            "pending": len(pending),
            "ready": sum(1 for row in pending if not row["blockers"]),
            "blocked": sum(1 for row in pending if row["blockers"]),
            "by_blocker": sorted(
                ({"code": code, "label": BLOCKER_LABELS.get(code, code), "count": count} for code, count in blocker_counts.items()),
                key=lambda item: (-item["count"], item["label"]),
            ),
        },
    }


def source_policy_data(session: Session, *, asset_id: int | None = None) -> list[dict[str, Any]]:
    statement = (
        select(AssetSourcePolicy, Asset, AssetProviderMapping, ProviderConnection)
        .join(Asset, Asset.id == AssetSourcePolicy.asset_id)
        .join(AssetProviderMapping, AssetProviderMapping.id == AssetSourcePolicy.provider_mapping_id)
        .join(ProviderConnection, ProviderConnection.id == AssetProviderMapping.provider_connection_id)
        .order_by(AssetSourcePolicy.valid_from.desc(), AssetSourcePolicy.id.desc())
    )
    if asset_id is not None:
        statement = statement.where(AssetSourcePolicy.asset_id == asset_id)
    return [
        {
            "id": policy.id,
            "asset_id": asset.id,
            "asset_name": asset.canonical_name,
            "mapping_id": mapping.id,
            "provider": provider_label(connection.provider_code),
            "source_use": policy.source_use,
            "priority": policy.priority,
            "is_fallback": policy.is_fallback,
            "valid_from": policy.valid_from,
            "valid_to": policy.valid_to,
        }
        for policy, asset, mapping, connection in session.execute(statement).all()
    ]


def asset_detail_data(session: Session, asset_id: int) -> dict[str, Any] | None:
    row = session.execute(
        select(Asset, Organization.display_name.label("organization_name"))
        .outerjoin(Organization, Organization.id == Asset.owner_id)
        .where(Asset.id == asset_id)
    ).first()
    if row is None:
        return None
    asset, organization_name = row
    aliases = list(session.scalars(select(AssetAlias).where(AssetAlias.asset_id == asset.id, AssetAlias.active.is_(True)).order_by(AssetAlias.alias)))
    mapping_rows = session.execute(
        select(
            AssetProviderMapping.provider_connection_id,
            ProviderConnection.provider_code,
            ProviderConnection.display_name.label("connection_name"),
            AssetProviderMapping.external_name,
            AssetProviderMapping.external_id,
            AssetProviderMapping.mapping_status,
            AssetProviderMapping.valid_from,
            AssetProviderMapping.valid_to,
        )
        .join(ProviderConnection, ProviderConnection.id == AssetProviderMapping.provider_connection_id)
        .where(AssetProviderMapping.asset_id == asset.id)
        .order_by(AssetProviderMapping.valid_from.desc(), AssetProviderMapping.id.desc())
    ).mappings()
    mappings = [
        {
            "provider": provider_label(row["provider_code"]),
            "connection_name": row["connection_name"],
            "external_name": row["external_name"],
            "external_id": row["external_id"],
            "mapping_status": row["mapping_status"],
            "mapping_label": mapping_state(row["mapping_status"])["label"],
            "mapping_tone": mapping_state(row["mapping_status"])["tone"],
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
        }
        for row in mapping_rows
    ]
    review_issues: list[str] = []
    if asset.review_status == "needs_review":
        review_issues.append(asset.review_note or "O ativo foi marcado para revisão na importação.")
    if not asset.timezone:
        review_issues.append("Fuso horário desconhecido.")
    if asset.installed_dc_power_kw is None:
        review_issues.append("Potência DC instalada em falta.")
    if not asset.locality:
        review_issues.append("Localidade em falta.")
    if organization_name is None:
        review_issues.append("Organização não associada.")
    if any(mapping["mapping_status"] == "pending_review" for mapping in mappings):
        review_issues.append("Existe pelo menos um mapping pendente de revisão.")
    monitoring_count = session.scalar(select(func.count(MonitoringObservation.id)).where(MonitoringObservation.asset_id == asset.id)) or 0
    production_count = session.scalar(select(func.count(ProductionFact.id)).where(ProductionFact.asset_id == asset.id)) or 0
    return {
        "asset": asset,
        "organization_name": organization_name,
        "aliases": aliases,
        "mappings": mappings,
        "review_issues": review_issues,
        "has_monitoring": monitoring_count > 0,
        "has_production": production_count > 0,
    }


def reconciliation_data(session: Session) -> dict[str, int]:
    return review_summary(session)


def commercial_panel_data(session: Session, *, asset_id: int, on: date | None = None) -> dict[str, Any]:
    """Everything the asset page needs to show and edit the commercial inputs."""
    today = on or utc_now().date()
    models = list(
        session.scalars(
            select(FinancialModel)
            .where(FinancialModel.asset_id == asset_id)
            .order_by(FinancialModel.version.desc())
        )
    )
    return {
        "financial_models": models,
        "confirmed_model": next((model for model in models if model.status == "confirmed"), None),
        "tariff": resolve_tariff(session, asset_id=asset_id, on=today),
        "billing": resolve_billing_config(session, asset_id=asset_id, on=today),
        "tariff_types": TARIFF_TYPES,
        "report_types": REPORT_TYPES,
        "billing_modes": BILLING_MODES,
        "billing_energy_bases": BILLING_ENERGY_BASES,
    }
