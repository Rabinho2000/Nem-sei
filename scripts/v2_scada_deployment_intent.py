#!/usr/bin/env python3
"""Decide whether this deployment declares the Huawei SCADA listener.

The listener is the one inbound port this system opens, so starting it can
never be a side effect of deploying. It also cannot be left behind: a
deployment that *is* the SCADA pilot and silently ships without its listener
loses readings for as long as nobody notices, and nothing else in the stack
reports the gap.

The signal is `NEMSEI_V2_HUAWEI_SCADA_CONNECTION_ID`, which compose renders
into the service as `NEMSEI_V2_HUAWEI_SCADA_LISTENER_CONNECTION_ID`. It is
already load-bearing rather than decorative: `Settings.validate` refuses an
enabled listener without it, because there is no portfolio-wide mode. Reusing
it means there is no second switch to keep in agreement with the first.

The decision reads the *rendered* compose configuration rather than the env
file, so it resolves defaults and substitutions with exactly the rules the
deployment itself uses. Nothing here prints the configuration it was given:
that document carries the admin hash and the secret key.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

SERVICE = "scada-listener"
PROFILE = "huawei-scada"
CONNECTION_ID_KEY = "NEMSEI_V2_HUAWEI_SCADA_LISTENER_CONNECTION_ID"
LISTENER_PORT = 1502
# An empty host_ip is compose's own "every interface", so it is the same
# answer as naming the wildcard explicitly.
WIDE_OPEN_HOSTS = {"", "0.0.0.0", "::"}


class ScadaDeploymentError(RuntimeError):
    """Raised when the SCADA deployment shape cannot be trusted to start."""


def listener_service(config: dict) -> dict | None:
    return (config.get("services") or {}).get(SERVICE)


def declared_connection_id(service: dict) -> str:
    environment = service.get("environment") or {}
    return str(environment.get(CONNECTION_ID_KEY) or "").strip()


def services_publishing_listener_port(config: dict) -> list[str]:
    names = []
    for name, service in (config.get("services") or {}).items():
        for port in service.get("ports") or []:
            if int(port.get("target") or 0) == LISTENER_PORT:
                names.append(name)
                break
    return sorted(names)


def check_exactly_one_listener(config: dict) -> None:
    """One process holds the port, and one process holds the advisory lock.

    The lock in `huawei_scada/listener.py` is the real guarantee, but it turns
    a second listener into a refusal at runtime. Catching it here means the
    deploy refuses instead, while the first listener is still serving.
    """
    publishers = services_publishing_listener_port(config)
    if publishers != [SERVICE]:
        raise ScadaDeploymentError(
            f"Exactly one service may publish the SCADA port; found {publishers or 'none'}."
        )
    replicas = ((listener_service(config) or {}).get("deploy") or {}).get("replicas")
    if replicas is not None and int(replicas) != 1:
        raise ScadaDeploymentError(
            f"The SCADA listener runs exactly once; this configuration asks for {replicas}."
        )


def check_stays_behind_its_profile(service: dict) -> None:
    """The profile is what keeps an ordinary deployment from opening the port."""
    if PROFILE not in (service.get("profiles") or []):
        raise ScadaDeploymentError(
            f"The SCADA listener must stay behind the '{PROFILE}' profile; without it every "
            "deployment opens an inbound port."
        )


def check_bind_is_not_wide_open(service: dict) -> None:
    for port in service.get("ports") or []:
        if int(port.get("target") or 0) != LISTENER_PORT:
            continue
        if str(port.get("host_ip") or "").strip() in WIDE_OPEN_HOSTS:
            raise ScadaDeploymentError(
                "The SCADA listener would publish on every interface. Set "
                "NEMSEI_V2_SCADA_BIND_ADDRESS to the one address the logger reaches."
            )


def declares_scada(config: dict) -> bool:
    """True when this deployment intends to run the listener, after checking it can.

    A configuration that declares SCADA badly raises rather than answering
    False: silently degrading to "no listener" is the failure this whole change
    exists to remove.
    """
    service = listener_service(config)
    if service is None or not declared_connection_id(service):
        return False
    check_stays_behind_its_profile(service)
    check_exactly_one_listener(config)
    check_bind_is_not_wide_open(service)
    return True


def load_config(path: Path | None) -> dict:
    if path is not None:
        return json.loads(path.read_text(encoding="utf-8"))
    rendered = os.environ.get("SCADA_RENDERED_COMPOSE")
    if not rendered:
        raise ScadaDeploymentError(
            "No rendered compose configuration was supplied; set SCADA_RENDERED_COMPOSE."
        )
    return json.loads(rendered)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-json",
        type=Path,
        help="Rendered `docker compose config --format json`. Defaults to $SCADA_RENDERED_COMPOSE.",
    )
    args = parser.parse_args()
    try:
        declared = declares_scada(load_config(args.config_json))
    except (ScadaDeploymentError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(f"__NEMSEI_SCADA_DECLARED__={'true' if declared else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
