#!/usr/bin/env python3
"""What the collected samples say about the three values nobody may guess.

Meant to be run inside a V2 container. `Dockerfile.v2` does not copy `scripts/`
into the image, so mount it:

    docker compose -p nemsei-v2 -f docker-compose.v2.yml --env-file .env.v2 \
        --profile manual run --rm --no-deps \
        -v "$PWD/scripts:/app/scripts:ro" migrate \
        python /app/scripts/huawei_scada_verify_contract.py --days 3

`POWER_UNIT`, `PRODUCTION_SIGNAL` and `GRID_SIGN_CONVENTION` have no defaults
anywhere in this integration, which is right -- each of them, guessed wrong,
produces a number that looks entirely reasonable and is not. The cost of that
correctness is that an operator has three blanks and nothing obvious to fill
them from.

This reads samples the listener already persisted and reports the evidence.
It decides nothing and writes nothing: no environment variable is set here,
no fact is written, and a verdict of "unknown" is a normal, common answer that
means keep collecting.

The grid sign is the one physics settles outright. At night, with no PV and no
battery movement, a site with load *must* be importing; whichever sign the grid
register carries at those moments is the convention. So the useful first run of
this script is after a full night of collection, not after an afternoon.
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from sqlalchemy import select

from nemsei.assets.models import Asset
from nemsei.config import Settings
from nemsei.db import build_engine, build_session_factory
from nemsei.integrations.huawei_scada.contract_evidence import evidence_for
from nemsei.integrations.huawei_scada.rollup import current_samples
from nemsei.providers.models import ProviderConnection
from nemsei.providers.registry import ProviderCode
from nemsei.providers.repository import ProviderRepository
from nemsei.shared.clock import utc_now


def resolve_connection(session, connection_id: int | None) -> ProviderConnection:
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
    if len(found) != 1:
        raise SystemExit("Pass --connection-id: there is not exactly one huawei_scada connection.")
    return found[0]


def report(args) -> int:
    factory = build_session_factory(build_engine(Settings.from_environment()))
    until = utc_now()
    since = until - timedelta(days=args.days)
    settled_everywhere = True

    with factory() as session:
        connection = resolve_connection(session, args.connection_id)
        mappings = [
            mapping
            for mapping in ProviderRepository(session).current_mappings_for_connection(connection.id)
            if mapping.mapping_status == "active"
        ]
        if not mappings:
            raise SystemExit("No active mapping on this connection: nothing has been collected for any plant.")

        print(f"Janela: {since:%Y-%m-%d %H:%M} → {until:%Y-%m-%d %H:%M} UTC ({args.days} dia(s))")
        for mapping in mappings:
            asset = session.get(Asset, mapping.asset_id)
            samples = current_samples(
                session, provider_mapping_id=mapping.id, period_start=since, period_end=until
            )
            evidence = evidence_for(samples, installed_dc_power_kw=asset.installed_dc_power_kw)

            print(f"\n=== {asset.canonical_name} (asset {asset.id}, dongle {mapping.external_id})")
            print(f"    amostras na janela: {evidence.sample_count}")

            scale = evidence.power_scale
            print(f"\n  [POWER_UNIT] {scale.verdict or 'inconclusivo'}")
            print(f"    {scale.reason}")

            signal = evidence.production_signal
            print(f"\n  [PRODUCTION_SIGNAL] {signal.verdict or 'inconclusivo'}")
            print(f"    {signal.reason}")

            sign = evidence.grid_sign
            print(f"\n  [GRID_SIGN_CONVENTION] {sign.verdict or 'inconclusivo'}")
            print(f"    momentos qualificados (sem PV, sem bateria, com carga): {sign.quiet_samples}")
            print(f"    positivos: {sign.positive_while_importing}   negativos: {sign.negative_while_importing}")
            print(f"    {sign.reason}")
            if sign.verdict:
                reference = (connection.credential_reference or "primary").upper()
                print(f"\n    → NEMSEI_V2_HUAWEI_SCADA_{reference}_GRID_SIGN_CONVENTION={sign.verdict}")
                print("      Confirmar contra o contador real antes de o fixar: esta leitura é")
                print("      consistente com a física, mas o contador é a fonte de verdade.")
            else:
                settled_everywhere = False

    print("\n" + ("Todas as centrais têm evidência para a convenção de sinal."
                  if settled_everywhere else
                  "Pelo menos uma central ainda não tem evidência suficiente. Continuar a recolher."))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--connection-id", type=int)
    parser.add_argument("--days", type=int, default=3, help="Janela de análise em dias (por omissão 3).")
    return report(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
