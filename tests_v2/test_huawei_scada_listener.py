"""The listener process: one binding, one conversation, one place with a socket.

`handle_transport` is driven directly with the same fake logger the session
tests use, so the whole path -- banner, identity, poll, persist, teardown --
runs end to end against a real database and no networking at all.
"""
from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select

from nemsei.assets.service import create_asset
from nemsei.config import ConfigurationError, Settings
from nemsei.db import build_engine, build_session_factory
from nemsei.integrations.huawei_scada.listener import (
    ExclusiveListenerLock,
    HuaweiScadaListener,
    ListenerConfig,
)
from nemsei.integrations.huawei_scada.models import (
    HuaweiScadaPendingDongle,
    HuaweiScadaPowerSample,
    HuaweiScadaSession,
)
from nemsei.providers.service import create_connection, create_mapping
from nemsei.sync.models import SyncRun
from tests_v2.test_huawei_scada_ingestion import SERIAL
from tests_v2.test_huawei_scada_session import FakeDongle
from tests_v2.test_huawei_scada_protocol import REAL_ADVERTISEMENT
from tests_v2.test_migrations import upgrade

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "src" / "nemsei"


def listener_settings(settings: Settings, connection_id: int) -> Settings:
    return replace(
        settings,
        process_role="scada_listener",
        huawei_scada_listener_enabled=True,
        huawei_scada_listener_connection_id=connection_id,
        # Fast enough to make a test finish, still inside the validated
        # ordering (read <= poll < idle).
        huawei_scada_poll_interval_seconds=1,
        huawei_scada_read_timeout_seconds=1,
        huawei_scada_handshake_timeout_seconds=2,
        huawei_scada_idle_timeout_seconds=2,
    )


