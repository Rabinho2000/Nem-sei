"""Bloco F: whether the platform is working, answered without asking a provider.

Until this screen existed, learning that FusionSolar had been rate-limiting
every sync since morning meant opening psql. An operator who cannot see a
degraded provider reads an empty chart as an empty plant, which is the worst
mistake this product can cause.
"""
from __future__ import annotations

from datetime import timedelta

from nemsei.app import create_app
from nemsei.db import build_engine, build_session_factory
from nemsei.providers.service import create_connection
from nemsei.shared.clock import utc_now
from nemsei.sync.models import IntegrationHealth, SyncRun
from nemsei.system.integration_health import system_health
from tests_v2.test_migrations import upgrade


def seeded(settings, monkeypatch, *, runs: list[tuple[str, int]] = (), health: dict | None = None):
    upgrade(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    connection = create_connection(
        session, provider_code="fusionsolar", connection_key="live", display_name="FusionSolar",
        credential_reference="ref", enabled=True, configuration_status="configured",
    )
    session.flush()
    now = utc_now()
    for status, hours_ago in runs:
        session.add(
            SyncRun(
                provider_connection_id=connection.id, capability="production_history", status=status,
                started_at=now - timedelta(hours=hours_ago), completeness="unknown",
                error_code="rate_limited" if status == "rate_limited" else None, metadata_json={},
            )
        )
    if health is not None:
        session.add(IntegrationHealth(provider_connection_id=connection.id, updated_at=now, **health))
    session.commit()
    connection_id = connection.id
    session.close()
    client = create_app(settings).test_client()
    with client.session_transaction() as browser:
        browser["authenticated"], browser["username"] = True, "admin"
    return client, connection_id


def test_rate_limited_runs_are_counted_as_not_delivered(settings, monkeypatch) -> None:
    # A rate limit is not a defect in this platform -- it is a shared account
    # saying no -- but it is still a run that brought back nothing.
    client, _ = seeded(settings, monkeypatch, runs=[("success", 1), ("rate_limited", 2), ("rate_limited", 3), ("failed", 4)])

    session = build_session_factory(build_engine(settings))()
    summary = system_health(session)
    session.close()

    assert summary["runs_attempted"] == 4
    assert summary["runs_unhappy"] == 3
    assert summary["run_counts"]["rate_limited"] == 2


def test_runs_outside_the_window_are_not_counted(settings, monkeypatch) -> None:
    client, _ = seeded(settings, monkeypatch, runs=[("success", 1), ("failed", 200)])

    session = build_session_factory(build_engine(settings))()
    summary = system_health(session, window_hours=48)
    session.close()

    assert summary["runs_attempted"] == 1
    assert summary["runs_unhappy"] == 0


def test_the_worst_of_the_tracked_states_is_the_one_reported(settings, monkeypatch) -> None:
    client, _ = seeded(
        settings, monkeypatch,
        health={"auth_state": "healthy", "access_state": "healthy", "provider_state": "healthy", "quota_state": "degraded", "sync_state": "healthy", "discovery_state": "healthy"},
    )

    session = build_session_factory(build_engine(settings))()
    connection = system_health(session)["connections"][0]
    session.close()

    # Four states healthy and one degraded is a degraded connection, not a
    # healthy one with a footnote.
    assert connection.worst_state == "degraded"


def test_a_connection_never_contacted_reads_unknown_not_healthy(settings, monkeypatch) -> None:
    client, _ = seeded(settings, monkeypatch)

    session = build_session_factory(build_engine(settings))()
    connection = system_health(session)["connections"][0]
    session.close()

    assert connection.health is None
    assert connection.worst_state == "unknown"


def test_the_page_explains_a_rate_limit_instead_of_showing_a_blank(settings, monkeypatch) -> None:
    client, _ = seeded(settings, monkeypatch, runs=[("rate_limited", 1), ("rate_limited", 2)])

    page = client.get("/system")

    assert page.status_code == 200
    assert "limite de chamadas" in page.text
    assert "conta do provider a recusar" in page.text
    # And it says why there is no manual trigger, rather than leaving a gap.
    assert "Porque não há botão de sincronizar" in page.text


def test_the_page_lists_the_audit_trail(settings, monkeypatch) -> None:
    client, _ = seeded(settings, monkeypatch)

    page = client.get("/system")

    assert page.status_code == 200
    assert "Quem fez o quê" in page.text
