"""Bloco D: an incident gets an owner and an outcome, without lying to the detector.

644 open incidents in production, 0 ever closed -- not because detection is
weak but because there was nowhere to say a person had dealt with one. The
handling dimension is deliberately separate from `status`: folding human states
into it would free the identity guarded by the partial unique index and let the
next evaluation open a duplicate for a condition that never went away.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from nemsei.app import create_app
from nemsei.assets.service import create_asset
from nemsei.db import build_engine, build_session_factory
from nemsei.diagnostics.handling import record_incident_handling
from nemsei.diagnostics.models import DiagnosticIncident, IncidentNote
from nemsei.shared.clock import utc_now
from tests_v2.test_migrations import upgrade


def seeded(settings, monkeypatch, *, status: str = "open"):
    upgrade(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()
    asset = create_asset(session, canonical_name="Central Incidente", timezone="Europe/Lisbon")
    session.flush()
    now = utc_now()
    incident = DiagnosticIncident(
        rule_code="stale_reading",
        asset_id=asset.id,
        device_id=None,
        severity="warning",
        status=status,
        opened_at=now,
        last_observed_at=now,
        resolved_at=now if status == "resolved" else None,
        occurrence_count=2,
        detector_version="1",
        evidence_json={"last_seen": "2026-07-16"},
        created_at=now,
        updated_at=now,
    )
    session.add(incident)
    session.commit()
    incident_id = incident.id
    session.close()
    client = create_app(settings).test_client()
    with client.session_transaction() as browser:
        browser["authenticated"], browser["username"], browser["csrf_token"] = True, "admin", "test"
    return client, incident_id


def reload(settings, incident_id: int) -> DiagnosticIncident:
    session = build_session_factory(build_engine(settings))()
    try:
        incident = session.get(DiagnosticIncident, incident_id)
        session.expunge(incident)
        return incident
    finally:
        session.close()


def notes(settings, incident_id: int) -> list[IncidentNote]:
    session = build_session_factory(build_engine(settings))()
    try:
        rows = list(session.scalars(select(IncidentNote).where(IncidentNote.incident_id == incident_id).order_by(IncidentNote.id)))
        for row in rows:
            session.expunge(row)
        return rows
    finally:
        session.close()


def test_an_untouched_incident_starts_new_and_unowned(settings, monkeypatch) -> None:
    _, incident_id = seeded(settings, monkeypatch)

    incident = reload(settings, incident_id)
    assert incident.handling_state == "new"
    assert incident.assigned_to is None
    assert incident.handling_updated_at is None


def test_taking_an_incident_records_state_owner_and_a_note(settings, monkeypatch) -> None:
    client, incident_id = seeded(settings, monkeypatch)

    response = client.post(
        f"/diagnostics/incidents/{incident_id}/handling",
        data={"csrf_token": "test", "handling_state": "investigating", "assigned_to": "Sérgio", "note": "Liguei ao cliente."},
    )

    assert response.status_code == 302
    incident = reload(settings, incident_id)
    assert (incident.handling_state, incident.assigned_to) == ("investigating", "Sérgio")
    assert incident.handling_updated_at is not None
    entry = notes(settings, incident_id)[0]
    assert (entry.author, entry.body, entry.handling_state_after) == ("admin", "Liguei ao cliente.", "investigating")


def test_marking_done_does_not_resolve_what_the_detector_still_sees(settings, monkeypatch) -> None:
    # The whole reason the two dimensions are separate. Resolving here would
    # free the identity and let the next evaluation open a duplicate.
    client, incident_id = seeded(settings, monkeypatch)

    client.post(f"/diagnostics/incidents/{incident_id}/handling", data={"csrf_token": "test", "handling_state": "done"})

    incident = reload(settings, incident_id)
    assert incident.handling_state == "done"
    assert incident.status == "open"
    assert incident.resolved_at is None
    page = client.get(f"/diagnostics/incidents/{incident_id}")
    assert "O detetor continua a ver esta condição." in page.text


def test_a_detector_resolved_incident_can_still_be_investigated(settings, monkeypatch) -> None:
    client, incident_id = seeded(settings, monkeypatch, status="resolved")

    client.post(f"/diagnostics/incidents/{incident_id}/handling", data={"csrf_token": "test", "handling_state": "investigating"})

    incident = reload(settings, incident_id)
    assert (incident.status, incident.handling_state) == ("resolved", "investigating")


def test_a_note_alone_is_recorded_without_moving_the_state(settings, monkeypatch) -> None:
    client, incident_id = seeded(settings, monkeypatch)

    client.post(f"/diagnostics/incidents/{incident_id}/handling", data={"csrf_token": "test", "handling_state": "new", "note": "Sem novidades."})

    incident = reload(settings, incident_id)
    assert incident.handling_state == "new"
    assert incident.handling_updated_at is None
    entry = notes(settings, incident_id)[0]
    assert entry.body == "Sem novidades."
    assert entry.handling_state_after is None


def test_an_empty_submission_changes_nothing(settings, monkeypatch) -> None:
    client, incident_id = seeded(settings, monkeypatch)

    client.post(f"/diagnostics/incidents/{incident_id}/handling", data={"csrf_token": "test", "handling_state": "new", "note": "   "})

    assert notes(settings, incident_id) == []


def test_the_handling_history_is_append_only(settings, monkeypatch) -> None:
    client, incident_id = seeded(settings, monkeypatch)

    for state, body in (("acknowledged", "Visto."), ("investigating", "A ver o inversor."), ("done", "Falso positivo.")):
        client.post(f"/diagnostics/incidents/{incident_id}/handling", data={"csrf_token": "test", "handling_state": state, "note": body})

    history = [(note.handling_state_after, note.body) for note in notes(settings, incident_id)]
    assert history == [("acknowledged", "Visto."), ("investigating", "A ver o inversor."), ("done", "Falso positivo.")]


def test_an_unknown_handling_state_is_refused(settings, monkeypatch) -> None:
    _, incident_id = seeded(settings, monkeypatch)
    session = build_session_factory(build_engine(settings))()

    with pytest.raises(ValueError, match="Estado de tratamento desconhecido"):
        record_incident_handling(session, incident_id=incident_id, actor="admin", handling_state="banana")
    session.close()


def test_the_list_summarises_the_backlog_and_filters_by_handling(settings, monkeypatch) -> None:
    client, incident_id = seeded(settings, monkeypatch)

    untouched = client.get("/diagnostics/incidents?om=todos&handling=untouched")
    assert "Central Incidente" in untouched.text
    assert "por triar" in untouched.text

    client.post(f"/diagnostics/incidents/{incident_id}/handling", data={"csrf_token": "test", "handling_state": "done"})

    assert "Central Incidente" not in client.get("/diagnostics/incidents?om=todos&handling=untouched").text
    assert "Central Incidente" in client.get("/diagnostics/incidents?om=todos&handling=done").text
