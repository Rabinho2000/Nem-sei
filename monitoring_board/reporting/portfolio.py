from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from monitoring_board.reporting.models import ReportingPeriod


ENGINE_VERSION = "portfolio-reporting-v1"


@dataclass(frozen=True)
class PortfolioMetricDefinition:
    key: str
    label: str
    description: str
    category: str
    unit: str
    value_type: str
    decimals: int
    aggregation: str
    total_allowed: bool = True
    comparison_allowed: bool = True
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class PortfolioReportColumn:
    metric_key: str
    label: str
    decimals: int | None = None
    visible: bool = True
    display_order: int = 0


@dataclass(frozen=True)
class PortfolioThreshold:
    metric_key: str
    operator: str
    value: Decimal
    severity: str


@dataclass(frozen=True)
class PortfolioReportProfile:
    id: int | None
    name: str
    description: str
    portfolio_id: int | None
    period_type: str
    columns: tuple[PortfolioReportColumn, ...]
    filters: dict[str, Any]
    sort: tuple[str, str] | None = None
    comparison: str = ""
    thresholds: tuple[PortfolioThreshold, ...] = ()
    compact: bool = False
    include_pending: bool = True
    include_inactive: bool = False
    warnings_only: bool = False


@dataclass(frozen=True)
class PortfolioReportRequest:
    portfolio_id: int
    period: ReportingPeriod
    profile_id: int | None = None
    comparison: str = ""
    filters: dict[str, Any] | None = None


@dataclass(frozen=True)
class PortfolioReportRow:
    asset_id: int | None
    values: dict[str, Any]
    warnings: tuple[str, ...] = ()
    severity: str = "ok"


@dataclass(frozen=True)
class PortfolioReportSummary:
    values: dict[str, Any]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class PortfolioComparisonResult:
    mode: str
    values: dict[str, dict[str, Any]]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PortfolioDataCoverage:
    global_pct: Decimal
    by_source: dict[str, Decimal]
    complete_installations: int
    incomplete_installations: int
    missing_months: tuple[str, ...]


@dataclass(frozen=True)
class PortfolioReportResult:
    portfolio_id: int
    portfolio_name: str
    profile: PortfolioReportProfile
    profile_version: int
    period: ReportingPeriod
    columns: tuple[PortfolioReportColumn, ...]
    rows: tuple[PortfolioReportRow, ...]
    summary: PortfolioReportSummary
    comparison: PortfolioComparisonResult | None
    coverage: PortfolioDataCoverage
    warnings: tuple[str, ...]
    metadata: dict[str, Any]
    engine_version: str
    generated_at: datetime


