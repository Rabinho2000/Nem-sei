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
from nemsei.notifications.models import NotificationChannel, NotificationPolicy
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


def test_the_page_names_each_scheduler_and_the_variable_that_controls_it(settings, monkeypatch) -> None:
    client, _ = seeded(settings, monkeypatch)

    page = client.get("/automations")

    assert page.status_code == 200
    for label in ("Sincronização de produção", "Sonda de dispositivos", "Avaliação de incidentes", "Digest periódico"):
        assert label in page.text
    # The screen must say where the switch actually lives, not imply it is here.
    assert "NEMSEI_V2_PRODUCTION_SYNC_SCHEDULER_ENABLED" in page.text
    assert "só leitura" in page.text
    # And it must not claim to know a flag it cannot see: with no schedule_state
    # row, the honest answer is "sem registo", never "desligada".
    assert ">sem registo</span>" in page.text
    # ...and no row claims a flag this process cannot see. The word may appear
    # in the explanation below the table; what must not exist is the badge.
    assert ">desligada</span>" not in page.text
    assert ">ligada</span>" not in page.text


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
