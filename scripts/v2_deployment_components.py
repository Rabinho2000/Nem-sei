#!/usr/bin/env python3
"""Which components a canonical V2 deployment is made of, and whether they arrived.

`docker compose` has no notion of a deployment that is incomplete. Leave a
`-f` off and it renders a perfectly valid configuration that happens to be
missing an override, recreates the services from it, and reports success. That
is how the diagnostic-incident evaluator was switched off twice without an
error anywhere: on 2026-08-21 for about twelve minutes, and again some time
before 2026-08-31, when it had been off for hours and the interface still said
the automation was "a correr".

So the list of components stops being something an operator carries in their
head. `deploy/v2_deployment_components.json` names them, this module reads it,
and `scripts/v2_compose_up.sh` builds its compose invocation from the result.

The manifest also carries assertions, because naming a file only proves compose
was told about it. `check_rendered` reads the merged configuration compose is
about to apply, and `check_live` reads the environment of the container that is
actually running afterwards. A component that is declared but did not reach the
process fails the deploy, loudly, while someone is still watching.

Nothing here ever prints a rendered configuration or a container environment:
those carry the admin password hash, the secret key, and every secret path.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

MANIFEST_PATH = "deploy/v2_deployment_components.json"


class DeploymentComponentError(RuntimeError):
    """Raised when the deployment is not made of the components it declares."""


@dataclass(frozen=True)
class Assertion:
    service: str
    variable: str
    value: str


@dataclass(frozen=True)
class Component:
    name: str
    compose_file: str
    required: bool
    asserts: tuple[Assertion, ...]


@dataclass(frozen=True)
class Manifest:
    base_compose_file: str
    components: tuple[Component, ...]

    @property
    def required_components(self) -> tuple[Component, ...]:
        return tuple(component for component in self.components if component.required)

    def compose_files(self) -> tuple[str, ...]:
        """The base file first, then every required overlay, in manifest order.

        Order is the merge order, so it is part of the contract rather than an
        implementation detail: a later file's `environment` wins.
        """
        return (self.base_compose_file, *(component.compose_file for component in self.required_components))

    def assertions(self) -> tuple[Assertion, ...]:
        return tuple(assertion for component in self.required_components for assertion in component.asserts)


def _assertion(raw: dict, *, component: str) -> Assertion:
    missing = [key for key in ("service", "variable", "value") if not str(raw.get(key) or "").strip()]
    if missing:
        raise DeploymentComponentError(
            f"Component '{component}' has an assertion missing {', '.join(missing)}."
        )
    return Assertion(service=str(raw["service"]), variable=str(raw["variable"]), value=str(raw["value"]))


def load_manifest(root: Path) -> Manifest:
    """Read and validate the manifest, refusing anything it cannot act on.

    Every declared compose file must exist on disk. A manifest that names a
    file the checkout does not have would otherwise fail much later, inside
    compose, with an error about a path rather than about a component.
    """
    path = root / MANIFEST_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DeploymentComponentError(f"The deployment manifest is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DeploymentComponentError(f"The deployment manifest is not valid JSON: {exc}") from exc

    base = str(raw.get("base_compose_file") or "").strip()
    if not base:
        raise DeploymentComponentError("The deployment manifest must name a base_compose_file.")

    components: list[Component] = []
    seen: set[str] = set()
    for entry in raw.get("components") or []:
        name = str(entry.get("name") or "").strip()
        compose_file = str(entry.get("compose_file") or "").strip()
        if not name or not compose_file:
            raise DeploymentComponentError("Every component needs a name and a compose_file.")
        if name in seen:
            raise DeploymentComponentError(f"Component '{name}' is declared more than once.")
        seen.add(name)
        if "required" not in entry:
            raise DeploymentComponentError(
                f"Component '{name}' must say whether it is required; there is no default."
            )
        components.append(
            Component(
                name=name,
                compose_file=compose_file,
                required=bool(entry["required"]),
                asserts=tuple(_assertion(item, component=name) for item in entry.get("asserts") or []),
            )
        )

    manifest = Manifest(base_compose_file=base, components=tuple(components))
    for compose_file in (base, *(component.compose_file for component in manifest.components)):
        if not (root / compose_file).is_file():
            raise DeploymentComponentError(f"The manifest names a compose file that does not exist: {compose_file}")
    return manifest


def _rendered_environment(config: dict, service: str) -> dict:
    services = config.get("services") or {}
    if service not in services:
        raise DeploymentComponentError(
            f"The rendered configuration has no '{service}' service, so its components cannot be checked."
        )
    return (services[service] or {}).get("environment") or {}


def check_rendered(config: dict, manifest: Manifest) -> None:
    """Every required component reached the configuration compose will apply."""
    failures = []
    for assertion in manifest.assertions():
        actual = _rendered_environment(config, assertion.service).get(assertion.variable)
        if actual is None:
            failures.append(f"{assertion.service} is missing {assertion.variable}")
        elif str(actual) != assertion.value:
            # The expected value is safe to print: these are declared switches,
            # never a credential. The actual value is not printed, because a
            # future assertion could name something that is.
            failures.append(f"{assertion.service}'s {assertion.variable} is not {assertion.value!r}")
    if failures:
        raise DeploymentComponentError(
            "This deployment is missing components it declares: " + "; ".join(failures)
        )


def check_live(observations: dict[tuple[str, str], str | None], manifest: Manifest) -> None:
    """The same assertions, against the container that is actually running.

    `check_rendered` proves compose was told the right thing. This proves the
    process was recreated from it -- the case that matters when a service was
    already up with an older environment and nothing about the new
    configuration forced it to restart.
    """
    failures = []
    for assertion in manifest.assertions():
        actual = observations.get((assertion.service, assertion.variable))
        if actual is None:
            failures.append(f"{assertion.service} is running without {assertion.variable}")
        elif actual != assertion.value:
            failures.append(f"{assertion.service} is running with {assertion.variable} not set to {assertion.value!r}")
    if failures:
        raise DeploymentComponentError(
            "The running deployment does not match its declared components: " + "; ".join(failures)
        )


def read_observations(text: str) -> dict[tuple[str, str], str | None]:
    """Parse `service<TAB>variable<TAB>value` lines, one per assertion.

    A variable the container does not define is reported as a line with no
    value rather than as a missing line, so a caller that forgets to ask about
    something cannot be mistaken for a container that answered "unset".
    """
    observations: dict[tuple[str, str], str | None] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) == 2:
            service, variable, value = parts[0], parts[1], None
        elif len(parts) == 3:
            service, variable, value = parts[0], parts[1], parts[2]
        else:
            raise DeploymentComponentError("Each observation must be service, variable and value, tab separated.")
        observations[(service, variable)] = value
    return observations


def _print_compose_files(manifest: Manifest) -> None:
    for compose_file in manifest.compose_files():
        print(compose_file)


def _print_assertions(manifest: Manifest) -> None:
    for assertion in manifest.assertions():
        print(f"{assertion.service}\t{assertion.variable}\t{assertion.value}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository root holding the manifest.")
    parser.add_argument(
        "action",
        choices=("compose-files", "assertions", "check-rendered", "check-live"),
        help=(
            "compose-files: one path per line, in merge order. "
            "assertions: service, variable and expected value, tab separated. "
            "check-rendered: validate $RENDERED_COMPOSE. "
            "check-live: validate observations read from stdin as service\\tvariable\\tvalue lines."
        ),
    )
    args = parser.parse_args()
    try:
        manifest = load_manifest(args.root)
        if args.action == "compose-files":
            _print_compose_files(manifest)
        elif args.action == "assertions":
            _print_assertions(manifest)
        elif args.action == "check-rendered":
            rendered = os.environ.get("RENDERED_COMPOSE")
            if not rendered:
                raise DeploymentComponentError("No rendered compose configuration was supplied; set RENDERED_COMPOSE.")
            check_rendered(json.loads(rendered), manifest)
            print(f"{len(manifest.assertions())} declared component settings are in the rendered configuration")
        else:
            check_live(read_observations(sys.stdin.read()), manifest)
            print(f"{len(manifest.assertions())} declared component settings are live in the running containers")
    except (DeploymentComponentError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
