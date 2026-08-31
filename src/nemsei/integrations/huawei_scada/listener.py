"""The dedicated listener process: the only place in V2 that owns a socket.

This is a **separate process**, not a thread inside the web worker, and the
reason is not tidiness. Gunicorn is free to fork, restart or scale its workers;
every one of those events would either fight for the same TCP port or silently
open a second listener that answers half the dongles. A process whose entire
job is one bound port cannot do either.

Two guarantees keep it single:

1. **The port.** One container publishes it, and `docker-compose.v2.yml`
   binds it exactly once. A second instance fails to bind and exits loudly.
2. **A Postgres advisory lock** on the connection id. This catches what the
   port cannot: a second listener started on a different host or a different
   port but pointed at the *same* provider connection, which would double every
   sample without any collision to notice it.

The listener is event-driven and never outbound: it makes no scheduled provider
requests, it has no queue to drain, and it exists entirely to answer loggers
that dialled in. Rollup, retention and reconciliation are ordinary durable jobs
running in the worker, which is where recurring work belongs.
"""
from __future__ import annotations

import logging
import signal
import socket
import socketserver
import threading
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from nemsei.config import ConfigurationError, Settings
from nemsei.db import build_engine, build_session_factory
from nemsei.integrations.huawei_scada.ingestion import HuaweiScadaIngestion
from nemsei.integrations.huawei_scada.service import (
    HuaweiScadaConfigurationError,
    peer_fingerprint,
    require_huawei_scada_connection,
)
from nemsei.integrations.huawei_scada.session import (
    DongleSession,
    SessionClosed,
    SessionProtocolError,
    SessionTransport,
)
from nemsei.providers.repository import ProviderRepository

logger = logging.getLogger("nemsei.huawei_scada.listener")

# A fixed namespace for the advisory lock, so the key depends only on the
# connection id and is identical across deployments of this same code.
ADVISORY_LOCK_NAMESPACE = 0x48555721  # "HUW!" -- arbitrary, stable, documented.
# After this many consecutive failed polls the session is torn down. The dongle
# reconnects on its own; a socket that only produces errors is worth less than
# a fresh one.
MAX_CONSECUTIVE_POLL_ERRORS = 5
# Probe `unit=1` once per session plus once every N polls. Both pilots answer
# 0x83/0x04, so this is cheap curiosity, not a dependency.
DOWNSTREAM_PROBE_EVERY_POLLS = 120


@dataclass(frozen=True)
class ListenerConfig:
    host: str
    port: int
    connection_id: int
    poll_interval_seconds: float
    read_timeout_seconds: float
    handshake_timeout_seconds: float
    idle_timeout_seconds: float
    max_sessions: int
    sample_bucket_seconds: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "ListenerConfig":
        if not settings.huawei_scada_listener_enabled:
            raise ConfigurationError("Huawei SCADA listener is not enabled.")
        if settings.huawei_scada_listener_connection_id is None:
            raise ConfigurationError("Huawei SCADA listener requires an explicit provider connection id.")
        return cls(
            host=settings.huawei_scada_listener_host,
            port=settings.huawei_scada_listener_port,
            connection_id=settings.huawei_scada_listener_connection_id,
            poll_interval_seconds=float(settings.huawei_scada_poll_interval_seconds),
            read_timeout_seconds=float(settings.huawei_scada_read_timeout_seconds),
            handshake_timeout_seconds=float(settings.huawei_scada_handshake_timeout_seconds),
            idle_timeout_seconds=float(settings.huawei_scada_idle_timeout_seconds),
            max_sessions=settings.huawei_scada_max_sessions,
            sample_bucket_seconds=settings.huawei_scada_poll_interval_seconds,
        )