METRIC_CATALOG: dict[str, PortfolioMetricDefinition] = {
    "installation": PortfolioMetricDefinition("installation", "Instalacao", "Nome apresentado", "Identificacao", "", "text", 0, "none", False, False),
    "local_installation": PortfolioMetricDefinition("local_installation", "Nome local", "Nome interno", "Identificacao", "", "text", 0, "none", False, False),
    "nif": PortfolioMetricDefinition("nif", "NIF", "NIF externo/local", "Identificacao", "", "text", 0, "none", False, False),
    "sub_account": PortfolioMetricDefinition("sub_account", "Subconta", "Subconta", "Identificacao", "", "text", 0, "none", False, False),
    "installed_power_kwp": PortfolioMetricDefinition("installed_power_kwp", "Potencia kWp", "Potencia instalada", "Identificacao", "kWp", "number", 2, "sum"),
    "mapping_confidence": PortfolioMetricDefinition("mapping_confidence", "Confianca mapping", "Confianca do mapping", "Qualidade dos dados", "", "number", 2, "none", False),
    "actual_production_kwh": PortfolioMetricDefinition("actual_production_kwh", "Producao real", "Producao real", "Producao", "kWh", "number", 2, "sum"),
    "helioscope_expected_kwh": PortfolioMetricDefinition("helioscope_expected_kwh", "Helioscope", "Producao esperada", "Producao", "kWh", "number", 2, "sum"),
    "expected_production_kwh": PortfolioMetricDefinition("expected_production_kwh", "Producao prevista", "Producao prevista base", "Previsao", "kWh", "number", 2, "sum"),
    "expected_consumption_kwh": PortfolioMetricDefinition("expected_consumption_kwh", "Consumo previsto", "Consumo previsto", "Previsao", "kWh", "number", 2, "sum"),
    "expected_self_use_kwh": PortfolioMetricDefinition("expected_self_use_kwh", "Autoconsumo previsto", "Autoconsumo previsto", "Previsao", "kWh", "number", 2, "sum"),
    "expected_export_kwh": PortfolioMetricDefinition("expected_export_kwh", "Excedente previsto", "Excedente previsto", "Previsao", "kWh", "number", 2, "sum"),
    "expected_grid_import_kwh": PortfolioMetricDefinition("expected_grid_import_kwh", "Importacao prevista", "Importacao da rede prevista", "Previsao", "kWh", "number", 2, "sum"),
    "expected_self_consumption_rate_pct": PortfolioMetricDefinition("expected_self_consumption_rate_pct", "Taxa AC prevista", "Taxa de autoconsumo prevista", "Previsao", "%", "number", 2, "recalculate"),
    "expected_self_sufficiency_rate_pct": PortfolioMetricDefinition("expected_self_sufficiency_rate_pct", "Taxa AS prevista", "Taxa de autossuficiencia prevista", "Previsao", "%", "number", 2, "recalculate"),
    "expected_specific_yield": PortfolioMetricDefinition("expected_specific_yield", "Specific yield previsto", "Producao prevista por kWp", "Previsao", "kWh/kWp", "number", 2, "recalculate"),
    "expected_production_source": PortfolioMetricDefinition("expected_production_source", "Origem prevista", "Origem da producao prevista", "Previsao", "", "text", 0, "none", False, False),
    "adjusted_expected_kwh": PortfolioMetricDefinition("adjusted_expected_kwh", "Esperada ajustada", "Producao esperada ajustada", "Performance", "kWh", "number", 2, "sum"),
    "deviation_kwh": PortfolioMetricDefinition("deviation_kwh", "Desvio", "Desvio de producao", "Performance", "kWh", "number", 2, "recalculate"),
    "deviation_pct": PortfolioMetricDefinition("deviation_pct", "Desvio %", "Desvio percentual", "Performance", "%", "number", 2, "recalculate"),
    "specific_yield": PortfolioMetricDefinition("specific_yield", "Specific yield", "Producao por kWp", "Performance", "kWh/kWp", "number", 2, "recalculate"),
    "availability_pct": PortfolioMetricDefinition("availability_pct", "Disponibilidade", "Disponibilidade ponderada", "Disponibilidade", "%", "number", 2, "weighted_avg"),
    "self_use_kwh": PortfolioMetricDefinition("self_use_kwh", "Autoconsumo", "Energia autoconsumida", "Autoconsumo", "kWh", "number", 2, "sum"),
    "export_kwh": PortfolioMetricDefinition("export_kwh", "Excedente", "Energia exportada", "Rede", "kWh", "number", 2, "sum"),
    "consumption_kwh": PortfolioMetricDefinition("consumption_kwh", "Consumo", "Consumo", "Rede", "kWh", "number", 2, "sum"),
    "grid_import_kwh": PortfolioMetricDefinition("grid_import_kwh", "Importacao rede", "Importacao da rede", "Rede", "kWh", "number", 2, "sum"),
    "self_consumption_rate_pct": PortfolioMetricDefinition("self_consumption_rate_pct", "Taxa autoconsumo", "Autoconsumo / producao", "Autoconsumo", "%", "number", 2, "recalculate"),
    "self_sufficiency_rate_pct": PortfolioMetricDefinition("self_sufficiency_rate_pct", "Taxa autossuficiencia", "Autoconsumo / consumo", "Rede", "%", "number", 2, "recalculate"),
    "estimated_value_eur": PortfolioMetricDefinition("estimated_value_eur", "Valor autoconsumo", "Valor estimado", "Financeiro", "EUR", "money", 2, "sum"),
    "export_revenue_eur": PortfolioMetricDefinition("export_revenue_eur", "Receita excedente", "Receita exportacao", "Financeiro", "EUR", "money", 2, "sum"),
    "esco_payment_eur": PortfolioMetricDefinition("esco_payment_eur", "Pagamento ESCO", "Pagamento ESCO", "Financeiro", "EUR", "money", 2, "sum"),
    "fixed_fee_eur": PortfolioMetricDefinition("fixed_fee_eur", "Mensalidade fixa", "Fee fixa", "Financeiro", "EUR", "money", 2, "sum"),
    "net_benefit_eur": PortfolioMetricDefinition("net_benefit_eur", "Beneficio liquido", "Beneficio liquido", "Financeiro", "EUR", "money", 2, "sum"),
    "invoice_status": PortfolioMetricDefinition("invoice_status", "Fatura", "Estado da fatura", "Qualidade dos dados", "", "text", 0, "none", False, False),
    "tariff_type": PortfolioMetricDefinition("tariff_type", "Tarifa", "Tipo de tarifa", "Configuracao", "", "text", 0, "none", False, False),
    "data_status": PortfolioMetricDefinition("data_status", "Estado", "Estado dos dados", "Qualidade dos dados", "", "text", 0, "none", False, False),
    "coverage_pct": PortfolioMetricDefinition("coverage_pct", "Cobertura", "Cobertura por instalacao", "Qualidade dos dados", "%", "number", 2, "weighted_avg"),
    "warning_count": PortfolioMetricDefinition("warning_count", "Warnings", "Numero de warnings", "Qualidade dos dados", "", "number", 0, "sum"),
    "warning_labels": PortfolioMetricDefinition("warning_labels", "Avisos", "Avisos", "Qualidade dos dados", "", "list", 0, "none", False, False),
}


