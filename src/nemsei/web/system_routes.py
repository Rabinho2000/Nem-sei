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

from nemsei.system.integration_health import STATE_TONES, system_health
from nemsei.web.db_session import get_request_session
from nemsei.web.home_routes import require_authenticated

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
