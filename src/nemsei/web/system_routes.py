"""Estado do sistema: read-only, and deliberately so.

There is no "sync now" button here, and its absence is a decision rather than
an omission. The FusionSolar account is shared with production V1 and is
already rate-limiting the scheduled runs; a manual trigger would let a worried
operator spend the very budget the scheduler needs, against an account the
running operation depends on. The page's job is to make that visible, which is
what turns "the charts are empty" into "the provider is refusing us".
"""
from __future__ import annotations

from flask import Blueprint, render_template

from flask import request

from nemsei.system.integration_health import STATE_TONES, system_health
from nemsei.web.db_session import get_request_session
from nemsei.web.home_routes import require_authenticated
from nemsei.web.production_coverage_queries import production_coverage_page

system_bp = Blueprint("system", __name__, url_prefix="/system")


@system_bp.get("")
@require_authenticated
def index() -> str:
    return render_template(
        "system.html",
        title="Estado do sistema",
        tones=STATE_TONES,
        **system_health(get_request_session()),
    )


@system_bp.get("/cobertura-producao")
@require_authenticated
def production_coverage() -> str:
    """Porque é que a frota tem centrais sem produção diária.

    Vive sob /system pela mesma razão que a página ao lado: é
    observabilidade de infra-estrutura, não uma vista de negócio, e não
    executa nada. Também não faz uma única chamada ao provider -- tudo o
    que lê já está em tabelas que esta plataforma escreveu.
    """
    return render_template(
        "production_coverage.html",
        title="Cobertura de produção",
        **production_coverage_page(
            get_request_session(),
            only_problems=request.args.get("only_problems") == "1",
            provider=request.args.get("provider", "").strip(),
            connection=request.args.get("connection", "").strip(),
            state=request.args.get("state", "").strip(),
        ),
    )