DEFAULT_PROFILE_COLUMNS = {
    "Resumo operacional": ("installation", "actual_production_kwh", "adjusted_expected_kwh", "deviation_pct", "availability_pct", "estimated_value_eur", "data_status"),
    "Performance": ("installation", "installed_power_kwp", "actual_production_kwh", "specific_yield", "adjusted_expected_kwh", "deviation_kwh", "deviation_pct", "availability_pct"),
    "Financeiro": ("installation", "self_use_kwh", "export_kwh", "estimated_value_eur", "export_revenue_eur", "esco_payment_eur", "net_benefit_eur"),
    "Qualidade dos dados": ("installation", "mapping_confidence", "coverage_pct", "invoice_status", "tariff_type", "warning_count", "warning_labels"),
    "Completo": tuple(METRIC_CATALOG.keys()),
}


def default_profile(name: str = "Completo", *, portfolio_id: int | None = None) -> PortfolioReportProfile:
    columns = DEFAULT_PROFILE_COLUMNS.get(name, DEFAULT_PROFILE_COLUMNS["Completo"])
    return PortfolioReportProfile(
        id=None,
        name=name,
        description=f"Perfil {name}",
        portfolio_id=portfolio_id,
        period_type="monthly",
        columns=tuple(PortfolioReportColumn(metric_key=key, label=METRIC_CATALOG[key].label, decimals=METRIC_CATALOG[key].decimals, display_order=index * 10) for index, key in enumerate(columns, start=1)),
        filters={},
    )


