"""Session-reuse proofs (V2 FusionSolar rollout, session-reuse priority).

V1 caches one authenticated session per account for ~55 minutes
(`Nem-sei/monitoring_board/app_factory.py`: `FUSIONSOLAR_SESSION_CACHE`). V2
never did, until this module -- confirmed live 2026-08-23 when a handful of
test syncs against the shared production account tripped FusionSolar's own
login-endpoint rate limit after 13 cumulative logins. These tests prove the
fix at both layers: the cache primitive in isolation (fast, in-memory,
thread-based concurrency), and the real service call path end to end
(Postgres-backed, proving two separate sync calls share one login).
"""
from __future__ import annotations

import threading
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.integrations.fusionsolar.client import FusionSolarClient, FusionSolarClientError, FusionSolarCredentials, HttpResponse
from nemsei.integrations.fusionsolar.monitoring import FusionSolarMonitoringService
from nemsei.integrations.fusionsolar.session_cache import FusionSolarSessionCache
from nemsei.providers.errors import ProviderError, ProviderErrorCode
from nemsei.providers.service import create_connection, create_mapping
from nemsei.sources.service import create_source_policy
from nemsei.sync.models import ProviderRequestAttempt, ProviderRequestState
from tests_v2.test_migrations import upgrade


LOGIN_OK = {"success": True, "failCode": 0, "data": None}
CREDS = FusionSolarCredentials(username="fixture-user", password="fixture-password", base_url="https://fusion.example.test")


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, payload, headers, timeout_seconds):
        self.calls.append((url, payload, headers))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def response(payload, status=200, headers=None):
    return HttpResponse(status, headers or {}, payload)


def realtime(rows):
    return response({"success": True, "failCode": 0, "data": rows})


def row(station_code: str, state: str) -> dict:
    return {"stationCode": station_code, "dataItemMap": {"real_health_state": state}}


def factory_for(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(build_engine(settings))


def configured_environment(monkeypatch):
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_MONITOR_USERNAME", "fixture-user")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_MONITOR_PASSWORD", "fixture-password")
    monkeypatch.setenv("NEMSEI_V2_FUSIONSOLAR_MONITOR_BASE_URL", "https://fusion.example.test")


def selected_connection(factory):
    with factory() as session:
        connection = create_connection(
            session, provider_code="fusionsolar", connection_key="fusion-monitoring",
            display_name="Fusion monitoring", credential_reference="monitor",
            enabled=True, configuration_status="configured",
        )
        asset = create_asset(session, canonical_name="Session reuse asset")
        mapping = create_mapping(session, asset_id=asset.id, provider_connection_id=connection.id, external_id="FS-001")
        create_source_policy(session, asset_id=asset.id, provider_mapping_id=mapping.id, source_use="monitoring", priority=1, valid_from=date(2020, 1, 1))
        session.commit()
        return connection.id


def service_with_cache(factory, settings, transport, cache):
    configured = replace(settings, capabilities={**settings.capabilities, "provider_reads": True})
    return FusionSolarMonitoringService(
        factory, configured,
        client_factory=lambda credentials: FusionSolarClient(credentials, transport=transport),
        session_cache=cache,
    )


def login_count(factory, connection_id) -> int:
    with factory() as session:
        return len(list(session.scalars(
            select(ProviderRequestAttempt)
            .join(ProviderRequestState, ProviderRequestState.id == ProviderRequestAttempt.request_state_id)
            .where(
                ProviderRequestState.provider_connection_id == connection_id,
                ProviderRequestState.endpoint_family == "authentication",
                ProviderRequestAttempt.status == "succeeded",
            )
        )))


# --- Cache primitive, in isolation -----------------------------------------

