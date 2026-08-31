#!/usr/bin/env python3
"""Onboard one Huawei SDongle: see who is knocking, then bind a serial to a plant.

Meant to be run inside a V2 container. `Dockerfile.v2` does not copy `scripts/`
into the image, so mount it:

    docker compose -p nemsei-v2 -f docker-compose.v2.yml --env-file .env.v2 \
        --profile manual run --rm --no-deps \
        -v "$PWD/scripts:/app/scripts:ro" migrate \
        python /app/scripts/huawei_scada_onboard.py status

Why this exists: a dongle identifies itself by the serial it announces and by
nothing else, and nothing in the running system will ever bind one on its own.
That is the right rule, and it means the binding has to happen somewhere. Doing
it here -- through `create_mapping` and `create_source_policy`, the same
deterministic path every other provider uses -- keeps it out of hand-written
SQL, where a typo silently attributes one customer's production to another.

Every action is idempotent and every action is refused rather than guessed:
an unknown asset, a serial already claimed by a different plant, or a
connection that is not enabled all stop the script with a reason.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from sqlalchemy import select

from nemsei.config import Settings
from nemsei.db import build_engine, build_session_factory
from nemsei.assets.models import Asset
from nemsei.contracts.models import AssetServiceContract
from nemsei.integrations.huawei_scada.fusionsolar_discovery import collectors_in
from nemsei.integrations.huawei_scada.models import (
    HuaweiScadaPendingDongle,
    HuaweiScadaPowerSample,
    HuaweiScadaSession,
)
from nemsei.providers.models import AssetProviderMapping, ProviderConnection
from nemsei.providers.registry import ProviderCode, normalize_external_id
from nemsei.providers.repository import ProviderRepository
from nemsei.providers.service import create_connection, create_mapping
from nemsei.sources.models import AssetSourcePolicy
from nemsei.sources.service import create_source_policy
from nemsei.sync.models import SyncRun


def sessions():
    return build_session_factory(build_engine(Settings.from_environment()))


def _connection(session, connection_id: int | None) -> ProviderConnection:
    if connection_id is not None:
        connection = session.get(ProviderConnection, connection_id)
        if connection is None or connection.provider_code != ProviderCode.HUAWEI_SCADA.value:
            raise SystemExit(f"Connection {connection_id} is not a huawei_scada connection.")
        return connection
    found = list(
        session.scalars(
            select(ProviderConnection).where(ProviderConnection.provider_code == ProviderCode.HUAWEI_SCADA.value)
        )
    )
    if not found:
        raise SystemExit("No huawei_scada connection exists yet. Create one with `create-connection`.")
    if len(found) > 1:
        raise SystemExit(f"Several huawei_scada connections exist ({[item.id for item in found]}); pass --connection-id.")
    return found[0]


def command_create_connection(args) -> int:
    """The connection carries no credential -- only the name of a contract."""
    with sessions()() as session, session.begin():
        existing = session.scalar(
            select(ProviderConnection).where(
                ProviderConnection.provider_code == ProviderCode.HUAWEI_SCADA.value,
                ProviderConnection.connection_key == args.key,
            )
        )
        if existing is not None:
            print(f"Connection already exists: id={existing.id} key={existing.connection_key}")
            return 0
        connection = create_connection(
            session,
            provider_code="huawei_scada",
            connection_key=args.key,
            display_name=args.name,
            credential_reference=args.credential_reference,
            enabled=True,
            configuration_status="configured",
        )
        session.flush()
        print(f"Created huawei_scada connection id={connection.id} key={connection.connection_key}")
        print(
            "Set the verified contract for it before the rollup can run:\n"
            f"  NEMSEI_V2_HUAWEI_SCADA_{args.credential_reference.upper()}_POWER_UNIT=kW\n"
            f"  NEMSEI_V2_HUAWEI_SCADA_{args.credential_reference.upper()}_PRODUCTION_SIGNAL=..."
        )
    return 0


def command_status(args) -> int:
    """Who is knocking, who is bound, and what has actually arrived."""
    with sessions()() as session:
        connection = _connection(session, args.connection_id)
        print(f"Connection {connection.id} ({connection.connection_key}) "
              f"enabled={connection.enabled} status={connection.configuration_status}")

        pending = list(
            session.scalars(
                select(HuaweiScadaPendingDongle).order_by(HuaweiScadaPendingDongle.last_seen_at.desc())
            )
        )
        print(f"\nDongles em quarentena ({len(pending)}):")
        for row in pending:
            print(f"  {row.dongle_serial:<20} {row.status:<9} sessions={row.session_count:<4} "
                  f"last_seen={row.last_seen_at:%Y-%m-%d %H:%M}")
        if not pending:
            print("  (nenhum)")

        mappings = [
            mapping
            for mapping in ProviderRepository(session).current_mappings_for_connection(connection.id)
            if mapping.mapping_status == "active"
        ]
        print(f"\nDongles ligados a centrais ({len(mappings)}):")
        for mapping in mappings:
            asset = session.get(Asset, mapping.asset_id)
            policies = list(
                session.scalars(
                    select(AssetSourcePolicy).where(AssetSourcePolicy.provider_mapping_id == mapping.id)
                )
            )
            uses = ",".join(sorted(policy.source_use for policy in policies)) or "SEM POLÍTICA"
            latest = session.scalar(
                select(HuaweiScadaPowerSample.observed_at)
                .where(HuaweiScadaPowerSample.provider_mapping_id == mapping.id)
                .order_by(HuaweiScadaPowerSample.observed_at.desc())
                .limit(1)
            )
            seen = f"{latest:%Y-%m-%d %H:%M}" if latest else "sem amostras"
            print(
                f"  {mapping.external_id:<20} asset={mapping.asset_id:<5} "
                f"{asset.canonical_name[:28]:<30} tz={asset.timezone or 'SEM FUSO':<14} "
                f"policies={uses:<24} última amostra={seen}"
            )

        open_sessions = list(
            session.scalars(
                select(HuaweiScadaSession)
                .where(HuaweiScadaSession.session_state != "closed")
                .order_by(HuaweiScadaSession.last_seen_at.desc())
            )
        )
        print(f"\nSessões abertas ({len(open_sessions)}):")
        for row in open_sessions:
            print(f"  {row.dongle_serial or '(por identificar)':<20} {row.session_state:<12} "
                  f"polls={row.poll_count:<5} samples={row.sample_count:<6} errors={row.error_count:<4} "
                  f"last_seen={row.last_seen_at:%Y-%m-%d %H:%M:%S}")
        if not open_sessions:
            print("  (nenhuma)")
    return 0


def command_bind(args) -> int:
    """Bind one announced serial to one plant, with its source policies."""
    with sessions()() as session, session.begin():
        connection = _connection(session, args.connection_id)
        if not connection.enabled or connection.configuration_status != "configured":
            raise SystemExit("Refusing: the connection must be enabled and configured first.")
        asset = session.get(Asset, args.asset_id)
        if asset is None:
            raise SystemExit(f"Refusing: asset {args.asset_id} does not exist.")
        if not asset.timezone:
            raise SystemExit(
                f"Refusing: asset {args.asset_id} has no timezone. Without it there is no local day "
                "and no source policy can be resolved, so the rollup would skip this plant entirely."
            )
        normalized = normalize_external_id(ProviderCode.HUAWEI_SCADA, args.serial)
        claimed = ProviderRepository(session).active_external_claim(
            connection_id=connection.id, normalized_external_id=normalized, resource_kind="plant"
        )
        if claimed is not None:
            if claimed.asset_id != asset.id:
                raise SystemExit(
                    f"Refusing: serial {args.serial} is already mapped to asset {claimed.asset_id}."
                )
            mapping = claimed
            print(f"Mapping already exists: id={mapping.id} asset={mapping.asset_id}")
        else:
            mapping = create_mapping(
                session,
                asset_id=asset.id,
                provider_connection_id=connection.id,
                external_id=args.serial.strip(),
                external_name=args.dongle_name,
                valid_from=date.fromisoformat(args.valid_from) if args.valid_from else None,
            )
            session.flush()
            print(f"Created mapping id={mapping.id} serial={mapping.external_id} asset={asset.id}")

        for use, wanted in (("monitoring", args.monitoring), ("production", args.production)):
            if not wanted:
                continue
            existing = session.scalar(
                select(AssetSourcePolicy).where(
                    AssetSourcePolicy.provider_mapping_id == mapping.id,
                    AssetSourcePolicy.source_use == use,
                )
            )
            if existing is not None:
                print(f"  {use}: policy already exists (priority={existing.priority}, fallback={existing.is_fallback})")
                continue
            create_source_policy(
                session,
                asset_id=asset.id,
                provider_mapping_id=mapping.id,
                source_use=use,
                priority=args.priority,
                is_fallback=args.fallback,
                valid_from=mapping.valid_from,
                actor_username=args.actor,
            )
            print(f"  {use}: policy created (priority={args.priority}, fallback={args.fallback})")

        pending = session.scalar(
            select(HuaweiScadaPendingDongle).where(HuaweiScadaPendingDongle.dongle_serial == args.serial.strip())
        )
        if pending is not None and pending.status != "mapped":
            pending.status = "mapped"
            print(f"  quarantine: {args.serial} marked as mapped")
    return 0


def command_reject(args) -> int:
    with sessions()() as session, session.begin():
        pending = session.scalar(
            select(HuaweiScadaPendingDongle).where(HuaweiScadaPendingDongle.dongle_serial == args.serial.strip())
        )
        if pending is None:
            raise SystemExit(f"No pending dongle with serial {args.serial}.")
        pending.status = "rejected"
        pending.notes = args.reason
        print(f"{args.serial} rejected: {args.reason}")
    return 0


# What a Huawei device calls itself when it is a data collector rather than an
# inverter. Matched on the model/name text the account already shows, NOT on
# `devTypeId`: the numeric type ids for these classes are not documented
# anywhere this repository can point at, and guessing one would silently map
# the wrong device. Every row's real `devTypeId` is reported instead, so one
# live run turns the guess into evidence.
COLLECTOR_MARKERS = ("sdongle", "dongle", "smartlogger", "logger")


def _collector_kind(row: dict) -> str | None:
    """`dongle`, `logger`, or None for anything that is not a collector."""
    text = " ".join(
        str(row.get(field) or "")
        for field in ("devName", "devTypeName", "model", "devModel", "invType", "name")
    ).casefold()
    if "smartlogger" in text or ("logger" in text and "dongle" not in text):
        return "logger"
    if "dongle" in text:
        return "dongle"
    return None


def _serial_of(row: dict) -> str:
    """The serial the device will *announce*, not its cloud id.

    `esnCode`/`sn` is what appears in field 4 of the announcement and is
    therefore the only identifier a mapping can be keyed on. `devId` is the
    account's internal handle and the dongle never says it.
    """
    return str(row.get("esnCode") or row.get("sn") or "").strip()


def command_discover(args) -> int:
    """Derive dongle/logger serials from FusionSolar instead of typing them.

    Reading them from the account that already knows them removes the step
    where a mistyped serial attributes one customer's production to another --
    the one error in this whole process that the source policy cannot catch.

    Dry run unless `--apply` is given, because creating a mapping is the
    consequential act here.
    """
    from nemsei.integrations.fusionsolar.client import FusionSolarClient
    from nemsei.integrations.fusionsolar.request_control import FusionSolarRequestController
    from nemsei.integrations.fusionsolar.service import credentials_for
    from nemsei.integrations.fusionsolar.session_cache import authenticated_client
    from nemsei.providers.registry import ProviderCapability
    from nemsei.sync.service import finish_sync_run, start_sync_run

    factory = sessions()
    with factory() as session:
        fusion = session.get(ProviderConnection, args.fusionsolar_connection_id) if args.fusionsolar_connection_id else None
        if fusion is None:
            candidates = list(
                session.scalars(
                    select(ProviderConnection).where(
                        ProviderConnection.provider_code == "fusionsolar", ProviderConnection.enabled
                    )
                )
            )
            if len(candidates) != 1:
                raise SystemExit("Pass --fusionsolar-connection-id: there is not exactly one enabled FusionSolar connection.")
            fusion = candidates[0]
        target = _connection(session, args.connection_id)

        scope = (
            select(AssetProviderMapping, Asset)
            .join(Asset, Asset.id == AssetProviderMapping.asset_id)
            .where(
                AssetProviderMapping.provider_connection_id == fusion.id,
                AssetProviderMapping.mapping_status == "active",
                AssetProviderMapping.resource_kind == "plant",
            )
        )
        if args.om_only:
            # `valid_from` is nullable (one V1 asset has no dates at all), so a
            # plain `<=` would silently drop it. NULL start means "always been
            # in scope", which is what the contracts model itself assumes.
            today = date.today()
            scope = scope.where(
                select(1)
                .where(
                    AssetServiceContract.asset_id == Asset.id,
                    (AssetServiceContract.valid_from.is_(None)) | (AssetServiceContract.valid_from <= today),
                    (AssetServiceContract.valid_to.is_(None)) | (AssetServiceContract.valid_to > today),
                )
                .exists()
            )
        plants = {
            mapping.external_id: (mapping.asset_id, asset.canonical_name)
            for mapping, asset in session.execute(scope).all()
        }
        credentials = credentials_for(fusion)

    if not plants:
        raise SystemExit("No active FusionSolar plant mapping matches that scope.")
    codes = sorted(plants)
    if args.limit:
        codes = codes[: args.limit]
    print(f"Centrais no âmbito: {len(codes)}"
          + (" (só com contrato O&M em vigor)" if args.om_only else ""))

    calls = FusionSolarRequestController(factory)
    with factory() as session:
        run = start_sync_run(
            session, provider_connection_id=fusion.id, capability=ProviderCapability.DEVICE_DISCOVERY.value
        )
        session.commit()
        run_id = run.id

    if not args.live:
        # Default. The device-status poll's own hourly `getDevList` already
        # carries every collector, and on this account it is the only call
        # that gets through -- competing with it for the slot just burns 407s.
        collectors = _collectors_from_last_sync(factory, fusion.id)
        if collectors is None:
            raise SystemExit(
                "No device-status sync run has recorded a collector list yet. "
                "Wait for the next poll cycle, or pass --live to spend a call."
            )
        return _report_collectors(factory, target, collectors, plants, apply=args.apply)

    rows: list[dict] = []
    error = None
    client, error = authenticated_client(
        calls=calls, connection_id=fusion.id, sync_run_id=run_id,
        purpose="huawei_scada_collector_discovery", credentials=credentials,
        client_factory=FusionSolarClient,
    )
    if client is not None:
        # The endpoint accepts up to 100 station codes, but a large query is
        # not necessarily a cheap one for the account: `failCode 407` came back
        # for a 61-code batch sixteen minutes after a smaller one succeeded.
        # Smaller batches cost more calls and may be the only ones that pass.
        size = max(1, min(100, args.batch_size))
        for start in range(0, len(codes), size):
            batch = codes[start : start + size]
            value, error = calls.call(
                connection_id=fusion.id, sync_run_id=run_id, endpoint_family="device_discovery",
                purpose="huawei_scada_collector_discovery",
                operation=lambda batch=batch: client.device_list_batch(batch),
            )
            if error:
                break
            rows.extend(value or [])

    with factory() as session:
        run = session.get(SyncRun, run_id)
        run.metadata_json = {"expected_items": len(codes), "items_received": len(rows)}
        finish_sync_run(
            session, run=run,
            status="success" if error is None else "failed",
            completeness="complete" if error is None else "none",
            error=error,
        )
        session.commit()

    if error is not None:
        raise SystemExit(f"FusionSolar refused the device list: {error.code.value} — {error.safe_message}")

    found = collectors_in(rows, asset_by_station={code: asset[0] for code, asset in plants.items()})
    collectors = [
        {
            "kind": item.kind, "serial": item.serial, "station": item.station_code,
            "asset_id": item.asset_id, "dev_type_id": item.dev_type_id, "model": item.model,
        }
        for item in found
    ]
    print(f"Dispositivos devolvidos: {len(rows)}")
    return _report_collectors(factory, target, collectors, plants, apply=args.apply)


def _collectors_from_last_sync(factory, fusionsolar_connection_id: int) -> list[dict] | None:
    """The collectors the last successful device-status poll happened to see."""
    with factory() as session:
        runs = session.scalars(
            select(SyncRun)
            .where(
                SyncRun.provider_connection_id == fusionsolar_connection_id,
                SyncRun.capability == "device_discovery",
            )
            .order_by(SyncRun.id.desc())
            .limit(40)
        )
        for run in runs:
            payload = run.metadata_json or {}
            if payload.get("collectors"):
                print(f"A ler da sincronização {run.id} de {run.started_at:%Y-%m-%d %H:%M} — zero chamadas à API.")
                return list(payload["collectors"])
    return None


def _report_collectors(factory, target, collectors: list[dict], plants: dict, *, apply: bool) -> int:
    print(f"Colectores encontrados: {len(collectors)}")
    by_kind: dict[str, int] = {}
    for item in collectors:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
    for kind, count in sorted(by_kind.items()):
        print(f"  {kind}: {count}")
    # The type ids these classes actually use, learned rather than assumed.
    observed = sorted({str(item["dev_type_id"]) for item in collectors})
    print(f"  devTypeId observados nos colectores: {', '.join(observed) or '—'}")

    print()
    names = {asset_id: name for asset_id, name in plants.values()}
    for item in sorted(collectors, key=lambda row: (row["kind"], str(row.get("asset_id") or ""))):
        item["asset_name"] = names.get(item.get("asset_id"))
        problem = "SEM SÉRIE" if not item["serial"] else ("SEM CENTRAL" if item["asset_id"] is None else "")
        print(f"  {item['kind']:<7} {item['serial'] or '(?)':<20} {str(item['model'] or ''):<18} "
              f"asset={str(item['asset_id'] or '?'):<5} {(item['asset_name'] or '')[:32]:<34} {problem}")

    usable = [item for item in collectors if item["serial"] and item["asset_id"]]
    print(f"\nMapeáveis sem escrever um número de série: {len(usable)} de {len(collectors)}")

    if not apply:
        print("\nEnsaio. Nada foi criado. Repetir com --apply para criar os mappings.")
        return 0

    created = reused = 0
    with factory() as session, session.begin():
        for item in usable:
            normalized = normalize_external_id(ProviderCode.HUAWEI_SCADA, item["serial"])
            claimed = ProviderRepository(session).active_external_claim(
                connection_id=target.id, normalized_external_id=normalized, resource_kind="plant"
            )
            if claimed is not None:
                reused += 1
                continue
            create_mapping(
                session, asset_id=item["asset_id"], provider_connection_id=target.id,
                external_id=item["serial"], external_name=item.get("model"),
            )
            created += 1
    # Deliberately no source policies: the FusionSolar mapping stays the
    # asset's production and monitoring source, and an active mapping is
    # already enough for samples to be collected.
    print(f"\nMappings criados: {created}   já existentes: {reused}   (sem políticas de origem)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-connection", help="Create the huawei_scada provider connection.")
    create.add_argument("--key", default="scada-pilot")
    create.add_argument("--name", default="Huawei SCADA")
    create.add_argument("--credential-reference", default="primary")
    create.set_defaults(handler=command_create_connection)

    status = sub.add_parser("status", help="Pending dongles, bound plants, open sessions.")
    status.add_argument("--connection-id", type=int)
    status.set_defaults(handler=command_status)

    bind = sub.add_parser("bind", help="Bind an announced serial to a plant.")
    bind.add_argument("--connection-id", type=int)
    bind.add_argument("--serial", required=True)
    bind.add_argument("--asset-id", type=int, required=True)
    bind.add_argument("--dongle-name")
    bind.add_argument("--valid-from", help="ISO date; defaults to today.")
    bind.add_argument("--monitoring", action="store_true", help="Make this the plant's monitoring source.")
    bind.add_argument("--production", action="store_true", help="Make this the plant's production source.")
    bind.add_argument("--fallback", action="store_true", help="Create the policies as fallback, not primary.")
    bind.add_argument("--priority", type=int, default=1)
    bind.add_argument("--actor", default="operator")
    bind.set_defaults(handler=command_bind)

    discover = sub.add_parser(
        "discover", help="Derive dongle/logger serials from FusionSolar's own device list."
    )
    discover.add_argument("--connection-id", type=int, help="The huawei_scada connection to create mappings on.")
    discover.add_argument("--fusionsolar-connection-id", type=int)
    discover.add_argument("--om-only", action="store_true", help="Restrict to assets with an O&M contract in force.")
    discover.add_argument("--apply", action="store_true", help="Actually create the mappings (default: dry run).")
    discover.add_argument("--batch-size", type=int, default=100, help="Station codes per getDevList call (1-100).")
    discover.add_argument("--limit", type=int, help="Only look at the first N plants (for a cheap probe).")
    discover.add_argument(
        "--live", action="store_true",
        help="Spend a getDevList call instead of reading the last poll's own list (rate limited to once an hour).",
    )
    discover.set_defaults(handler=command_discover)

    reject = sub.add_parser("reject", help="Mark a pending serial as rejected; it stays rejected.")
    reject.add_argument("--serial", required=True)
    reject.add_argument("--reason", required=True)
    reject.set_defaults(handler=command_reject)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