class ExclusiveListenerLock:
    """A Postgres advisory lock held for the lifetime of the listener.

    Held on its own dedicated connection, deliberately: an advisory lock lives
    as long as the session that took it, so borrowing a pooled connection
    would release the lock the moment that connection was returned.
    """

    def __init__(self, engine: Any, connection_id: int) -> None:
        self._engine = engine
        self._key = (ADVISORY_LOCK_NAMESPACE, connection_id)
        self._connection: Any | None = None

    def acquire(self) -> bool:
        connection = self._engine.connect()
        acquired = bool(
            connection.execute(
                text("SELECT pg_try_advisory_lock(:namespace, :connection_id)"),
                {"namespace": self._key[0], "connection_id": self._key[1]},
            ).scalar_one()
        )
        if not acquired:
            connection.close()
            return False
        self._connection = connection
        return True

    def release(self) -> None:
        if self._connection is None:
            return
        try:
            self._connection.execute(
                text("SELECT pg_advisory_unlock(:namespace, :connection_id)"),
                {"namespace": self._key[0], "connection_id": self._key[1]},
            )
        finally:
            self._connection.close()
            self._connection = None


class HuaweiScadaListener:
    """Accepts dongle connections and drives each one to persisted evidence.

    `handle_transport` is the seam everything is tested through: it takes any
    object shaped like a socket, so the full path from banner to
    `production_facts` runs in a test with no networking at all.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: Any = None,
        engine: Any = None,
        config: ListenerConfig | None = None,
    ) -> None:
        self.settings = settings.validate()
        self.config = config or ListenerConfig.from_settings(self.settings)
        self._engine = engine if engine is not None else build_engine(settings)
        self._sessions = session_factory or build_session_factory(self._engine)
        self._ingestion = HuaweiScadaIngestion(
            self._sessions,
            connection_id=self.config.connection_id,
            sample_bucket_seconds=self.config.sample_bucket_seconds,
        )
        self._stop = threading.Event()
        self._slots = threading.BoundedSemaphore(self.config.max_sessions)
        self._server: socketserver.TCPServer | None = None

    # --- lifecycle ----------------------------------------------------------

    def preflight(self) -> None:
        """Refuse to start against a connection that cannot receive data.

        Checked once, at startup, rather than per dongle: a listener bound to a
        disabled or wrong-provider connection would accept loggers and then
        quarantine every one of them, which looks like a protocol problem and
        is a configuration problem.
        """
        with self._sessions() as session:
            require_huawei_scada_connection(ProviderRepository(session).connection(self.config.connection_id))

    def stop(self, *_args: Any) -> None:
        self._stop.set()
        server = self._server
        if server is not None:
            threading.Thread(target=server.shutdown, daemon=True).start()

    def serve_forever(self) -> None:
        lock = ExclusiveListenerLock(self._engine, self.config.connection_id)
        if not lock.acquire():
            raise RuntimeError(
                "Another Huawei SCADA listener already holds provider connection "
                f"{self.config.connection_id}. Two listeners would double every sample."
            )
        try:
            self.preflight()
            signal.signal(signal.SIGTERM, self.stop)
            signal.signal(signal.SIGINT, self.stop)
            with _ListenerServer((self.config.host, self.config.port), self) as server:
                self._server = server
                logger.info(
                    "Huawei SCADA listener bound on %s:%s for provider connection %s",
                    self.config.host,
                    self.config.port,
                    self.config.connection_id,
                )
                server.serve_forever(poll_interval=0.5)
        finally:
            self._server = None
            lock.release()

    # --- one conversation ---------------------------------------------------

    def handle_transport(self, transport: SessionTransport, *, peer_host: str) -> str:
        """Drive one dongle from banner to teardown. Returns the close reason."""
        if not self._slots.acquire(blocking=False):
            _close_quietly(transport)
            return "session_limit"
        fingerprint = peer_fingerprint(peer_host, salt=self.settings.secret_key)
        session_id = self._ingestion.open_session(peer_fingerprint=fingerprint)
        dongle = DongleSession(
            transport=transport,
            poll_interval_seconds=self.config.poll_interval_seconds,
            read_timeout_seconds=self.config.read_timeout_seconds,
            handshake_timeout_seconds=self.config.handshake_timeout_seconds,
        )
        reason, detail = "peer_closed", None
        try:
            advertisement = dongle.handshake()
            binding = self._ingestion.identify(
                session_id=session_id, advertisement=advertisement, describe=dongle.describe()
            )
            if not binding.is_bound:
                # Quarantine already recorded. The socket is closed rather than
                # held: nothing useful can be done with data that belongs to
                # nobody, and holding it would hide the problem behind an
                # apparently healthy connection.
                return self._finish(session_id, "unmapped_dongle", "Dongle serial is not mapped to an asset.")
            reason, detail = self._poll_loop(dongle, session_id=session_id, binding=binding)
        except SessionClosed as closed:
            reason, detail = "peer_closed", str(closed)
        except TimeoutError as timeout:
            reason, detail = "idle_timeout", str(timeout)
        except SessionProtocolError as error:
            reason, detail = "protocol_error", str(error)
        except HuaweiScadaConfigurationError as error:
            reason, detail = "protocol_error", str(error)
        except OSError as error:  # pragma: no cover - transport died mid-teardown
            reason, detail = "read_error", str(error)
        finally:
            dongle.close()
            self._slots.release()
        return self._finish(session_id, reason, detail)

    def _poll_loop(self, dongle: DongleSession, *, session_id: int, binding: Any) -> tuple[str, str | None]:
        probe = dongle.probe_downstream_unit(1)
        self._ingestion.record_downstream_probe(session_id=session_id, probe=probe)
        consecutive_errors = 0
        polls = 0
        while not self._stop.is_set():
            outcome = dongle.poll_aggregate()
            polls += 1
            if outcome.ok:
                self._ingestion.record_sample(session_id=session_id, binding=binding, outcome=outcome)
                consecutive_errors = 0
            elif outcome.error_code == "unknown_register_map":
                # Not a fault, and not worth retrying: this device does not
                # implement the SDongle aggregate block. A SmartLogger is the
                # expected case. Record what it is and let go of the socket --
                # the evidence needed to add its map later is in the session
                # row, and hammering it five more times adds nothing.
                self._ingestion.record_unknown_register_map(session_id=session_id, outcome=outcome)
                return "protocol_error", (
                    "Device answered illegal-data-address for the SDongle aggregate block; "
                    "its register map is not one this integration knows."
                )
            else:
                self._ingestion.record_poll_failure(session_id=session_id, outcome=outcome)
                consecutive_errors += 1
                if consecutive_errors >= MAX_CONSECUTIVE_POLL_ERRORS:
                    return "read_error", f"{consecutive_errors} consecutive failed polls."
                # Backing off must not mean going silent: the NAT mapping the
                # dongle dialled through expires on an idle socket, and losing
                # it turns a transient fault into a full reconnect.
                dongle.keepalive_read()
            if polls % DOWNSTREAM_PROBE_EVERY_POLLS == 0:
                self._ingestion.record_downstream_probe(
                    session_id=session_id, probe=dongle.probe_downstream_unit(1)
                )
            if self._stop.wait(self.config.poll_interval_seconds):
                return "listener_shutdown", None
        return "listener_shutdown", None

    def _finish(self, session_id: int, reason: str, detail: str | None) -> str:
        self._ingestion.close_session(session_id=session_id, reason=reason, safe_detail=detail)
        return reason


class _ListenerServer(socketserver.ThreadingTCPServer):
    """One thread per dongle. A handful of loggers, each mostly idle."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], listener: HuaweiScadaListener) -> None:
        self.listener = listener
        super().__init__(address, _ConnectionHandler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        logger.exception("Huawei SCADA session raised from %s", _safe_peer(client_address))


class _ConnectionHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        listener: HuaweiScadaListener = self.server.listener  # type: ignore[attr-defined]
        peer_host = self.client_address[0] if self.client_address else ""
        self.request.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        reason = listener.handle_transport(self.request, peer_host=peer_host)
        logger.info("Huawei SCADA session from %s ended: %s", _safe_peer(self.client_address), reason)


def _safe_peer(client_address: Any) -> str:
    """Peer addresses stay out of persisted data; a log line is another matter."""
    try:
        return f"{client_address[0]}:{client_address[1]}"
    except (TypeError, IndexError):  # pragma: no cover
        return "unknown"


def _close_quietly(transport: SessionTransport) -> None:
    try:
        transport.close()
    except OSError:  # pragma: no cover
        pass


def main() -> None:  # pragma: no cover - process entry point
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_environment()
    if settings.process_role != "scada_listener":
        raise ConfigurationError("The Huawei SCADA listener must run with NEMSEI_V2_PROCESS_ROLE=scada_listener.")
    HuaweiScadaListener(settings).serve_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
