"""Provider-neutral activation/readiness checks with no network access."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from nemsei.assets.models import Asset
from nemsei.providers.models import AssetProviderMapping, ProviderConnection
from nemsei.providers.registry import (
    ImplementationSupport,
    ProviderCapability,
    ProviderCode,
    RuntimeAvailability,
    evaluate_capability,
)
from nemsei.providers.repository import ProviderRepository
from nemsei.sources.repository import SourcePolicyRepository
from nemsei.sources.service import source_policy_date_for_asset
from nemsei.sync.models import IntegrationHealth


@dataclass(frozen=True)
class PreflightFinding:
    code: str
    message: str
    blocking: bool = True


@dataclass(frozen=True)
class ActivationPreflight:
    asset_id: int | None
    connection_id: int | None
    mapping_id: int | None
    capability: str
    implementation_support: str
    runtime_availability: str
    checks: dict[str, bool]
    findings: tuple[PreflightFinding, ...]

    @property
    def blocking_findings(self) -> tuple[PreflightFinding, ...]:
        return tuple(item for item in self.findings if item.blocking)

    @property
    def warnings(self) -> tuple[PreflightFinding, ...]:
        return tuple(item for item in self.findings if not item.blocking)

    @property
    def ready(self) -> bool:
        return not self.blocking_findings


def _finding(code: str, message: str, *, blocking: bool = True) -> PreflightFinding:
    return PreflightFinding(code, message, blocking)


def activation_preflight(
    session: Session,
    *,
    settings: Any,
    mapping_id: int,
    capability: ProviderCapability | str,
    on_date: date | None = None,
) -> ActivationPreflight:
    requested = ProviderCapability(capability)
    mapping = session.get(AssetProviderMapping, mapping_id)
    if mapping is None:
        return ActivationPreflight(
            None,
            None,
            mapping_id,
            requested.value,
            ImplementationSupport.UNSUPPORTED.value,
            RuntimeAvailability.UNKNOWN.value,
            {},
            (_finding("mapping_missing", "Mapping não existe."),),
        )

    asset = session.get(Asset, mapping.asset_id)
    connection = session.get(ProviderConnection, mapping.provider_connection_id)
    findings: list[PreflightFinding] = []
    checks: dict[str, bool] = {}
    if asset is None:
        findings.append(_finding("asset_missing", "A central canónica não existe."))
    else:
        checks["asset_exists"] = True
        if asset.review_status == "needs_review":
            findings.append(_finding("asset_needs_review", "A identidade da central ainda requer revisão."))
        if not asset.timezone:
            if requested is ProviderCapability.PRODUCTION_HISTORY:
                findings.append(_finding("timezone_missing", "Produção requer um fuso horário IANA explícito."))
        else:
            try:
                ZoneInfo(asset.timezone)
                checks["timezone_valid"] = True
            except ZoneInfoNotFoundError:
                findings.append(_finding("timezone_invalid", "O fuso horário da central não é um identificador IANA válido."))

    if connection is None:
        findings.append(_finding("connection_missing", "A ligação provider não existe."))
        return ActivationPreflight(
            mapping.asset_id,
            None,
            mapping.id,
            requested.value,
            ImplementationSupport.UNSUPPORTED.value,
            RuntimeAvailability.UNKNOWN.value,
            checks,
            tuple(findings),
        )

    checks["connection_exists"] = True
    configured = connection.configuration_status == "configured" and bool(connection.credential_reference)
    health = session.get(IntegrationHealth, connection.id)
    provider_temporarily_unavailable = bool(health and health.provider_state == "unavailable")
    runtime_known = bool(connection.enabled and getattr(settings, "capabilities", {}).get("provider_reads", False))
    status = evaluate_capability(
        connection.provider_code,
        requested,
        connection_configured=configured,
        integration_temporarily_unavailable=provider_temporarily_unavailable,
        runtime_known=runtime_known,
    )
    if status.implementation_support is ImplementationSupport.UNSUPPORTED:
        findings.append(_finding("capability_unsupported", "Este provider não suporta esta capacidade no V2."))
    if not configured:
        findings.append(_finding("credentials_not_configured", "A ligação não tem uma referência de credenciais configurada."))
    if not connection.enabled:
        findings.append(_finding("connection_disabled", "A ligação provider está desativada."))
    if not getattr(settings, "capabilities", {}).get("provider_reads", False):
        findings.append(_finding("provider_reads_disabled", "A política global provider_reads está desativada."))
    if mapping.mapping_status != "active":
        findings.append(_finding("mapping_not_approved", "O mapping ainda não foi aprovado explicitamente."))
    if not mapping.external_id.strip():
        findings.append(_finding("external_id_missing", "O identificador externo é obrigatório."))

    conflict = ProviderRepository(session).active_external_claim(
        connection_id=connection.id,
        normalized_external_id=mapping.normalized_external_id,
    )
    if conflict is not None and conflict.id != mapping.id:
        findings.append(_finding("mapping_conflict", "Existe outra reivindicação ativa para este identificador provider."))

    target_date = on_date
    if target_date is None:
        try:
            target_date = source_policy_date_for_asset(session, asset_id=mapping.asset_id)
        except ValueError as exc:
            if requested in {ProviderCapability.CURRENT_MONITORING, ProviderCapability.PRODUCTION_HISTORY}:
                findings.append(_finding("policy_date_unavailable", str(exc)))
            target_date = date.today()

    policies = SourcePolicyRepository(session).active_for(
        asset_id=mapping.asset_id,
        source_use="monitoring",
        on_date=target_date,
    )
    primary_policies = [policy for policy in policies if not policy.is_fallback]
    if primary_policies:
        top_priority = primary_policies[0].priority
        if sum(policy.priority == top_priority for policy in primary_policies) > 1:
            findings.append(_finding("source_policy_conflict", "Existem fontes primárias concorrentes para este período."))
    if requested is ProviderCapability.CURRENT_MONITORING and not any(
        policy.provider_mapping_id == mapping.id and not policy.is_fallback for policy in policies
    ):
        findings.append(_finding("monitoring_source_policy_missing", "Não existe uma política primária de monitorização válida para este mapping."))

    if requested is ProviderCapability.PRODUCTION_HISTORY:
        production_policies = SourcePolicyRepository(session).active_for(
            asset_id=mapping.asset_id,
            source_use="production",
            on_date=target_date,
        )
        production_primary = [policy for policy in production_policies if not policy.is_fallback]
        if production_primary:
            top_priority = production_primary[0].priority
            if sum(policy.priority == top_priority for policy in production_primary) > 1:
                findings.append(_finding("production_source_policy_conflict", "Existem fontes primárias de produção concorrentes para este período."))
        if not any(policy.provider_mapping_id == mapping.id and not policy.is_fallback for policy in production_policies):
            findings.append(_finding("production_source_policy_missing", "Não existe uma política primária de produção válida para este mapping."))
        if connection.provider_code == ProviderCode.FUSIONSOLAR.value:
            from nemsei.integrations.fusionsolar.client import FusionSolarClientError
            from nemsei.integrations.fusionsolar.production import production_contract_for

            try:
                production_contract_for(connection)
            except FusionSolarClientError as exc:
                findings.append(_finding("production_contract_missing", exc.error.safe_message))

    if provider_temporarily_unavailable:
        findings.append(_finding("provider_temporarily_unavailable", "O último estado provider indica indisponibilidade temporária."))

    checks.update(
        {
            "connection_configured": configured,
            "connection_enabled": bool(connection.enabled),
            "provider_reads": bool(getattr(settings, "capabilities", {}).get("provider_reads", False)),
            "mapping_active": mapping.mapping_status == "active",
        }
    )
    return ActivationPreflight(
        mapping.asset_id,
        connection.id,
        mapping.id,
        requested.value,
        status.implementation_support.value,
        status.runtime_availability.value,
        checks,
        tuple(findings),
    )
