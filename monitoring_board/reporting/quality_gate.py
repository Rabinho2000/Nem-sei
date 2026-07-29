from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable


READY = "ready"
WARNING = "warning"
BLOCKED = "blocked"


@dataclass(frozen=True)
class QualityFinding:
    code: str
    severity: str
    scope: str
    asset_id: int | None
    message: str
    source: str
    remediation: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QualityGateResult:
    status: str
    findings: tuple[QualityFinding, ...]

    @property
    def blockers(self) -> tuple[QualityFinding, ...]:
        return tuple(item for item in self.findings if item.severity == BLOCKED)

    @property
    def warnings(self) -> tuple[QualityFinding, ...]:
        return tuple(item for item in self.findings if item.severity == WARNING)


def evaluate_report_quality(
    payload: dict[str, Any],
    *,
    scope: str,
    requires_financials: bool = False,
    requires_availability: bool = False,
    requires_customer: bool = False,
) -> QualityGateResult:
    rows = report_rows(payload)
    findings: list[QualityFinding] = []
    for row in rows:
        asset_id = int(row.get("asset_id") or 0) or None
        mapping = str(row.get("mapping_status") or "")
        if mapping in {"mapping_pending", "mapping_conflict", "unmapped"} or (
            scope == "portfolio" and not asset_id
        ):
            findings.append(
                finding(
                    f"mapping_{'conflict' if mapping == 'mapping_conflict' else 'pending'}",
                    BLOCKED,
                    scope,
                    asset_id,
                    "O mapping da instalação não está resolvido.",
                    "portfolio_mapping",
                    "Resolver o mapping e criar um novo snapshot.",
                )
            )
        production_status = str(
            row.get("production_quality_status")
            or payload.get("production_quality_status")
            or ""
        )
        if production_status in {"missing", "partial", "conflict", "in_progress"}:
            findings.append(
                finding(
                    f"production_{production_status}",
                    BLOCKED,
                    scope,
                    asset_id,
                    "A produção do mês fechado não está completa.",
                    str(row.get("production_source") or "production_records"),
                    "Completar/reconciliar a produção e criar um novo snapshot.",
                )
            )
        provider = str(
            row.get("energy_provider")
            or row.get("production_provider")
            or payload.get("energy_provider")
            or ""
        )
        if not provider:
            findings.append(
                finding(
                    "missing_energy_source",
                    BLOCKED,
                    scope,
                    asset_id,
                    "Não existe uma fonte energética identificada.",
                    "energy_source",
                    "Configurar uma fonte primária válida para o período.",
                )
            )
        if provider.casefold() == "sigenergy":
            history_status = str(
                row.get("sigenergy_history_status")
                or payload.get("sigenergy_history_status")
                or ""
            )
            unit_confirmed = bool(
                row.get("sigenergy_energy_unit_confirmed")
                or payload.get("sigenergy_energy_unit_confirmed")
            )
            if history_status in {"backfill_incomplete", "month_unusable"}:
                findings.append(
                    finding(
                        "sigenergy_history_incomplete",
                        BLOCKED,
                        scope,
                        asset_id,
                        "O histórico Sigenergy ainda não cobre um mês utilizável.",
                        "sigenergy_history",
                        "Executar o backfill normal e validar a cobertura mensal completa.",
                    )
                )
            if not unit_confirmed:
                findings.append(
                    finding(
                        "sigenergy_energy_unit_unconfirmed",
                        BLOCKED,
                        scope,
                        asset_id,
                        "A unidade da energia histórica Sigenergy não está confirmada.",
                        "sigenergy_history",
                        "Confirmar externamente a unidade antes de usar o mês.",
                    )
                )
        if row.get("capacity_ambiguous") or "ambiguous_installed_power" in set(
            row.get("warnings") or ()
        ):
            findings.append(
                finding(
                    "ambiguous_installed_power",
                    BLOCKED,
                    scope,
                    asset_id,
                    "A potência instalada aplicável ao período é ambígua.",
                    "asset_capacity_periods",
                    "Corrigir os períodos de potência e criar um novo snapshot.",
                )
            )
        if requires_customer and not (
            row.get("customer_id") or row.get("customer_name") or payload.get("customer_id")
        ):
            findings.append(
                finding(
                    "missing_customer",
                    BLOCKED,
                    scope,
                    asset_id,
                    "A instalação não está associada a um cliente.",
                    "customers",
                    "Associar a instalação ao cliente.",
                )
            )
        if requires_financials:
            for code, present in (
                ("missing_billing_config", row.get("billing_config_valid", payload.get("billing_config_valid"))),
                ("missing_tariff", row.get("tariff_valid", payload.get("tariff_valid"))),
            ):
                if not present:
                    findings.append(
                        finding(
                            code,
                            BLOCKED,
                            scope,
                            asset_id,
                            "Faltam dados financeiros obrigatórios para o template.",
                            "billing",
                            "Corrigir billing/tarifa e criar um novo snapshot.",
                        )
                    )
        if not row.get("availability_pct") and row.get("availability_pct") != 0:
            findings.append(
                finding(
                    "missing_availability",
                    BLOCKED if requires_availability else WARNING,
                    scope,
                    asset_id,
                    "A disponibilidade não está disponível.",
                    "availability",
                    "Recolher disponibilidade ou remover a secção obrigatória.",
                )
            )
        if not row.get("invoice_status") or row.get("invoice_status") == "missing_invoice":
            findings.append(
                finding(
                    "missing_invoice",
                    WARNING,
                    scope,
                    asset_id,
                    "Não existe fatura confirmada para o período.",
                    "invoice_documents",
                    "Carregar/rever a fatura se for necessária.",
                )
            )
    findings = deduplicate_findings(findings)
    status = BLOCKED if any(item.severity == BLOCKED for item in findings) else (
        WARNING if findings else READY
    )
    return QualityGateResult(status, tuple(findings))


def report_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("rows")
    if isinstance(rows, list):
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            values = row.get("values")
            if isinstance(values, dict):
                normalized.append(
                    {
                        **values,
                        "asset_id": row.get("asset_id") or values.get("asset_id"),
                        "warnings": row.get("warnings") or values.get("warnings") or (),
                    }
                )
            else:
                normalized.append(row)
        return normalized
    return [payload]


def finding(
    code: str,
    severity: str,
    scope: str,
    asset_id: int | None,
    message: str,
    source: str,
    remediation: str,
) -> QualityFinding:
    return QualityFinding(code, severity, scope, asset_id, message, source, remediation)


def deduplicate_findings(findings: Iterable[QualityFinding]) -> list[QualityFinding]:
    unique: dict[tuple[str, str, int | None], QualityFinding] = {}
    for item in findings:
        unique[(item.code, item.scope, item.asset_id)] = item
    return sorted(
        unique.values(),
        key=lambda item: (item.severity != BLOCKED, item.code, item.asset_id or 0),
    )