def validate_profile(profile: PortfolioReportProfile) -> PortfolioReportProfile:
    if not profile.name.strip():
        raise ValueError("profile_name_required")
    if len(profile.name.strip()) > 120:
        raise ValueError("profile_name_too_long")
    if profile.period_type not in {"monthly", "quarterly", "semiannual", "annual"}:
        raise ValueError("invalid_profile_period_type")
    if profile.comparison not in {"", "previous_period", "previous_year"}:
        raise ValueError("invalid_profile_comparison")
    metric_keys = [column.metric_key for column in profile.columns if column.visible]
    if len(metric_keys) != len(set(metric_keys)):
        raise ValueError("duplicate_profile_metrics")
    invalid = [key for key in metric_keys if key not in METRIC_CATALOG]
    if invalid:
        raise ValueError("invalid_profile_metrics:" + ",".join(invalid))
    for column in profile.columns:
        if column.decimals is not None and not 0 <= int(column.decimals) <= 6:
            raise ValueError("invalid_profile_decimals")
        if len(column.label or "") > 80:
            raise ValueError("profile_label_too_long")
    if profile.sort:
        key, direction = profile.sort
        if key not in METRIC_CATALOG or direction.lower() not in {"asc", "desc"}:
            raise ValueError("invalid_profile_sort")
    allowed_filters = {"warnings_only"}
    if set(profile.filters) - allowed_filters:
        raise ValueError("invalid_profile_filters")
    for threshold in profile.thresholds:
        if threshold.metric_key not in METRIC_CATALOG:
            raise ValueError("invalid_threshold_metric")
        if threshold.operator not in {"<", "<=", ">", ">=", "=="}:
            raise ValueError("invalid_threshold_operator")
        if threshold.severity not in {"info", "warning", "critical", "missing"}:
            raise ValueError("invalid_threshold_severity")
        if not threshold.value.is_finite():
            raise ValueError("invalid_threshold_value")
    return profile


def aggregate_rows(rows: tuple[PortfolioReportRow, ...], columns: tuple[PortfolioReportColumn, ...]) -> PortfolioReportSummary:
    values: dict[str, Any] = {}
    warnings = sorted({warning for row in rows for warning in row.warnings})
    for column in columns:
        definition = METRIC_CATALOG[column.metric_key]
        if not definition.total_allowed:
            continue
        metric_values = [row.values.get(column.metric_key) for row in rows if row.values.get(column.metric_key) is not None]
        if definition.aggregation == "sum":
            values[column.metric_key] = _round(sum(Decimal(str(value)) for value in metric_values), definition.decimals) if metric_values else None
        elif definition.aggregation == "weighted_avg":
            values[column.metric_key] = weighted_average(rows, column.metric_key, "installed_power_kwp", definition.decimals)
        elif definition.aggregation == "recalculate":
            values[column.metric_key] = recalculate_metric(rows, column.metric_key, definition.decimals)
    return PortfolioReportSummary(values=values, warnings=tuple(warnings))


def weighted_average(rows: tuple[PortfolioReportRow, ...], metric_key: str, weight_key: str, decimals: int) -> Decimal | None:
    weighted = Decimal("0")
    weights = Decimal("0")
    for row in rows:
        value = row.values.get(metric_key)
        weight = row.values.get(weight_key) or 0
        if value is None or Decimal(str(weight)) <= 0:
            continue
        weighted += Decimal(str(value)) * Decimal(str(weight))
        weights += Decimal(str(weight))
    return _round(weighted / weights, decimals) if weights else None


def recalculate_metric(rows: tuple[PortfolioReportRow, ...], metric_key: str, decimals: int) -> Decimal | None:
    def total(key: str) -> Decimal:
        return sum((Decimal(str(row.values.get(key))) for row in rows if row.values.get(key) is not None), Decimal("0"))

    actual = total("actual_production_kwh")
    adjusted = total("adjusted_expected_kwh")
    installed = total("installed_power_kwp")
    self_use = total("self_use_kwh")
    exported = total("export_kwh")
    consumption = total("consumption_kwh")
    if metric_key == "deviation_kwh":
        return _round(actual - adjusted, decimals) if adjusted or actual else None
    if metric_key == "deviation_pct":
        return _round((actual - adjusted) / adjusted * Decimal("100"), decimals) if adjusted else None
    if metric_key == "specific_yield":
        return _round(actual / installed, decimals) if installed else None
    if metric_key == "self_consumption_rate_pct":
        production = self_use + exported
        return _round(self_use / production * Decimal("100"), decimals) if production else None
    if metric_key == "self_sufficiency_rate_pct":
        return _round(self_use / consumption * Decimal("100"), decimals) if consumption else None
    return None


