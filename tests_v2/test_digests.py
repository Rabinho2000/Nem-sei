"""Periodic diagnostic digests (D6): summary, never a second finding.

Every test proves a D6 requirement directly: window chaining ("since the
last digest"), idempotency (the same window never generates twice),
restart/concurrency safety (a SAVEPOINT-guarded race, same discipline as
D1/D3), new/persistent/resolved classification from real opened_at/
resolved_at fields, "nothing changed" rendering, priority ordering
(novidades > críticos > mudanças > backlog persistente), the top-N cap so
the digest never lists hundreds of incidents, and mock-only delivery with
the same auditable-failure/never-falsely-delivered guarantees D3 already
proved for immediate notifications.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError

from nemsei.assets.service import create_asset, create_device
from nemsei.db.session import build_session_factory
from nemsei.diagnostics.models import DiagnosticIncident
from nemsei.notifications.digests import (
    TOP_INSTALLATIONS_PER_PORTFOLIO,
    build_digest_payload,
    deliver_digest,
    generate_digest,
)
from nemsei.notifications.models import DigestRun, NotificationChannel
from nemsei.notifications.telegram_client import MockTelegramClient
from nemsei.portfolios.service import add_member, create_portfolio


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def utc(hour: int = 12, minute: int = 0, *, day: int = 24) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture
def factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(create_engine(settings.database_url))


def make_incident(
    session, *, asset_id: int, device_id: int | None = None, rule_code: str = "device_unavailable",
    severity: str = "critical", status: str = "open", opened_at: datetime | None = None, resolved_at: datetime | None = None,
) -> DiagnosticIncident:
    opened = opened_at or utc(9)
    incident = DiagnosticIncident(
        rule_code=rule_code, asset_id=asset_id, device_id=device_id, severity=severity, status=status,
        opened_at=opened, last_observed_at=resolved_at or opened, resolved_at=resolved_at,
        occurrence_count=1, detector_version="1", evidence_json={}, created_at=opened, updated_at=opened,
    )
    session.add(incident)
    session.flush()
    return incident


def make_portfolio_with_member(session, *, name: str, asset_id: int) -> int:
    portfolio = create_portfolio(session, name=name, created_by="tester")
    add_member(session, portfolio_id=portfolio.id, asset_id=asset_id, valid_from=date(2026, 1, 1), created_by="tester")
    return portfolio.id


def make_channel(session, *, enabled: bool = True, chat_id: str = "chat-1") -> NotificationChannel:
    channel = NotificationChannel(
        name="Digest Telegram", kind="telegram", enabled=enabled, target_chat_id=chat_id,
        created_at=utc(), updated_at=utc(),
    )
    session.add(channel)
    session.flush()
    return channel


def digest_runs(factory) -> list[DigestRun]:
    with factory() as session:
        return list(session.scalars(select(DigestRun).order_by(DigestRun.window_end)))


# --- window chaining: "since the last digest" is always literally true --------


def test_the_first_digest_bootstraps_its_window_to_one_interval(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Plant")
        make_portfolio_with_member(session, name="Portfolio", asset_id=asset.id)
        digest = generate_digest(session, window_end=utc(12), interval_minutes=60)

    assert digest is not None
    assert digest.window_end == utc(12)
    assert digest.window_start == utc(11)  # exactly one interval back, not an arbitrary size


def test_the_second_digests_window_starts_where_the_first_ended(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Plant")
        make_portfolio_with_member(session, name="Portfolio", asset_id=asset.id)
        first = generate_digest(session, window_end=utc(12), interval_minutes=60)
    with factory() as session, session.begin():
        second = generate_digest(session, window_end=utc(13), interval_minutes=60)

    assert second.window_start == first.window_end == utc(12)
    assert second.window_end == utc(13)


# --- idempotency: the same window never generates twice -----------------------


def test_generating_the_same_window_twice_returns_the_same_row(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Plant")
        make_portfolio_with_member(session, name="Portfolio", asset_id=asset.id)
        first = generate_digest(session, window_end=utc(12), interval_minutes=60)
        first_id = first.id
    with factory() as session, session.begin():
        second = generate_digest(session, window_end=utc(12), interval_minutes=60)

    assert second.id == first_id
    assert len(digest_runs(factory)) == 1


def test_restarted_process_reconciles_from_persisted_state_alone(factory, settings) -> None:
    """A fresh engine/session (standing in for a worker restart) must not
    grant a second digest for a window already generated."""
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Plant")
        make_portfolio_with_member(session, name="Portfolio", asset_id=asset.id)
        generate_digest(session, window_end=utc(12), interval_minutes=60)

    restarted_factory = build_session_factory(create_engine(settings.database_url))
    with restarted_factory() as session, session.begin():
        again = generate_digest(session, window_end=utc(12), interval_minutes=60)

    assert again is not None
    assert len(digest_runs(factory)) == 1


# --- restart/concurrency safety: a real race, not just re-running -------------


def test_concurrent_generation_for_the_same_window_never_duplicates(settings, factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Plant")
        make_portfolio_with_member(session, name="Portfolio", asset_id=asset.id)

    def run_once(_unused) -> None:
        engine = create_engine(settings.database_url)
        with build_session_factory(engine)() as session, session.begin():
            generate_digest(session, window_end=utc(12), interval_minutes=60)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(run_once, range(4)))

    assert len(digest_runs(factory)) == 1


def test_the_database_itself_rejects_a_second_run_for_the_same_window(factory) -> None:
    """Not just application logic -- the unique constraint on
    (window_start, window_end) must reject this even bypassing the service."""
    with pytest.raises(IntegrityError):
        with factory() as session, session.begin():
            for _ in range(2):
                session.add(
                    DigestRun(
                        window_start=utc(11), window_end=utc(12), generated_at=utc(12),
                        summary_json={}, rendered_text="x", delivery_status="pending", delivery_attempt_count=0,
                        created_at=utc(12), updated_at=utc(12),
                    )
                )
                session.flush()


# --- new / persistent / resolved classification --------------------------------


def test_incidents_are_classified_new_persistent_and_resolved_correctly(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Mixed Plant")
        device_a = create_device(session, asset_id=asset.id, device_kind="inverter", label="A", valid_from=date(2026, 1, 1))
        device_b = create_device(session, asset_id=asset.id, device_kind="inverter", label="B", valid_from=date(2026, 1, 1))
        device_c = create_device(session, asset_id=asset.id, device_kind="inverter", label="C", valid_from=date(2026, 1, 1))
        device_d = create_device(session, asset_id=asset.id, device_kind="inverter", label="D", valid_from=date(2026, 1, 1))
        make_portfolio_with_member(session, name="Portfolio", asset_id=asset.id)

        # A: persistent backlog -- opened well before the window, still open.
        make_incident(session, asset_id=asset.id, device_id=device_a.id, rule_code="stale_reading", severity="warning", opened_at=utc(hour=9, day=1))
        # B: new -- opened inside the window, still open.
        make_incident(session, asset_id=asset.id, device_id=device_b.id, rule_code="device_unavailable", severity="critical", opened_at=utc(11, 30))
        # C: resolved this window -- opened long before, resolved inside the window.
        make_incident(session, asset_id=asset.id, device_id=device_c.id, rule_code="stale_reading", severity="warning", status="resolved", opened_at=utc(hour=9, day=1), resolved_at=utc(11, 45))
        # D: opened AND resolved inside the same window -- both new and resolved, honestly.
        make_incident(session, asset_id=asset.id, device_id=device_d.id, rule_code="device_unavailable", severity="critical", status="resolved", opened_at=utc(11, 15), resolved_at=utc(11, 50))

        payload = build_digest_payload(session, window_start=utc(11), window_end=utc(12))

    entry = payload["portfolios"][0]
    assert entry["new_count"] == 2  # B and D
    assert entry["new_critical_count"] == 2  # both B and D are critical
    assert entry["resolved_count"] == 2  # C and D
    assert entry["persistent_count"] == 1  # only A: open, opened before the window


def test_a_window_with_no_changes_says_so_explicitly(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Quiet Plant")
        device = create_device(session, asset_id=asset.id, device_kind="inverter", label="A", valid_from=date(2026, 1, 1))
        make_portfolio_with_member(session, name="Portfolio", asset_id=asset.id)
        # Persistent backlog only -- nothing opened or resolved inside the window.
        make_incident(session, asset_id=asset.id, device_id=device.id, rule_code="stale_reading", severity="warning", opened_at=utc(hour=9, day=1))
        digest = generate_digest(session, window_end=utc(12), interval_minutes=60)

    assert "Sem alterações" in digest.rendered_text
    assert "nenhuma ocorrência crítica nova" in digest.rendered_text


# --- priority: novidades > críticos > mudanças > backlog persistente ----------


def test_rendered_text_leads_with_new_critical_occurrences(factory) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Plant")
        device = create_device(session, asset_id=asset.id, device_kind="inverter", label="A", valid_from=date(2026, 1, 1))
        make_portfolio_with_member(session, name="Portfolio", asset_id=asset.id)
        make_incident(session, asset_id=asset.id, device_id=device.id, rule_code="device_unavailable", severity="critical", opened_at=utc(11, 30))
        digest = generate_digest(session, window_end=utc(12), interval_minutes=60)

    lines = digest.rendered_text.splitlines()
    priority_index = lines.index("Prioridade:")
    assert "1 ocorrência(s) crítica(s) nova(s)" in lines[priority_index + 1]


def test_top_installations_are_capped_and_never_list_hundreds(factory) -> None:
    with factory() as session, session.begin():
        portfolio = create_portfolio(session, name="Big Portfolio", created_by="tester")
        for index in range(10):
            asset = create_asset(session, canonical_name=f"Plant {index:02d}")
            device = create_device(session, asset_id=asset.id, device_kind="inverter", label="A", valid_from=date(2026, 1, 1))
            add_member(session, portfolio_id=portfolio.id, asset_id=asset.id, valid_from=date(2026, 1, 1), created_by="tester")
            make_incident(session, asset_id=asset.id, device_id=device.id, rule_code="stale_reading", severity="warning", opened_at=utc(hour=9, day=1))
        digest = generate_digest(session, window_end=utc(12), interval_minutes=60)

    entry = digest.summary_json["portfolios"][0]
    assert entry["installations_with_incidents"] == 10  # the real total
    assert len(entry["top_installations"]) == TOP_INSTALLATIONS_PER_PORTFOLIO  # but the digest only names a few


# --- delivery: mock-only, same discipline D3 already proved -------------------


def test_a_digest_with_no_channel_configured_is_never_delivered(factory, settings) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Plant")
        make_portfolio_with_member(session, name="Portfolio", asset_id=asset.id)
        digest = generate_digest(session, window_end=utc(12), interval_minutes=60)
        digest_id = digest.id

    result = deliver_digest(build_session_factory(create_engine(settings.database_url)), digest_run_id=digest_id)
    assert result.attempted is False
    with factory() as session:
        assert session.get(DigestRun, digest_id).delivery_status == "pending"


def test_a_disabled_channel_never_calls_a_client_at_all(factory, settings) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Plant")
        make_portfolio_with_member(session, name="Portfolio", asset_id=asset.id)
        channel = make_channel(session, enabled=False)
        digest = generate_digest(session, window_end=utc(12), interval_minutes=60)
        digest.channel_id = channel.id
        digest_id = digest.id

    def explode(_channel):
        raise AssertionError("a disabled channel must never reach the client factory")

    result = deliver_digest(
        build_session_factory(create_engine(settings.database_url)), digest_run_id=digest_id, client_factory=explode
    )
    assert result.attempted is False


def test_a_successful_delivery_marks_the_digest_delivered(factory, settings) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Plant")
        make_portfolio_with_member(session, name="Portfolio", asset_id=asset.id)
        channel = make_channel(session, enabled=True)
        digest = generate_digest(session, window_end=utc(12), interval_minutes=60)
        digest.channel_id = channel.id
        digest_id = digest.id

    mock_client = MockTelegramClient()
    result = deliver_digest(
        build_session_factory(create_engine(settings.database_url)), digest_run_id=digest_id,
        client_factory=lambda _channel: mock_client,
    )
    assert result.attempted is True
    assert result.delivered is True
    assert len(mock_client.sent) == 1
    with factory() as session:
        row = session.get(DigestRun, digest_id)
        assert row.delivery_status == "delivered"
        assert row.delivered_at is not None


def test_a_failed_delivery_is_auditable_and_never_falsely_marked_delivered(factory, settings) -> None:
    with factory() as session, session.begin():
        asset = create_asset(session, canonical_name="Plant")
        make_portfolio_with_member(session, name="Portfolio", asset_id=asset.id)
        channel = make_channel(session, enabled=True, chat_id="chat-fails")
        digest = generate_digest(session, window_end=utc(12), interval_minutes=60)
        digest.channel_id = channel.id
        digest_id = digest.id

    failing_client = MockTelegramClient(fail_for_chat_ids=frozenset({"chat-fails"}))
    result = deliver_digest(
        build_session_factory(create_engine(settings.database_url)), digest_run_id=digest_id,
        client_factory=lambda _channel: failing_client,
    )
    assert result.attempted is True
    assert result.delivered is False
    with factory() as session:
        row = session.get(DigestRun, digest_id)
        assert row.delivery_status == "failed"
        assert row.delivered_at is None
        assert row.last_error is not None
        assert row.delivery_attempt_count == 1

    # And a retry with a working client succeeds without duplicating the row.
    succeeding_client = MockTelegramClient()
    retry_result = deliver_digest(
        build_session_factory(create_engine(settings.database_url)), digest_run_id=digest_id,
        client_factory=lambda _channel: succeeding_client,
    )
    assert retry_result.delivered is True
    assert len(digest_runs(factory)) == 1
    with factory() as session:
        row = session.get(DigestRun, digest_id)
        assert row.delivery_status == "delivered"
        assert row.delivery_attempt_count == 2