@pytest.fixture
def world(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    with factory() as session, session.begin():
        connection = create_connection(
            session,
            provider_code="huawei_scada",
            connection_key="scada-pilot",
            display_name="Huawei SCADA pilot",
            credential_reference="primary",
            enabled=True,
            configuration_status="configured",
        )
        asset = create_asset(session, canonical_name="Piloto SDongle", timezone="Europe/Lisbon")
        session.flush()
        mapping = create_mapping(
            session,
            asset_id=asset.id,
            provider_connection_id=connection.id,
            external_id=SERIAL,
            valid_from=date(2026, 1, 1),
        )
        session.flush()
        ids = {"connection": connection.id, "asset": asset.id, "mapping": mapping.id}
    return factory, engine, ids, listener_settings(settings, connection.id)


def listener_for(world) -> HuaweiScadaListener:
    factory, engine, _ids, listener_config = world
    return HuaweiScadaListener(listener_config, session_factory=factory, engine=engine)


def terminated_banner(serial: str = SERIAL) -> bytes:
    return REAL_ADVERTISEMENT.replace(SERIAL.encode(), serial.encode()) + b")"


# --- one full conversation ----------------------------------------------------


def test_a_mapped_dongle_is_polled_and_its_readings_are_persisted(world) -> None:
    """The acceptance criterion, end to end: connect, identify, persist."""
    factory, _engine, ids, _settings = world
    listener = listener_for(world)
    # probe + two polls, then the logger hangs up.
    dongle = FakeDongle(banner=terminated_banner(), reset_after_requests=3)

    reason = listener.handle_transport(dongle, peer_host="203.0.113.9")

    assert reason == "peer_closed"
    with factory() as session:
        samples = list(session.scalars(select(HuaweiScadaPowerSample)))
        assert samples, "the listener persisted nothing"
        assert {sample.asset_id for sample in samples} == {ids["asset"]}
        assert {sample.dongle_serial for sample in samples} == {SERIAL}
        row = session.scalar(select(HuaweiScadaSession))
        assert row.session_state == "closed"
        assert row.close_reason == "peer_closed"
        assert row.dongle_serial == SERIAL
        assert row.sample_count == len(samples)
        # The inverter's refusal was recorded, and cost nothing.
        assert row.metadata_json["downstream_probe"]["exception"] == "slave_device_failure"
        assert session.scalar(select(SyncRun)).status == "success"


def test_the_refusing_inverter_never_interrupts_the_session(world) -> None:
    factory, _engine, _ids, _settings = world
    listener = listener_for(world)
    dongle = FakeDongle(banner=terminated_banner(), refuse_units={1: 0x04}, reset_after_requests=5)

    listener.handle_transport(dongle, peer_host="203.0.113.9")

    with factory() as session:
        row = session.scalar(select(HuaweiScadaSession))
        assert row.sample_count >= 2
        assert row.error_count == 0


def test_an_unknown_dongle_is_quarantined_and_disconnected(world) -> None:
    factory, _engine, _ids, _settings = world
    listener = listener_for(world)
    dongle = FakeDongle(banner=terminated_banner("HV0000000000"))

    reason = listener.handle_transport(dongle, peer_host="203.0.113.9")

    assert reason == "unmapped_dongle"
    assert dongle.closed
    with factory() as session:
        assert session.scalar(select(HuaweiScadaPowerSample)) is None
        assert session.scalar(select(HuaweiScadaPendingDongle)).dongle_serial == "HV0000000000"
        # No sync run: nothing was ever collected under this session.
        assert session.scalar(select(SyncRun)) is None


def test_a_peer_that_never_announces_itself_ends_as_an_idle_timeout(world) -> None:
    factory, _engine, _ids, _settings = world
    listener = listener_for(world)

    reason = listener.handle_transport(FakeDongle(banner=b""), peer_host="203.0.113.9")

    assert reason == "idle_timeout"
    with factory() as session:
        row = session.scalar(select(HuaweiScadaSession))
        assert row.close_reason == "idle_timeout"
        assert row.dongle_serial is None


def test_repeated_failed_polls_tear_the_session_down_rather_than_spin(world) -> None:
    factory, _engine, _ids, _settings = world
    listener = listener_for(world)
    # Answers the banner and the probe, then refuses every aggregate read.
    dongle = FakeDongle(banner=terminated_banner(), refuse_units={1: 0x04, 100: 0x06})

    reason = listener.handle_transport(dongle, peer_host="203.0.113.9")

    assert reason == "read_error"
    with factory() as session:
        row = session.scalar(select(HuaweiScadaSession))
        assert row.error_count >= 5
        assert session.scalar(select(HuaweiScadaPowerSample)) is None
        assert session.scalar(select(SyncRun)).status == "failed"


def test_more_dongles_than_the_session_cap_are_refused_not_queued(world) -> None:
    factory, engine, _ids, listener_config = world
    listener = HuaweiScadaListener(
        replace(listener_config, huawei_scada_max_sessions=1), session_factory=factory, engine=engine
    )
    listener._slots.acquire()  # one session already in flight

    dongle = FakeDongle(banner=terminated_banner())
    assert listener.handle_transport(dongle, peer_host="203.0.113.9") == "session_limit"
    assert dongle.closed


def test_shutting_down_ends_the_poll_loop_at_the_next_interval(world) -> None:
    factory, _engine, _ids, _settings = world
    listener = listener_for(world)
    listener.stop()

    reason = listener.handle_transport(FakeDongle(banner=terminated_banner()), peer_host="203.0.113.9")

    assert reason == "listener_shutdown"
    with factory() as session:
        assert session.scalar(select(HuaweiScadaSession)).close_reason == "listener_shutdown"


def test_the_listener_refuses_a_connection_that_cannot_receive_data(world, settings, monkeypatch) -> None:
    """A listener bound to a disabled connection would quarantine every dongle."""
    from nemsei.providers.models import ProviderConnection
    from nemsei.integrations.huawei_scada.service import HuaweiScadaConfigurationError

    factory, _engine, ids, _listener_settings = world
    with factory() as session, session.begin():
        session.get(ProviderConnection, ids["connection"]).enabled = False

    with pytest.raises(HuaweiScadaConfigurationError, match="enabled and configured"):
        listener_for(world).preflight()


# --- exactly one listener -----------------------------------------------------


def test_a_second_listener_on_the_same_connection_cannot_take_the_lock(world) -> None:
    """What the published port cannot catch: a second listener elsewhere.

    Two listeners on one connection would double every sample, and there is no
    provider-side collision to make that visible -- the dongles would simply
    be split between them.
    """
    _factory, engine, ids, _settings = world
    first = ExclusiveListenerLock(engine, ids["connection"])
    second = ExclusiveListenerLock(create_engine(engine.url), ids["connection"])
    try:
        assert first.acquire()
        assert not second.acquire()
        first.release()
        # Released, so the next listener can take over after a restart.
        assert second.acquire()
    finally:
        first.release()
        second.release()


def test_two_connections_get_two_independent_locks(world) -> None:
    _factory, engine, ids, _settings = world
    one = ExclusiveListenerLock(engine, ids["connection"])
    two = ExclusiveListenerLock(create_engine(engine.url), ids["connection"] + 1)
    try:
        assert one.acquire()
        assert two.acquire()
    finally:
        one.release()
        two.release()


# --- configuration ------------------------------------------------------------


def test_the_listener_requires_an_explicit_connection_id(settings) -> None:
    with pytest.raises(ConfigurationError, match="portfolio-wide"):
        replace(settings, huawei_scada_listener_enabled=True).validate()


def test_a_poll_slower_than_the_idle_timeout_is_refused(settings) -> None:
    """The poll is the keep-alive, so this ordering is not a preference."""
    with pytest.raises(ConfigurationError, match="shorter than the idle timeout"):
        replace(settings, huawei_scada_poll_interval_seconds=600, huawei_scada_idle_timeout_seconds=300).validate()


def test_a_read_timeout_longer_than_the_poll_interval_is_refused(settings) -> None:
    with pytest.raises(ConfigurationError, match="read timeout"):
        replace(settings, huawei_scada_read_timeout_seconds=60, huawei_scada_poll_interval_seconds=30).validate()


def test_retention_shorter_than_the_rollup_lookback_is_refused(settings) -> None:
    """Otherwise a day loses its samples before its energy is final."""
    with pytest.raises(ConfigurationError, match="outlast the rollup lookback"):
        replace(settings, huawei_scada_retention_days=1, huawei_scada_rollup_lookback_days=2).validate()


def test_the_rollup_requires_an_explicit_connection_id(settings) -> None:
    with pytest.raises(ConfigurationError, match="portfolio-wide"):
        replace(settings, huawei_scada_rollup_enabled=True).validate()


def test_the_listener_config_refuses_to_build_when_the_listener_is_off(settings) -> None:
    with pytest.raises(ConfigurationError, match="not enabled"):
        ListenerConfig.from_settings(settings)


def test_the_sampling_grid_follows_the_poll_interval(settings) -> None:
    """Sample identity has to match the cadence, or every poll mints a new row."""
    config = ListenerConfig.from_settings(
        replace(
            settings,
            huawei_scada_listener_enabled=True,
            huawei_scada_listener_connection_id=7,
            huawei_scada_poll_interval_seconds=60,
            huawei_scada_read_timeout_seconds=15,
        )
    )
    assert config.sample_bucket_seconds == 60


# --- deployment contract ------------------------------------------------------


def test_the_listener_is_its_own_service_with_its_own_process_role() -> None:
    compose = (ROOT / "docker-compose.v2.yml").read_text(encoding="utf-8")
    listener_block = compose.split("  scada-listener:", 1)[1]
    assert "NEMSEI_V2_PROCESS_ROLE: scada_listener" in listener_block
    assert "python -m nemsei.integrations.huawei_scada.listener" in listener_block
    # Never inside the web worker: gunicorn forks, and each fork would either
    # fight for the port or open a second listener.
    web_block = compose.split("  web:", 1)[1].split("  scheduler:", 1)[0]
    assert "huawei" not in web_block.lower()


def test_exactly_one_service_publishes_the_scada_port() -> None:
    compose = (ROOT / "docker-compose.v2.yml").read_text(encoding="utf-8")
    publications = [line for line in compose.splitlines() if ":1502\"]" in line]
    assert len(publications) == 1
    assert "SCADA_BIND_ADDRESS" in publications[0]


# --- the listener's place in the canonical deploy -----------------------------
#
# Two failures pull in opposite directions and both are real. Starting the
# listener by accident opens this system's only inbound port on a deployment
# that never asked for it. Leaving it out of the deploy -- which is what the
# profile did until now -- means the listener serves a build older than
# everything around it, for as long as nobody looks. The wrapper has to be
# guarded, not absent, so these tests check the guard rather than the absence.


def compose_up_script() -> str:
    """The wrapper, with heredoc bodies removed.

    The runtime-isolation check is Python inlined in a heredoc, and its `if`
    statements have no `fi`; leaving them in would desynchronise the block
    tracking below.
    """
    lines = (ROOT / "scripts/v2_compose_up.sh").read_text(encoding="utf-8").splitlines()
    kept, inside = [], False
    for line in lines:
        if not inside and "<<'PY'" in line:
            inside = True
            continue
        if inside:
            inside = line.strip() != "PY"
            continue
        kept.append(line)
    return "\n".join(kept)


def guarded_by_declaration(script: str, index: int) -> bool:
    """True when line `index` sits inside an `if [[ $scada_declared == true ]]`."""
    stack: list[bool] = []
    for line in script.splitlines()[:index]:
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("if ") or stripped.startswith("if\t"):
            stack.append("$scada_declared == true" in stripped)
        elif stripped == "fi" and stack:
            stack.pop()
    return any(stack)


def test_the_listener_does_not_start_with_the_ordinary_deployment() -> None:
    """Turning on an inbound port is still a deliberate act."""
    compose = (ROOT / "docker-compose.v2.yml").read_text(encoding="utf-8")
    listener_block = compose.split("  scada-listener:", 1)[1]
    assert 'profiles: ["huawei-scada"]' in listener_block
    script = compose_up_script()
    # The ordinary roles start without it, always.
    ordinary = [line for line in script.splitlines() if "up -d web scheduler worker" in line]
    assert ordinary and all("scada-listener" not in line for line in ordinary)
    # And nothing touches the listener outside a declaration guard.
    for index, line in enumerate(script.splitlines()):
        if "scada-listener" in line and not line.strip().startswith("#"):
            assert guarded_by_declaration(script, index), f"ungated: {line.strip()}"


def test_the_canonical_deploy_does_not_ignore_a_declared_scada() -> None:
    """The regression this wrapper change exists to prevent.

    Before it, `v2_compose_up.sh` never named the listener at all, so a SCADA
    deployment shipped new code everywhere except the one service holding an
    open socket. Each of these four is a separate way to fall back into that:
    not asking, not building, not starting, or starting and not checking.
    """
    script = compose_up_script()
    assert "v2_scada_deployment_intent.py" in script
    assert "build scada-listener" in script
    assert "up -d scada-listener" in script
    # A declared listener that does not come up fails the deploy.
    check = script.index("up -d scada-listener")
    assert "exit 1" in script[check:]


def test_the_listener_is_built_before_it_is_started() -> None:
    """Same rule the migrate image taught: never start an image you did not build."""
    script = compose_up_script()
    assert script.index("build scada-listener") < script.index("up -d scada-listener")
    # And after the migration gate, like every other role.
    assert script.index("--live-revision") < script.index("up -d scada-listener")


@pytest.mark.parametrize(
    "path",
    [ROOT / "docker-compose.v2.yml", *(SOURCE / "integrations" / "huawei_scada").rglob("*.py")],
)
def test_no_public_address_or_port_is_hardcoded(path: Path) -> None:
    """The pilot's NAT is network configuration, not a constant in this repo."""
    text = path.read_text(encoding="utf-8")
    # Any dotted quad that is not a loopback or bind-all placeholder.
    addresses = {
        match
        for match in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text)
        if match not in {"127.0.0.1", "0.0.0.0", "255.255.255.255"}
    }
    assert not addresses, f"{path.name} names {addresses}"


