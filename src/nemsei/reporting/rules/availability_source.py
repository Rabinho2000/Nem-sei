"""Where an availability number came from, and which one reporting may use.

Two different questions get answered here, deliberately in one place:

1. **What produced this percentage?** (`AVAILABILITY_SOURCES`) -- the concrete
   pipeline that computed it, kept as a free-standing vocabulary so a new
   provider contract adds a name here instead of a boolean somewhere.
2. **May it stand as a commercial figure?** (`AVAILABILITY_SOURCE_KINDS`) --
   the load-bearing distinction, and the reason this module exists at all.

V1 had both kinds and never separated them: its *slot* engine
(`reporting/availability.py` -> `plant_availability_daily`, fed by a daily
`/thirdData/getDevHistoryKpi` batch pull) produced the number its contracts
and monthly close actually used, while its *sampled* engine
(`services/sampled_availability.py` -> `plant_availability_sampled_daily`,
fed by the realtime poll) produced an operational indicator that only ever
reached an internal diagnostics panel. Nothing in V1's schema recorded which
was which -- they were kept apart only by which table a caller happened to
read. V2 records it, so a number cannot change meaning by being read from a
different place.

`fusionsolar_sampled` -- the only source V2 can actually produce today
(`diagnostics/availability_service.py`) -- is a port of the *sampled*
engine. It is therefore `operational`, and `SOURCE_KIND_BY_SOURCE` is the
single place that says so. `availability_daily`'s CHECK constraints enumerate
the same pairs at the database level, so an operational source is not merely
*conventionally* barred from being stored as contractual: the row is
rejected.
"""
from __future__ import annotations

from typing import Any

# What computed the number.
SOURCE_FUSIONSOLAR_SAMPLED = "fusionsolar_sampled"
SOURCE_FUSIONSOLAR_DEVICE_HISTORY = "fusionsolar_device_history"
SOURCE_PROVIDER_WAT = "provider_wat"
SOURCE_PROVIDER_DEVICE_AVAILABILITY = "provider_device_availability"
SOURCE_MANUAL = "manual"
AVAILABILITY_SOURCES = (
    SOURCE_FUSIONSOLAR_SAMPLED,
    SOURCE_FUSIONSOLAR_DEVICE_HISTORY,
    SOURCE_PROVIDER_WAT,
    SOURCE_PROVIDER_DEVICE_AVAILABILITY,
    SOURCE_MANUAL,
)

# Whether it may stand as a commercial figure.
KIND_CONTRACTUAL = "contractual"
KIND_OPERATIONAL = "operational"
AVAILABILITY_SOURCE_KINDS = (KIND_CONTRACTUAL, KIND_OPERATIONAL)

SOURCE_KIND_BY_SOURCE: dict[str, str] = {
    # Derived from a realtime poll: an operational indicator, never a WAT.
    # This is V1's sampled engine, and V1 never let it reach a customer
    # report either -- the difference is that here that is a stored fact
    # rather than an accident of which table the caller opened.
    SOURCE_FUSIONSOLAR_SAMPLED: KIND_OPERATIONAL,
    # Derived from `/thirdData/getDevHistoryKpi`: a dense 5-minute series for
    # a *closed* day, run through V1's slot engine
    # (`reporting/rules/availability_slots.py`). Contractual because that is
    # precisely the number V1's own contracts, Excel exports and monthly
    # close were built on -- same endpoint, same algorithm, same constants.
    #
    # Note the honesty boundary this name draws: FusionSolar does **not**
    # publish an availability or WAT figure anywhere. This is *derived from*
    # provider history, which is why it is not called `provider_wat` or
    # `provider_device_availability` -- those names are reserved for a
    # provider that actually states the number itself, and no provider V2
    # talks to does.
    SOURCE_FUSIONSOLAR_DEVICE_HISTORY: KIND_CONTRACTUAL,
    # A provider-published warranted-availability figure. No V2 provider
    # exposes one today (see `docs/v2/AVAILABILITY_MIGRATION_PLAN.md`); the
    # name exists so that wiring one is a writer change, not a schema change.
    SOURCE_PROVIDER_WAT: KIND_CONTRACTUAL,
    # A provider-published per-device availability series (V1's slot engine
    # shape: `/thirdData/getDevHistoryKpi`). Also unimplemented in V2.
    SOURCE_PROVIDER_DEVICE_AVAILABILITY: KIND_CONTRACTUAL,
    # An operator-entered figure, e.g. reconciled against an O&M contract
    # annex. Contractual by definition: a human asserted it commercially.
    SOURCE_MANUAL: KIND_CONTRACTUAL,
}

# Ordering *within* a kind, most authoritative first. Never across kinds --
# `select_availability` resolves kind first, so no operational source can be
# promoted past a contractual one by ranking high here.
_SOURCE_PRIORITY: dict[str, int] = {
    SOURCE_MANUAL: 0,
    SOURCE_PROVIDER_WAT: 1,
    SOURCE_PROVIDER_DEVICE_AVAILABILITY: 2,
    # Below a provider-stated figure (there is none today), above the
    # realtime estimate -- derived, but from a complete closed-day series.
    SOURCE_FUSIONSOLAR_DEVICE_HISTORY: 3,
    SOURCE_FUSIONSOLAR_SAMPLED: 4,
}


def source_kind(source: str) -> str:
    """The kind for a source name, raising on an unregistered one.

    Deliberately not `.get(source, KIND_OPERATIONAL)`: a source nobody
    classified is a bug to surface, not a value to default. Defaulting the
    other way (contractual) would be worse; defaulting either way hides that
    someone added a source and forgot to say what it means.
    """
    try:
        return SOURCE_KIND_BY_SOURCE[source]
    except KeyError:
        raise ValueError(f"unregistered availability source {source!r}") from None


def is_contractual(source: str) -> bool:
    return source_kind(source) == KIND_CONTRACTUAL


def select_availability(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the one availability figure reporting may present, deterministically.

    The policy, in full:

    1. Only candidates carrying a real `availability_pct` are eligible. A
       candidate with `None` is coverage evidence, not a figure -- it never
       wins and never blocks a different source that does have one.
    2. **Contractual outranks operational, always.** Not by recency, not by
       being computed more recently, not by having better coverage. This is
       the rule the whole module exists to enforce: a realtime-sampled
       number must never silently stand in for a warranted one.
    3. Within a kind, `_SOURCE_PRIORITY` decides, then the source name
       alphabetically -- so the outcome never depends on the order the
       caller happened to assemble the list in, which is what makes a report
       reproducible.

    Recency is deliberately absent from every tier. A sampled figure computed
    this morning does not outrank a contractual one computed last month; that
    inversion is exactly the "magic fallback that alters commercial meaning"
    this policy is here to prevent.

    Returns the winning candidate dict unchanged (callers read `source`,
    `source_kind` and `availability_pct` off it), or `None` when nothing is
    eligible -- never a fabricated zero.
    """
    eligible = [candidate for candidate in candidates if candidate.get("availability_pct") is not None]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda candidate: (
            0 if is_contractual(str(candidate["source"])) else 1,
            _SOURCE_PRIORITY.get(str(candidate["source"]), len(_SOURCE_PRIORITY)),
            str(candidate["source"]),
        ),
    )
