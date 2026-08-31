"""Bloco E: the automations screen, and the line it refuses to blur.

Five schedulers are environment variables read once at process start; the
notification channel and its policies are database rows. The screen reports the
first group and controls the second, and never pretends otherwise -- a toggle
that quietly did nothing would be worse than no toggle at all.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from nemsei.app import create_app
from nemsei.db import build_engine, build_session_factory
from nemsei.jobs.models import Job, ScheduleState
from nemsei.notifications.models import NotificationChannel, NotificationPolicy
from nemsei.providers.service import create_connection
from nemsei.system.automation_health import HEARTBEAT_KEY
from nemsei.providers.models import OperatorAuditEvent
from nemsei.shared.clock import utc_now
from nemsei.system.automations import set_channel_enabled
from tests_v2.test_migrations import upgrade


def seeded(settings, monkeypatch, *, target_chat_id: str | None = None):
    upgrade(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    now = utc_now()
    channel = NotificationChannel(
        name="Ops Telegram", kind="telegram", enabled=False,
        target_chat_id=target_chat_id, created_at=now, updated_at=now,
    )
    session.add(channel)
    session.flush()
    policy = NotificationPolicy(
        name="Criticos imediatos", enabled=False, channel_id=channel.id,
        min_severity="critical", notify_on_open=True, notify_on_resolve=True,
        baseline_at=now - timedelta(days=1), created_at=now, updated_at=now,
    )
    session.add(policy)
    session.commit()
    ids = (channel.id, policy.id)
    session.close()
    client = create_app(settings).test_client()
    with client.session_transaction() as browser:
        browser["authenticated"], browser["username"], browser["csrf_token"] = True, "admin", "test"
    return client, ids


def channel_of(settings, channel_id: int) -> NotificationChannel:
    session = build_session_factory(build_engine(settings))()
    try:
        row = session.get(NotificationChannel, channel_id)
        session.expunge(row)
        return row
    finally:
        session.close()


def audit(settings) -> list[str]:
    session = build_session_factory(build_engine(settings))()
    try:
        return list(session.scalars(select(OperatorAuditEvent.action).order_by(OperatorAuditEvent.id)))
    finally:
        session.close()


def test_the_page_invents_no_automation_that_is_not_really_scheduled(settings, monkeypatch) -> None:
    """Rows are derived from `schedule_state`, not from a list in the code.

    The screen used to render a fixed catalogue of six automations whether or
    not any of them existed, which made "sem registo" the most common answer
    and made the two `monitoring.current` schedules -- which really were
    running, every fifteen minutes -- invisible because no catalogue entry
    matched them.
    """
    client, _ = seeded(settings, monkeypatch)

    page = client.get("/automations")

    assert page.status_code == 200
    assert "Nenhum agendamento escreveu ainda" in page.text
    assert "Sincronização de produção" not in page.text
    # The scheduler's own health is a separate statement, and with no heartbeat
    # row it must not be claimed to be alive.
    assert "sem sinal" in page.text
    assert "só leitura" in page.text


def test_the_page_separates_scheduler_health_from_execution_health(settings, monkeypatch) -> None:
    """A schedule firing on time into a job that fails is not a healthy row."""
    client, _ = seeded(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    now = utc_now()
    connection = create_connection(
        session, provider_code="fusionsolar", connection_key="fs", display_name="FusionSolar principal",
        credential_reference="primary", enabled=True, configuration_status="configured",
    )
    session.flush()
    key = f"production.incremental:{connection.id}"
    session.add(ScheduleState(schedule_key=HEARTBEAT_KEY, next_run_at=now + timedelta(minutes=30),
                              last_enqueued_at=now, updated_at=now))
    session.add(ScheduleState(schedule_key=key, next_run_at=now + timedelta(hours=20),
                              last_enqueued_at=now - timedelta(hours=4), updated_at=now))
    session.add(Job(
        job_type="production.incremental", status="failed", payload_json={},
        dedupe_key=f"{key}:slot", priority=100, available_at=now - timedelta(hours=4),
        attempt_count=3, max_attempts=3, created_at=now - timedelta(hours=4), updated_at=now - timedelta(hours=4),
        started_at=now - timedelta(hours=4), finished_at=now - timedelta(hours=4),
        error_type="RetryableJobError",
    ))
    session.commit()
    session.close()

    page = client.get("/automations")

    assert page.status_code == 200
    # The row is named for its connection, not for a generic definition.
    assert "FusionSolar principal" in page.text
    assert key in page.text
    # Scheduler healthy, execution failed, and the headline follows execution.
    assert ">falhou</span>" in page.text
    assert "a agendar" in page.text
    assert "vivo" in page.text
    # The switch is still named, and still said to live outside this process.
    assert "docker-compose.v2.yml" in page.text


def test_enabling_a_channel_with_nowhere_to_deliver_is_refused(settings, monkeypatch) -> None:
    # Not a half-configured channel: one that would fail on every attempt.
    client, (channel_id, _) = seeded(settings, monkeypatch, target_chat_id=None)

    response = client.post(f"/automations/channels/{channel_id}", data={"csrf_token": "test", "enabled": "on"}, follow_redirects=True)

    assert "não tem destino configurado" in response.text
    assert channel_of(settings, channel_id).enabled is False
    assert audit(settings) == []


def test_enabling_a_configured_channel_works_and_is_audited(settings, monkeypatch) -> None:
    client, (channel_id, _) = seeded(settings, monkeypatch, target_chat_id="-100123")

    client.post(f"/automations/channels/{channel_id}", data={"csrf_token": "test", "enabled": "on"})

    assert channel_of(settings, channel_id).enabled is True
    assert audit(settings) == ["automation_enabled"]

    client.post(f"/automations/channels/{channel_id}", data={"csrf_token": "test"})

    assert channel_of(settings, channel_id).enabled is False
    assert audit(settings) == ["automation_enabled", "automation_disabled"]


def test_toggling_to_the_state_it_already_has_writes_no_audit_row(settings, monkeypatch) -> None:
    client, (channel_id, _) = seeded(settings, monkeypatch, target_chat_id="-100123")

    client.post(f"/automations/channels/{channel_id}", data={"csrf_token": "test"})

    assert audit(settings) == []


def test_a_policy_can_be_switched_independently_of_its_channel(settings, monkeypatch) -> None:
    client, (channel_id, policy_id) = seeded(settings, monkeypatch)

    client.post(f"/automations/policies/{policy_id}", data={"csrf_token": "test", "enabled": "on"})

    session = build_session_factory(build_engine(settings))()
    policy = session.get(NotificationPolicy, policy_id)
    assert policy.enabled is True
    # An enabled policy behind a disabled channel decides and records, and
    # delivers nothing. That combination is the whole point of the kill switch.
    assert session.get(NotificationChannel, channel_id).enabled is False
    session.close()


def test_the_digest_preview_writes_nothing(settings, monkeypatch) -> None:
    client, _ = seeded(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    from nemsei.notifications.models import DigestRun

    before = session.scalar(select(NotificationChannel.id))  # keep the session warm
    assert before is not None
    session.close()

    response = client.get("/automations/digest-preview")

    assert response.status_code == 200
    assert "nada foi escrito" in response.text
    session = build_session_factory(build_engine(settings))()
    assert list(session.scalars(select(DigestRun))) == []
    session.close()


def test_an_unknown_channel_is_refused(settings, monkeypatch) -> None:
    _, _ = seeded(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()

    with pytest.raises(ValueError, match="Canal desconhecido"):
        set_channel_enabled(session, channel_id=9999, enabled=True, actor="admin")
    session.close()