def apply_filters(rows: tuple[PortfolioReportRow, ...], profile: PortfolioReportProfile) -> tuple[PortfolioReportRow, ...]:
    filtered = rows
    if profile.warnings_only or profile.filters.get("warnings_only"):
        filtered = tuple(row for row in filtered if row.warnings)
    if not profile.include_pending:
        filtered = tuple(row for row in filtered if row.values.get("asset_id") is not None)
    return filtered


def apply_sort(rows: tuple[PortfolioReportRow, ...], profile: PortfolioReportProfile) -> tuple[PortfolioReportRow, ...]:
    if not profile.sort:
        return rows
    key, direction = profile.sort
    reverse = direction.lower() == "desc"
    return tuple(sorted(rows, key=lambda row: (row.values.get(key) is None, row.values.get(key)), reverse=reverse))


def evaluate_thresholds(row: PortfolioReportRow, thresholds: tuple[PortfolioThreshold, ...]) -> str:
    severity_rank = {"ok": 0, "info": 1, "warning": 2, "critical": 3, "missing": 4}
    severity = "ok"
    for threshold in thresholds:
        value = row.values.get(threshold.metric_key)
        if value is None:
            if threshold.severity == "missing":
                severity = max(severity, threshold.severity, key=lambda item: severity_rank[item])
            continue
        if _compare(Decimal(str(value)), threshold.operator, threshold.value):
            severity = max(severity, threshold.severity, key=lambda item: severity_rank[item])
    return severity


def comparison_values(current: PortfolioReportSummary, previous: PortfolioReportSummary, mode: str) -> PortfolioComparisonResult:
    values: dict[str, dict[str, Any]] = {}
    for key, current_value in current.values.items():
        previous_value = previous.values.get(key)
        if current_value is None or previous_value is None:
            values[key] = {"current": current_value, "previous": previous_value, "delta": None, "delta_pct": None}
            continue
        current_decimal = Decimal(str(current_value))
        previous_decimal = Decimal(str(previous_value))
        delta = current_decimal - previous_decimal
        values[key] = {
            "current": current_value,
            "previous": previous_value,
            "delta": delta,
            "delta_pct": None if previous_decimal == 0 else _round(delta / previous_decimal * Decimal("100"), 2),
        }
    warnings = ("comparison_data_missing",) if any(item["delta"] is None for item in values.values()) else ()
    return PortfolioComparisonResult(mode=mode, values=values, warnings=warnings)


def data_coverage(rows: tuple[PortfolioReportRow, ...], months: tuple[date, ...]) -> PortfolioDataCoverage:
    sources = ("production", "helioscope", "availability", "tariff", "self_use", "invoice", "mapping")
    by_source: dict[str, Decimal] = {}
    expected_months = len(months) or 1
    expected_slots = len(rows) * expected_months
    for source in sources:
        complete = sum(int((row.values.get("_source_slots") or {}).get(source, 0)) for row in rows)
        by_source[source] = _round(Decimal(complete) / Decimal(expected_slots) * Decimal("100"), 2) if expected_slots else Decimal("0.00")
    complete_rows = sum(1 for row in rows if not row.values.get("missing_sources"))
    global_pct = _round(sum(by_source.values(), Decimal("0")) / Decimal(len(sources)), 2)
    missing_months = tuple(sorted({month for row in rows for month in row.values.get("missing_months", ())}))
    return PortfolioDataCoverage(
        global_pct=global_pct,
        by_source=by_source,
        complete_installations=complete_rows,
        incomplete_installations=len(rows) - complete_rows,
        missing_months=missing_months or tuple(month.isoformat() for month in months if len(months) > 1 and not rows),
    )