def test_multiple_authenticate_calls_share_one_login():
    cache = FusionSolarSessionCache()
    login_calls = []

    def do_authenticate(client):
        login_calls.append(1)
        client.authenticate()
        return None, None

    def fresh(credentials):
        return FusionSolarClient(credentials, transport=FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"})]))

    client1, error1, reused1 = cache.get_or_authenticate(CREDS, client_factory=fresh, authenticate=do_authenticate)
    client2, error2, reused2 = cache.get_or_authenticate(CREDS, client_factory=fresh, authenticate=do_authenticate)

    assert error1 is None and error2 is None
    assert reused1 is False and reused2 is True
    assert len(login_calls) == 1, "a second call within the TTL must not re-authenticate"
    assert client1._token == client2._token == "t"


def test_expiry_forces_exactly_one_new_login():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = {"value": now}
    cache = FusionSolarSessionCache(ttl_minutes=45, clock=lambda: clock["value"])
    login_calls = []

    def do_authenticate(client):
        login_calls.append(1)
        client.authenticate()
        return None, None

    def fresh(credentials):
        return FusionSolarClient(credentials, transport=FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": f"token-{len(login_calls)}"})]))

    cache.get_or_authenticate(CREDS, client_factory=fresh, authenticate=do_authenticate)
    clock["value"] = now + timedelta(minutes=44)
    cache.get_or_authenticate(CREDS, client_factory=fresh, authenticate=do_authenticate)
    assert len(login_calls) == 1, "still within TTL -- must reuse"

    clock["value"] = now + timedelta(minutes=46)
    client, error, reused = cache.get_or_authenticate(CREDS, client_factory=fresh, authenticate=do_authenticate)
    assert error is None
    assert reused is False
    assert len(login_calls) == 2, "past TTL -- exactly one fresh login, not a leaked extra one"


def test_invalidate_forces_reauth_on_the_next_call():
    cache = FusionSolarSessionCache()
    login_calls = []

    def do_authenticate(client):
        login_calls.append(1)
        client.authenticate()
        return None, None

    def fresh(credentials):
        return FusionSolarClient(credentials, transport=FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"})]))

    cache.get_or_authenticate(CREDS, client_factory=fresh, authenticate=do_authenticate)
    assert len(login_calls) == 1
    cache.get_or_authenticate(CREDS, client_factory=fresh, authenticate=do_authenticate)
    assert len(login_calls) == 1, "still cached -- no invalidation yet"

    cache.invalidate(CREDS)
    cache.get_or_authenticate(CREDS, client_factory=fresh, authenticate=do_authenticate)
    assert len(login_calls) == 2, "invalidated -- the next call must re-authenticate exactly once"


def test_concurrent_callers_produce_at_most_one_login():
    cache = FusionSolarSessionCache()
    login_calls = []
    login_lock = threading.Lock()

    def do_authenticate(client):
        with login_lock:
            login_calls.append(1)
        client.authenticate()
        return None, None

    def fresh(credentials):
        return FusionSolarClient(credentials, transport=FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"})]))

    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait(timeout=5)
        results.append(cache.get_or_authenticate(CREDS, client_factory=fresh, authenticate=do_authenticate))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(login_calls) == 1, f"8 concurrent callers must produce exactly one login, not a retry storm; got {len(login_calls)}"
    assert all(error is None for _client, error, _reused in results)
    tokens = {client._token for client, _error, _reused in results if client is not None}
    assert tokens == {"t"}


def test_a_failed_login_is_never_cached():
    cache = FusionSolarSessionCache()

    def failing_authenticate(client):
        return None, ProviderError(ProviderErrorCode.AUTHENTICATION, "credentials rejected")

    def fresh(credentials):
        return FusionSolarClient(credentials, transport=FakeTransport([]))
    client, error, reused = cache.get_or_authenticate(CREDS, client_factory=fresh, authenticate=failing_authenticate)
    assert client is None
    assert error is not None and error.code is ProviderErrorCode.AUTHENTICATION
    assert reused is False

    # Nothing was cached -- the next attempt must try to authenticate again,
    # not silently reuse a session that was never actually established.
    login_calls = []

    def succeeding_authenticate(client):
        login_calls.append(1)
        client.authenticate()
        return None, None

    def fresh2(credentials):
        return FusionSolarClient(credentials, transport=FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"})]))
    cache.get_or_authenticate(CREDS, client_factory=fresh2, authenticate=succeeding_authenticate)
    assert len(login_calls) == 1


def test_session_expiry_on_a_data_call_is_classified_as_authentication_regardless_of_phase():
    """The widened client.py detection this module depends on: a session
    dying between logins during ordinary use (failCode 305 on a *data* call,
    not the login call) must be classified the same way a rejected login is,
    or invalidate_session()/is_session_expiry() never fire for it."""
    client = FusionSolarClient(CREDS, transport=FakeTransport([response({"success": False, "failCode": 305, "message": "USER_MUST_RELOGIN"})]))
    client._token = "stale-token"
    try:
        client.current_monitoring_batch(["FS-001"])
        raise AssertionError("expected FusionSolarClientError")
    except FusionSolarClientError as exc:
        assert exc.error.code is ProviderErrorCode.AUTHENTICATION


# --- Real service call path, end to end -------------------------------------

def test_two_separate_service_instances_sharing_a_cache_log_in_once(settings, monkeypatch):
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id = selected_connection(factory)
    cache = FusionSolarSessionCache()

    # One transport, reused: a cache hit keeps the *original* authenticated
    # client's transport (same cookie jar the login actually set), not
    # whatever transport a second `client_factory(credentials)` call would
    # otherwise construct -- that second client is discarded on a hit. So
    # the second sync's response is queued onto this same transport, not a
    # fresh one; see `test_two_independent_caches_...` below for the
    # contrasting case where two real transports are legitimately involved.
    transport = FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), realtime([row("FS-001", "3")])])

    first = service_with_cache(factory, settings, transport, cache).sync_current_monitoring(connection_id)
    assert first.status == "success"
    assert login_count(factory, connection_id) == 1

    # No LOGIN_OK appended -- if the service tried to re-authenticate, this
    # would raise IndexError (pop from an empty queue) instead of silently
    # succeeding, so a regression here fails loudly rather than passing by
    # accident.
    transport.responses.append(realtime([row("FS-001", "3")]))
    second = service_with_cache(factory, settings, transport, cache).sync_current_monitoring(connection_id)
    assert second.status == "success"
    assert login_count(factory, connection_id) == 1, "the second sync must reuse the cached session, not log in again"


def test_two_independent_caches_each_log_in_once_no_cross_talk(settings, monkeypatch):
    """Confirms the per-service-instance default (no shared cache passed)
    is safe by construction: two independently constructed services never
    silently share a session, matching every pre-existing test's assumption."""
    configured_environment(monkeypatch)
    factory = factory_for(settings, monkeypatch)
    connection_id = selected_connection(factory)
    configured = replace(settings, capabilities={**settings.capabilities, "provider_reads": True})

    def plain_service(transport):
        return FusionSolarMonitoringService(
            factory, configured,
            client_factory=lambda credentials: FusionSolarClient(credentials, transport=transport),
        )

    first = plain_service(FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), realtime([row("FS-001", "3")])])).sync_current_monitoring(connection_id)
    second = plain_service(FakeTransport([response(LOGIN_OK, headers={"XSRF-TOKEN": "t"}), realtime([row("FS-001", "3")])])).sync_current_monitoring(connection_id)
    assert first.status == "success" and second.status == "success"
    assert login_count(factory, connection_id) == 2, "independent service instances (the pre-existing default) must not share a cache"
