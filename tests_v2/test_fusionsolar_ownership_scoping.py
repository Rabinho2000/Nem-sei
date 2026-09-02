"""V1's account lease is only relevant to the connection that shares V1's
account. `om_api2` (2026-09-02), FusionSolarRequestController's first
connection whose `credential_reference` isn't `"primary"`, was never V1's to
coordinate -- leasing it anyway would be worse than pointless: it would hold
a coordination slot V1 itself might need, for a call that never touches V1's
account at all.

These prove the distinction directly against the ownership broker rather
than against a real provider call: point `NEMSEI_V1_OWNERSHIP_BROKER_URL` at
an address nothing answers on, so any attempt to acquire the lease fails
loudly. A `primary` connection's call must fail closed exactly as before;
any other connection's call must succeed, because it never asks.
"""
from __future__ import annotations


from nemsei.db import build_engine, build_session_factory
from nemsei.integrations.fusionsolar.request_control import FusionSolarRequestController
from nemsei.providers.registry import ProviderCapability
from nemsei.providers.service import create_connection
from nemsei.sync.service import start_sync_run
from tests_v2.test_migrations import upgrade


UNREACHABLE_BROKER = "http://127.0.0.1:1"


def factory_for(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))


def make_connection(factory, *, credential_reference: str, key: str) -> tuple[int, int]:
    with factory() as session:
        connection = create_connection(
            session,
            provider_code="fusionsolar",
            connection_key=key,
            display_name=key,
            credential_reference=credential_reference,
            enabled=True,
            configuration_status="configured",
        )
        session.flush()
        run = start_sync_run(session, provider_connection_id=connection.id, capability=ProviderCapability.PRODUCTION_HISTORY.value)
        session.commit()
        return connection.id, run.id


def with_broker(monkeypatch, url: str = UNREACHABLE_BROKER):
    monkeypatch.setenv("NEMSEI_V1_OWNERSHIP_BROKER_URL", url)
    monkeypatch.setenv("NEMSEI_V1_OWNERSHIP_BROKER_TOKEN", "test-token")
    monkeypatch.delenv("NEMSEI_V2_SKIP_V1_OWNERSHIP_CHECK", raising=False)


def test_the_primary_connection_still_requires_v1s_lease(settings, monkeypatch):
    """Unchanged from before this connection-aware scoping existed: the
    shared account's own connection fails closed when the broker cannot be
    reached, exactly as the ownership window design requires."""
    with_broker(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, run_id = make_connection(factory, credential_reference="primary", key="primary-scope")
    controller = FusionSolarRequestController(factory)

    calls: list[str] = []
    _value, error = controller.call(
        connection_id=connection_id,
        sync_run_id=run_id,
        endpoint_family="production_history_daily",
        purpose="test",
        operation=lambda: calls.append("called"),
    )

    assert calls == [], "the operation must never run without V1's lease"
    assert error is not None and "V1 ownership unavailable" in error.safe_message


def test_a_non_primary_connection_never_asks_for_v1s_lease(settings, monkeypatch):
    """The same unreachable broker, the same call shape -- but this
    connection's own account has nothing to do with V1's, so the call
    succeeds without ever touching the broker."""
    with_broker(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id, run_id = make_connection(factory, credential_reference="om_api2", key="om-api2-scope")
    controller = FusionSolarRequestController(factory)

    calls: list[str] = []
    value, error = controller.call(
        connection_id=connection_id,
        sync_run_id=run_id,
        endpoint_family="production_history_daily",
        purpose="test",
        operation=lambda: calls.append("called") or "ok",
    )

    assert calls == ["called"], "a connection that isn't V1's shared account must never wait on V1's lease"
    assert error is None
    assert value == "ok"


def test_the_global_skip_flag_still_covers_the_primary_connection_too(settings, monkeypatch):
    """The escape hatch for 'V1 no longer runs FusionSolar at all' still
    works the same way it always did -- global, loud, and orthogonal to the
    per-connection scoping, not replaced by it."""
    with_broker(monkeypatch)
    monkeypatch.setenv("NEMSEI_V2_SKIP_V1_OWNERSHIP_CHECK", "true")
    import importlib

    from nemsei.integrations.fusionsolar import request_control as request_control_module

    importlib.reload(request_control_module)
    try:
        factory = factory_for(settings, monkeypatch)
        connection_id, run_id = make_connection(factory, credential_reference="primary", key="primary-skip-flag")
        controller = request_control_module.FusionSolarRequestController(factory)

        calls: list[str] = []
        _value, error = controller.call(
            connection_id=connection_id,
            sync_run_id=run_id,
            endpoint_family="production_history_daily",
            purpose="test",
            operation=lambda: calls.append("called"),
        )
        assert calls == ["called"]
        assert error is None
    finally:
        monkeypatch.delenv("NEMSEI_V2_SKIP_V1_OWNERSHIP_CHECK", raising=False)
        importlib.reload(request_control_module)