def result_to_dict(result: PortfolioReportResult) -> dict[str, Any]:
    return {
        "portfolio_id": result.portfolio_id,
        "portfolio_name": result.portfolio_name,
        "profile": profile_to_config(result.profile),
        "profile_version": result.profile_version,
        "period": {
            "type": result.period.period_type.value,
            "start": result.period.start.isoformat(),
            "end": result.period.end.isoformat(),
            "label": result.period.label,
            "months": [month.isoformat() for month in result.period.included_months],
        },
        "columns": [column.__dict__ for column in result.columns],
        "rows": [{"asset_id": row.asset_id, "values": row.values, "warnings": list(row.warnings), "severity": row.severity} for row in result.rows],
        "summary": {"values": result.summary.values, "warnings": list(result.summary.warnings)},
        "comparison": None if result.comparison is None else {"mode": result.comparison.mode, "values": result.comparison.values, "warnings": list(result.comparison.warnings)},
        "coverage": {
            "global_pct": str(result.coverage.global_pct),
            "by_source": {key: str(value) for key, value in result.coverage.by_source.items()},
            "complete_installations": result.coverage.complete_installations,
            "incomplete_installations": result.coverage.incomplete_installations,
            "missing_months": list(result.coverage.missing_months),
        },
        "warnings": list(result.warnings),
        "metadata": result.metadata,
        "engine_version": result.engine_version,
        "generated_at": result.generated_at.isoformat(timespec="seconds"),
    }


def profile_to_config(profile: PortfolioReportProfile) -> dict[str, Any]:
    return {
        "name": profile.name,
        "description": profile.description,
        "portfolio_id": profile.portfolio_id,
        "period_type": profile.period_type,
        "columns": [column.__dict__ for column in profile.columns],
        "filters": profile.filters,
        "sort": profile.sort,
        "comparison": profile.comparison,
        "thresholds": [{"metric_key": item.metric_key, "operator": item.operator, "value": str(item.value), "severity": item.severity} for item in profile.thresholds],
        "compact": profile.compact,
        "include_pending": profile.include_pending,
        "include_inactive": profile.include_inactive,
        "warnings_only": profile.warnings_only,
    }


def profile_from_config(config: dict[str, Any], *, profile_id: int | None = None, portfolio_id: int | None = None) -> PortfolioReportProfile:
    columns = tuple(
        PortfolioReportColumn(
            metric_key=str(item["metric_key"]),
            label=str(item.get("label") or METRIC_CATALOG[str(item["metric_key"])].label),
            decimals=int(item["decimals"]) if str(item.get("decimals", "")).strip().isdigit() else None,
            visible=bool(item.get("visible", True)),
            display_order=int(item.get("display_order") or index * 10),
        )
        for index, item in enumerate(config.get("columns", []), start=1)
        if str(item.get("metric_key")) in METRIC_CATALOG
    )
    thresholds = tuple(
        PortfolioThreshold(metric_key=str(item["metric_key"]), operator=str(item["operator"]), value=Decimal(str(item["value"])), severity=str(item["severity"]))
        for item in config.get("thresholds", [])
        if str(item.get("metric_key")) in METRIC_CATALOG
    )
    return validate_profile(
        PortfolioReportProfile(
            id=profile_id,
            name=str(config.get("name") or "Completo"),
            description=str(config.get("description") or ""),
            portfolio_id=portfolio_id if portfolio_id is not None else config.get("portfolio_id"),
            period_type=str(config.get("period_type") or "monthly"),
            columns=columns or default_profile(str(config.get("name") or "Completo")).columns,
            filters=dict(config.get("filters") or {}),
            sort=tuple(config["sort"]) if config.get("sort") else None,
            comparison=str(config.get("comparison") or ""),
            thresholds=thresholds,
            compact=bool(config.get("compact", False)),
            include_pending=bool(config.get("include_pending", True)),
            include_inactive=bool(config.get("include_inactive", False)),
            warnings_only=bool(config.get("warnings_only", False)),
        )
    )


def _compare(left: Decimal, operator: str, right: Decimal) -> bool:
    if operator == "<":
        return left < right
    if operator == "<=":
        return left <= right
    if operator == ">":
        return left > right
    if operator == ">=":
        return left >= right
    if operator == "==":
        return left == right
    raise ValueError("invalid_threshold_operator")


def _round(value: Decimal, decimals: int) -> Decimal:
    return value.quantize(Decimal("1") if decimals <= 0 else Decimal("1." + ("0" * decimals)))