def test_no_tunnel_or_relay_stands_between_the_dongle_and_this_server() -> None:
    """No edge collector, no Pinggy, no socat -- as code, not as a promise.

    Naming them in a comment is the point of the comment; naming them in
    something that runs is what this forbids. Same distinction the V1-import
    boundary test draws for `monitoring_board`.
    """
    import ast

    banned = ("pinggy", "socat", "ngrok", "frp")
    compose = (ROOT / "docker-compose.v2.yml").read_text(encoding="utf-8")
    executable = [
        line for line in compose.splitlines()
        if not line.lstrip().startswith("#") and any(key in line for key in ("command:", "image:", "entrypoint:"))
    ]
    assert not [line for line in executable for word in banned if word in line.lower()]

    for source_path in (SOURCE / "integrations" / "huawei_scada").rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                assert not any(word in node.value.lower() for word in banned)


def test_a_device_with_an_unknown_register_map_is_recorded_and_released(world) -> None:
    """A SmartLogger should be identified, not retried into the ground.

    The session row keeps the model, the serial and the exception, which is
    exactly the evidence needed to add its register map later -- and the
    socket is released after one question instead of five.
    """
    factory, _engine, _ids, _settings = world
    listener = listener_for(world)
    dongle = FakeDongle(banner=terminated_banner(), refuse_units={1: 0x04, 100: 0x02})

    reason = listener.handle_transport(dongle, peer_host="203.0.113.9")

    assert reason == "protocol_error"
    with factory() as session:
        row = session.scalar(select(HuaweiScadaSession))
        assert row.metadata_json["register_map"]["supported"] is False
        assert row.metadata_json["register_map"]["exception"] == "illegal_data_address"
        assert row.dongle_serial == SERIAL
        assert row.dongle_model == "SDongle-WLAN-FE"
        # One poll, not MAX_CONSECUTIVE_POLL_ERRORS of them.
        assert row.poll_count == 1
        assert session.scalar(select(HuaweiScadaPowerSample)) is None
