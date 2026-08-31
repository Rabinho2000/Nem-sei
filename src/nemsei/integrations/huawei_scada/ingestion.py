"""Turning one dongle conversation into persisted, attributable evidence.

Everything here runs in short transactions, one per event, because the caller
is a long-lived socket handler: a session that stays open for eight hours must
not hold a database transaction for eight hours.

The rule that shapes this module is that **a dongle is identified by the serial
it announces and by nothing else**. There is no fallback to the address it
dialled in from, and there is no code path that creates a mapping. An unknown
serial lands in `huawei_scada_pending_dongles` and its session is closed. That
looks unhelpful for about a minute and then saves an entire class of incident:
a NAT rule edited on the customer's router, a DHCP lease moving, or two plants
behind the same public address would each, in an address-based scheme, quietly
attribute one customer's production to another.

The second rule is that a re-read is a revision, never a second measurement.
Samples are keyed on a quantised instant, so a dongle that reconnects and
re-reads the same interval supersedes its own row exactly the way a corrected
`ProductionFact` does. That is what makes "reconnects do not duplicate data"
a property of the schema rather than a hope about the network.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from nemsei.integrations.huawei_scada.models import (
    HuaweiScadaPendingDongle,
    HuaweiScadaPowerSample,
    HuaweiScadaSession,
)
from nemsei.integrations.huawei_scada.protocol import DongleAdvertisement
from nemsei.integrations.huawei_scada.session import DownstreamProbe, PollOutcome
from nemsei.monitoring.service import confirm_current_monitoring, record_current_monitoring_attempt
from nemsei.providers.errors import ProviderError, ProviderErrorCode
from nemsei.providers.models import AssetProviderMapping
from nemsei.providers.registry import ProviderCapability, ProviderCode, normalize_external_id
from nemsei.providers.repository import ProviderRepository
from nemsei.shared.clock import as_utc, utc_now
from nemsei.sources.service import resolve_source_policy, source_policy_date_for_asset
from nemsei.sync.models import SyncRun
from nemsei.sync.service import finish_sync_run, health_values_for_error, record_health, start_sync_run

# The provenance stamp every sample carries. The protocol has no timestamp of
# its own -- verified on both pilots -- and a reader must not have to guess
# whether `observed_at` came from the plant or from this server's clock.
OBSERVED_AT_SOURCE = "listener_receive_time"


@dataclass(frozen=True)
class DongleBinding:
    """Who a serial belongs to, or an explicit statement that nobody knows."""

    dongle_serial: str
    mapping_id: int | None
    asset_id: int | None
    monitoring_selected: bool = False
    reason: str | None = None

    @property
    def is_bound(self) -> bool:
        return self.mapping_id is not None and self.asset_id is not None


@dataclass(frozen=True)
class SampleOutcome:
    sample_id: int | None
    created: bool
    revision: int
    observation_written: bool


def quantise(moment: datetime, *, bucket_seconds: int) -> datetime:
    """Floor an instant onto the sampling grid that defines sample identity.

    Two reads of the same interval are the same measurement even when their
    clocks differ by a second, which is exactly what happens across a
    reconnect. Without this, every reconnect would mint a new row and a day's
    energy would be integrated over duplicated points.
    """
    if bucket_seconds <= 0:
        raise ValueError("Sample bucket must be a positive number of seconds.")
    moment = as_utc(moment)
    epoch_seconds = int(moment.timestamp())
    floored = epoch_seconds - (epoch_seconds % bucket_seconds)
    return datetime.fromtimestamp(floored, tz=moment.tzinfo)


def sample_key(dongle_serial: str, bucket: datetime) -> str:
    return f"huawei-scada:{dongle_serial}:{bucket.isoformat()}"


class HuaweiScadaIngestion:
    """Persistence for one listener process. Stateless between calls."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        connection_id: int,
        sample_bucket_seconds: int,
    ) -> None:
        self._sessions = session_factory
        self._connection_id = connection_id
        self._bucket_seconds = sample_bucket_seconds

    # --- session lifecycle --------------------------------------------------

    def open_session(self, *, peer_fingerprint: str | None) -> int:
        with self._sessions() as session:
            now = utc_now()
            row = HuaweiScadaSession(
                provider_connection_id=self._connection_id,
                session_state="connected",
                peer_fingerprint=peer_fingerprint,
                opened_at=now,
                last_seen_at=now,
                metadata_json={},
            )
            session.add(row)
            session.commit()
            return row.id

    def identify(self, *, session_id: int, advertisement: DongleAdvertisement, describe: dict[str, Any]) -> DongleBinding:
        """Bind an announced serial to a mapping, or quarantine it.

        Also opens the `SyncRun` this session reports under, so a dialled-in
        session appears in the same provider-sync surface every other
        integration already reports to, rather than in a listener-only log.
        """
        serial = advertisement.serial.strip()
        normalized = normalize_external_id(ProviderCode.HUAWEI_SCADA, serial)
        with self._sessions() as session:
            row = session.get(HuaweiScadaSession, session_id)
            assert row is not None
            mapping = ProviderRepository(session).active_external_claim(
                connection_id=self._connection_id, normalized_external_id=normalized, resource_kind="plant"
            )
            now = utc_now()
            row.dongle_serial = serial
            row.last_seen_at = now
            row.dongle_model = describe.get("dongle_model")
            row.dongle_software_version = describe.get("dongle_software_version")
            row.protocol_version = describe.get("protocol_version")
            row.aggregate_unit_id = describe.get("aggregate_unit_id")
            metadata = dict(row.metadata_json or {})
            metadata["advertisement_fields"] = describe.get("advertisement_fields", {})

            if mapping is None:
                row.session_state = "quarantined"
                row.safe_detail = "Dongle serial has no approved provider mapping."
                row.metadata_json = metadata
                self._quarantine(session, serial=serial, advertisement=advertisement, peer_fingerprint=row.peer_fingerprint)
                session.commit()
                return DongleBinding(serial, None, None, reason="unmapped_dongle")

            monitoring_selected = self._monitoring_is_selected(session, mapping)
            run = start_sync_run(
                session,
                provider_connection_id=self._connection_id,
                capability=ProviderCapability.CURRENT_MONITORING.value,
            )
            session.flush()
            metadata["sync_run_id"] = run.id
            metadata["monitoring_source_selected"] = monitoring_selected
            row.provider_mapping_id = mapping.id
            row.asset_id = mapping.asset_id
            row.session_state = "polling"
            row.metadata_json = metadata
            # A serial that was pending and is now mapped stops being pending.
            pending = session.scalar(
                select(HuaweiScadaPendingDongle).where(HuaweiScadaPendingDongle.dongle_serial == serial).with_for_update()
            )
            if pending is not None and pending.status != "mapped":
                pending.status = "mapped"
                pending.updated_at = now
            record_current_monitoring_attempt(session, provider_mapping_ids=[mapping.id], attempted_at=now)
            session.commit()
            return DongleBinding(serial, mapping.id, mapping.asset_id, monitoring_selected=monitoring_selected)

    def _monitoring_is_selected(self, session: Session, mapping: AssetProviderMapping) -> bool:
        """Does the asset's own source policy point monitoring at this mapping?

        A plant can be watched by FusionSolar and metered by a dongle at the
        same time. Writing a canonical current-state observation from both
        would make "what is this plant doing now" depend on which one answered
        last. Samples are stored either way -- they are this provider's own
        evidence -- but the canonical observation follows the policy.
        """
        try:
            on_date = source_policy_date_for_asset(session, asset_id=mapping.asset_id)
            policy = resolve_source_policy(
                session, asset_id=mapping.asset_id, source_use="monitoring", on_date=on_date
            )
        except ValueError:
            return False
        return policy.provider_mapping_id == mapping.id

    def _quarantine(
        self,
        session: Session,
        *,
        serial: str,
        advertisement: DongleAdvertisement,
        peer_fingerprint: str | None,
    ) -> None:
        now = utc_now()
        payload = {"fields": dict(advertisement.fields), "raw": advertisement.raw}
        session.execute(
            insert(HuaweiScadaPendingDongle)
            .values(
                dongle_serial=serial,
                provider_connection_id=self._connection_id,
                status="pending",
                first_seen_at=now,
                last_seen_at=now,
                session_count=1,
                peer_fingerprint=peer_fingerprint,
                advertisement_json=payload,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=("dongle_serial",),
                set_={
                    "last_seen_at": now,
                    "session_count": HuaweiScadaPendingDongle.session_count + 1,
                    "advertisement_json": payload,
                    "peer_fingerprint": peer_fingerprint,
                    "updated_at": now,
                },
                # A serial an operator explicitly rejected stays rejected. It
                # keeps knocking; that is not a reason to re-open the decision.
                where=HuaweiScadaPendingDongle.status != "rejected",
            )
        )

    def close_session(self, *, session_id: int, reason: str, safe_detail: str | None = None) -> None:
        with self._sessions() as session:
            row = session.get(HuaweiScadaSession, session_id)
            if row is None:  # pragma: no cover - only if the row was deleted
                return
            now = utc_now()
            row.session_state = "closed"
            row.close_reason = reason
            row.closed_at = now
            row.last_seen_at = max(as_utc(row.last_seen_at), now)
            if safe_detail:
                row.safe_detail = safe_detail[:500]
            run_id = (row.metadata_json or {}).get("sync_run_id")
            if isinstance(run_id, int):
                run = session.get(SyncRun, run_id)
                if run is not None and run.status == "running":
                    status, completeness, error = self._run_verdict(row, reason)
                    run.metadata_json = {
                        **(run.metadata_json or {}),
                        "actual_provider_calls": row.poll_count,
                        "expected_items": row.poll_count,
                        "items_received": row.sample_count,
                        "items_accepted": row.sample_count,
                        "items_rejected": row.error_count,
                        "close_reason": reason,
                        "transport": "inbound_tcp_session",
                    }
                    finish_sync_run(session, run=run, status=status, completeness=completeness, error=error)
                    record_health(
                        session,
                        provider_connection_id=self._connection_id,
                        partial=status == "partial",
                        error=error,
                        **health_values_for_error(error, operation="sync"),
                    )
            session.commit()

    def _run_verdict(self, row: HuaweiScadaSession, reason: str) -> tuple[str, str, ProviderError | None]:
        if row.sample_count and not row.error_count:
            return "success", "complete", None
        if row.sample_count:
            return "partial", "partial", None
        error_code = ProviderErrorCode.UNAVAILABLE if reason in {"read_error", "protocol_error"} else ProviderErrorCode.INVALID_RESPONSE
        return "failed", "none", ProviderError(error_code, f"Huawei SCADA session ended as {reason} with no usable sample.")

    def touch(self, *, session_id: int, polls: int = 0, samples: int = 0, errors: int = 0) -> None:
        """Session liveness and counters -- the `last_seen` requirement, per session."""
        with self._sessions() as session:
            row = session.get(HuaweiScadaSession, session_id)
            if row is None:  # pragma: no cover
                return
            row.last_seen_at = utc_now()
            row.poll_count += polls
            row.sample_count += samples
            row.error_count += errors
            session.commit()

    def record_poll_failure(self, *, session_id: int, outcome: PollOutcome) -> None:
        """A poll that produced no reading. Evidence, not a persisted sample.

        Nothing is written to `huawei_scada_power_samples`: a failed read is
        not a measurement of zero, and inventing a row for it would put a
        fabricated point into the energy integration.
        """
        with self._sessions() as session:
            row = session.get(HuaweiScadaSession, session_id)
            if row is None:  # pragma: no cover
                return
            row.error_count += 1
            row.poll_count += 1
            row.last_seen_at = utc_now()
            row.session_state = "degraded"
            row.safe_detail = (outcome.safe_detail or outcome.error_code or "poll failed")[:500]
            metadata = dict(row.metadata_json or {})
            failures = dict(metadata.get("poll_failures") or {})
            key = outcome.error_code or "unknown"
            failures[key] = int(failures.get(key, 0)) + 1
            metadata["poll_failures"] = failures
            row.metadata_json = metadata
            session.commit()

    def record_unknown_register_map(self, *, session_id: int, outcome: PollOutcome) -> None:
        """A device that does not speak the SDongle map, recorded as such.

        This is what a SmartLogger is expected to look like until someone
        verifies its registers: identified, its model captured from the
        banner, and explicitly marked as unsupported rather than left looking
        like a flaky connection. Everything a future map needs is here.
        """
        with self._sessions() as session:
            row = session.get(HuaweiScadaSession, session_id)
            if row is None:  # pragma: no cover
                return
            row.last_seen_at = utc_now()
            row.poll_count += 1
            row.error_count += 1
            row.session_state = "degraded"
            row.safe_detail = (outcome.safe_detail or "unknown register map")[:500]
            metadata = dict(row.metadata_json or {})
            metadata["register_map"] = {
                "supported": False,
                "probed_block": "sdongle_aggregate_37498_37517",
                "exception": outcome.exception_name,
                "exception_code": outcome.exception_code,
                "observed_at": utc_now().isoformat(),
            }
            row.metadata_json = metadata
            session.commit()

    def record_downstream_probe(self, *, session_id: int, probe: DownstreamProbe) -> None:
        """What `unit=1` said, kept as session evidence and nothing more.

        On both pilots this is `slave_device_failure`. It is recorded so the
        day it changes is visible, and it never influences whether the session
        keeps collecting.
        """
        with self._sessions() as session:
            row = session.get(HuaweiScadaSession, session_id)
            if row is None:  # pragma: no cover
                return
            metadata = dict(row.metadata_json or {})
            metadata["downstream_probe"] = {
                "unit_id": probe.unit_id,
                "answered": probe.answered,
                "exception": probe.exception_name,
                "exception_code": probe.exception_code,
                "checked_at": utc_now().isoformat(),
            }
            row.metadata_json = metadata
            session.commit()

    # --- samples ------------------------------------------------------------

    def record_sample(self, *, session_id: int, binding: DongleBinding, outcome: PollOutcome) -> SampleOutcome:
        """Persist one aggregate reading, superseding any earlier revision of it."""
        if not binding.is_bound or outcome.reading is None:
            raise ValueError("A sample requires a bound dongle and a decoded reading.")
        reading = outcome.reading
        bucket = quantise(outcome.observed_at, bucket_seconds=self._bucket_seconds)
        key = sample_key(binding.dongle_serial, bucket)
        quality, completeness = _quality_for(reading.signal_count)
        with self._sessions() as session:
            row = session.get(HuaweiScadaSession, session_id)
            existing = session.scalar(
                select(HuaweiScadaPowerSample)
                .where(
                    HuaweiScadaPowerSample.provider_mapping_id == binding.mapping_id,
                    HuaweiScadaPowerSample.source_sample_key == key,
                )
                .order_by(HuaweiScadaPowerSample.source_revision.desc())
            )
            values = {
                "pv_input_power_kw": reading.pv_input_power_kw,
                "load_power_kw": reading.load_power_kw,
                "grid_power_kw": reading.grid_power_kw,
                "battery_power_kw": reading.battery_power_kw,
                "total_active_power_kw": reading.total_active_power_kw,
            }
            if existing is not None and _unchanged(existing, values, quality, completeness):
                # The same interval, re-read across a reconnect, with the same
                # answer: nothing to record. The row that exists already is
                # the measurement.
                if row is not None:
                    row.last_seen_at = utc_now()
                    row.poll_count += 1
                session.commit()
                return SampleOutcome(existing.id, False, existing.source_revision, False)

            metadata = {
                "observed_at_source": OBSERVED_AT_SOURCE,
                "sample_bucket_seconds": self._bucket_seconds,
                "unit": "kW",
                "register_scale": 1000,
                "aggregate_unit_id": row.aggregate_unit_id if row is not None else None,
                "unsolicited_frames": outcome.unsolicited_frames,
            }
            if outcome.safe_detail:
                metadata["safe_detail"] = outcome.safe_detail[:500]
            sample = HuaweiScadaPowerSample(
                asset_id=binding.asset_id,
                provider_mapping_id=binding.mapping_id,
                session_id=session_id,
                dongle_serial=binding.dongle_serial,
                source_sample_key=key,
                source_revision=(existing.source_revision + 1) if existing is not None else 1,
                supersedes_sample_id=existing.id if existing is not None else None,
                observed_at=bucket,
                ingested_at=utc_now(),
                raw_registers_json={str(name): value for name, value in reading.raw_registers.items()},
                quality=quality,
                completeness=completeness,
                session_state="polling",
                metadata_json=metadata,
                **values,
            )
            session.add(sample)
            if row is not None:
                row.last_seen_at = utc_now()
                row.poll_count += 1
                row.sample_count += 1
                row.session_state = "polling"
            observation_written = False
            if binding.monitoring_selected:
                observation_written = self._confirm_monitoring(
                    session, binding=binding, reading=reading, quality=quality, completeness=completeness, row=row
                )
            session.commit()
            return SampleOutcome(sample.id, True, sample.source_revision, observation_written)

    def _confirm_monitoring(
        self,
        session: Session,
        *,
        binding: DongleBinding,
        reading: Any,
        quality: str,
        completeness: str,
        row: HuaweiScadaSession | None,
    ) -> bool:
        """Canonical current state, derived the same way Sigenergy's is.

        The dongle states no condition of any kind -- there is no status
        register in the aggregate block. So generation implies operational,
        and a complete reading with no generation stays `unknown` rather than
        `offline`: at night every plant in the country would otherwise be
        reported as down.
        """
        run_id = (row.metadata_json or {}).get("sync_run_id") if row is not None else None
        if not isinstance(run_id, int):
            return False
        generating = any(
            value is not None and value > 0
            for value in (reading.pv_input_power_kw, reading.total_active_power_kw)
        )
        condition = "operational" if generating else "unknown"
        confirm_current_monitoring(
            session,
            asset_id=binding.asset_id,
            provider_mapping_id=binding.mapping_id,
            source_observation_key=f"huawei-scada-current:{binding.dongle_serial}",
            observed_at=utc_now(),
            condition=condition,
            freshness="unknown",
            quality=quality,
            completeness=completeness,
            sync_run_id=run_id,
            raw_status_code=None,
            raw_status_text=None,
            metadata={
                "condition_source": "aggregate_power_generation" if generating else "aggregate_power_no_generation",
                "observed_at_source": OBSERVED_AT_SOURCE,
                "pv_input_power_kw": _plain(reading.pv_input_power_kw),
                "total_active_power_kw": _plain(reading.total_active_power_kw),
            },
            # `observed_at` is this server's clock and therefore always
            # different, so without this every poll would mint a revision.
            deduplicate_observed_at=True,
        )
        return True


def _plain(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _quality_for(signal_count: int) -> tuple[str, str]:
    if signal_count == 0:
        return "missing", "missing"
    if signal_count == 5:
        return "complete", "complete"
    return "partial", "partial"


def _unchanged(
    existing: HuaweiScadaPowerSample, values: dict[str, Decimal | None], quality: str, completeness: str
) -> bool:
    if existing.quality != quality or existing.completeness != completeness:
        return False
    for name, value in values.items():
        current = getattr(existing, name)
        if (current is None) != (value is None):
            return False
        if current is not None and value is not None and Decimal(current) != Decimal(value):
            return False
    return True


def stale_open_sessions(session: Session, *, older_than: timedelta, now: datetime | None = None) -> list[HuaweiScadaSession]:
    """Sessions a crashed listener left open. Reconciliation reads this."""
    cutoff = as_utc(now or utc_now()) - older_than
    return list(
        session.scalars(
            select(HuaweiScadaSession).where(
                HuaweiScadaSession.session_state != "closed",
                HuaweiScadaSession.last_seen_at < cutoff,
            )
        )
    )
