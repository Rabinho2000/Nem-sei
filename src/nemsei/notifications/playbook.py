"""Suggested operational action -- deterministic lookup, no AI.

Req 7: "A acção deve ser contextual e curta. Não criar uma AI complexa para
isto; regras determinísticas são suficientes inicialmente." One function,
one table, keyed by `problem_family` first (what kind of problem) and
`rule_code` second (for the cases where the generic family text is not
specific enough) -- never a session, never a network call.
"""
from __future__ import annotations

# Family-level default: what to do about "a plant lost communication" is the
# same first step whatever the exact rule_code that flagged it.
_ACTION_BY_FAMILY = {
    "communication": (
        "Verificar provider.\n"
        "Confirmar se toda a instalação está sem dados.\n"
        "Se persistir, contactar cliente/local para confirmar alimentação e comunicações."
    ),
    "fault": (
        "Consultar código de erro.\n"
        "Identificar equipamento.\n"
        "Efetuar diagnóstico remoto."
    ),
    "coverage": (
        "Sem avaria confirmada -- cobertura de monitorização insuficiente.\n"
        "Confirmar ligação do dispositivo/leitura na próxima visita de rotina."
    ),
}

# rule_code-specific overrides, only where the family default is not precise
# enough to act on immediately. The four production-shortfall rule_codes
# classify as `fault` in `diagnostics.incident_categories` (a confirmed
# production loss is a real fault, not a fourth family) but "consultar
# código de erro" is the wrong first step for them -- there is no error
# code, there is a peer comparison. Kept here, at the rule_code level, not
# by inventing a family the classification module does not have.
_PRODUCTION_ACTION = (
    "Comparar com os inversores vizinhos.\n"
    "Confirmar se a perda de produção é real ou uma leitura isolada.\n"
    "Se persistir, agendar diagnóstico remoto."
)

_ACTION_BY_RULE_CODE = {
    "plant_offline": _ACTION_BY_FAMILY["communication"],
    "plant_fault": (
        "Consultar código de erro reportado pelo provider.\n"
        "Identificar equipamento afectado.\n"
        "Efetuar diagnóstico remoto."
    ),
    "device_unavailable": _ACTION_BY_FAMILY["fault"],
    "zero_power_while_peers_active": _PRODUCTION_ACTION,
    "power_disparity_among_peers": _PRODUCTION_ACTION,
    "daily_energy_disparity_among_peers": _PRODUCTION_ACTION,
    "zero_production_in_productive_window": _PRODUCTION_ACTION,
}

# A meter offline at an ESCO installation is not a generic fault -- billing
# and self-consumption depend on it directly. Applied on top of the family/
# rule_code text, not instead of it, when the caller says this episode is
# both a meter problem and an ESCO installation.
_ESCO_METER_ACTION = (
    "Prioridade elevada: meter necessário para consumo/autoconsumo/faturação.\n"
    "Confirmar leitura do meter antes de qualquer outra ação."
)


def suggested_action(*, problem_family: str, rule_code: str, is_esco: bool = False, is_meter: bool = False) -> str:
    if is_esco and is_meter:
        return _ESCO_METER_ACTION
    return _ACTION_BY_RULE_CODE.get(rule_code) or _ACTION_BY_FAMILY.get(problem_family, "Diagnóstico manual necessário.")
