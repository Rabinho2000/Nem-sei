"""O ecrã de cobertura de produção: filtros e rótulos, nada de lógica.

A decisão de *porque* uma central não tem produção é toda de
`diagnostics.production_coverage`. Este módulo só escolhe quais das linhas
mostrar e traduz o estado para o que uma pessoa lê -- e é por isso que o
vocabulário está aqui e não no template: um estado novo no motor que não
tenha rótulo é uma falha de teste, não um código cru numa tabela.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from nemsei.diagnostics.production_coverage import (
    COVERAGE_STATES,
    STATE_OK,
    assess_production_coverage,
    coverage_summary,
)


# Rótulo, tom e -- separadamente -- se conta como problema. O tom é uma
# afirmação sobre urgência, não sobre gravidade: um limite de chamadas é um
# problema real mas resolve-se sozinho, e pintá-lo de vermelho ao lado de
# uma ligação sem mapping ensina o operador a ignorar vermelhos.
COVERAGE_STATE_LABELS: dict[str, tuple[str, str]] = {
    "ok": ("Com produção recente", "success"),
    "no_provider_mapping": ("Sem mapping", "danger"),
    "mapping_inactive": ("Mapping por aprovar", "warning"),
    "no_production_source_policy": ("Sem política de fonte", "danger"),
    "ambiguous_production_source_policy": ("Políticas em conflito", "danger"),
    "connection_disabled": ("Ligação desativada", "danger"),
    "credential_reference_missing": ("Sem credencial configurada", "danger"),
    "production_contract_missing": ("Sem contrato de produção verificado", "danger"),
    "scheduler_not_enabled_for_connection": ("Ligação sem agendamento", "warning"),
    "production_not_initialized": ("Produção não inicializada", "warning"),
    "production_cursor_missing": ("Bootstrap por correr", "warning"),
    "production_cursor_stale": ("Cursor atrasado demais", "danger"),
    "sync_failed": ("Sincronização falhou", "danger"),
    "rate_limited": ("Limite de chamadas", "warning"),
    "sync_deferred": ("Sincronização adiada", "muted"),
    "no_recent_fact": ("Sem factos recentes", "warning"),
    "unknown": ("Por apurar", "muted"),
}

UNLABELLED_COVERAGE_STATES = tuple(sorted(set(COVERAGE_STATES) - set(COVERAGE_STATE_LABELS)))


def coverage_state(state: str) -> dict[str, str]:
    label, tone = COVERAGE_STATE_LABELS.get(state, (state, "muted"))
    return {"state": state, "label": label, "tone": tone}


def production_coverage_page(
    session: Session,
    *,
    only_problems: bool = False,
    provider: str = "",
    connection: str = "",
    state: str = "",
) -> dict[str, Any]:
    """A tabela, o resumo e as opções de filtro.

    O resumo conta sempre a frota inteira, mesmo com filtros aplicados: o
    número no topo tem de responder "quantas centrais estão sem produção",
    não "quantas linhas estou a ver agora" -- que é a mesma confusão que
    fazia um parque parecer saudável por se estar a olhar para um filtro.

    Nenhuma acção é executada a partir daqui. É observabilidade: as coisas
    que consertam estes estados vivem em /mappings, /source-policies e
    /system, cada uma com a sua auditoria, e um botão aqui gastaria
    orçamento de chamadas da conta partilhada exactamente quando ela já
    está a recusar.
    """
    findings = assess_production_coverage(session)
    summary = coverage_summary(findings)

    rows = findings
    if only_problems:
        rows = [row for row in rows if row.state != STATE_OK]
    if provider:
        rows = [row for row in rows if (row.provider_code or "") == provider]
    if connection:
        rows = [row for row in rows if str(row.connection_id or "") == connection]
    if state:
        rows = [row for row in rows if row.state == state]

    # Pior primeiro, pela ordem em que o vocabulário os declara -- que é a
    # ordem em que a cadeia é percorrida, ou seja, do elo mais fundo para o
    # mais superficial. Uma central sem mapping vem antes de uma cuja
    # sincronização apenas foi adiada.
    order = {value: index for index, value in enumerate(COVERAGE_STATES)}
    rows = sorted(rows, key=lambda row: (row.state == STATE_OK, order.get(row.state, len(order)), row.asset_name or ""))

    return {
        "summary": summary,
        "rows": [{"finding": row, "state": coverage_state(row.state)} for row in rows],
        "state_options": [
            (value, COVERAGE_STATE_LABELS.get(value, (value, "muted"))[0], summary["by_state"].get(value, 0))
            for value in COVERAGE_STATES
            if summary["by_state"].get(value)
        ],
        "provider_options": sorted({row.provider_code for row in findings if row.provider_code}),
        "connection_options": sorted(
            {(row.connection_id, row.connection_name) for row in findings if row.connection_id is not None}
        ),
        "filters": {"only_problems": only_problems, "provider": provider, "connection": connection, "state": state},
        "showing": len(rows),
    }
