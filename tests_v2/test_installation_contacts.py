"""InstallationContact (Telegram O&M redesign, req 8, req 17).

Real Postgres, real constraints (`ck_installation_contacts_reachable` in
particular) -- not just the service layer's own validation.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError

from nemsei.db.session import build_session_factory
from nemsei.installations.contacts import (
    add_contact,
    contact_summary,
    contacts_for_installation,
    contacts_for_installations,
    format_contact,
    primary_or_first_contact,
)
from nemsei.installations.models import Installation


def upgrade(settings, monkeypatch) -> None:
    monkeypatch.setenv("NEMSEI_V2_ENV", "test")
    monkeypatch.setenv("NEMSEI_V2_DATABASE_URL", settings.database_url)
    command.upgrade(Config("alembic.ini"), "head")


def utc() -> datetime:
    return datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def factory(settings, monkeypatch):
    upgrade(settings, monkeypatch)
    return build_session_factory(create_engine(settings.database_url))


@pytest.fixture
def installation_id(factory):
    with factory() as session, session.begin():
        installation = Installation(display_name="DIACO", timezone_source="manual", created_at=utc(), updated_at=utc())
        session.add(installation)
        session.flush()
        return installation.id


# --- an installation with no contact recorded -----------------------------------


def test_an_installation_with_no_contact_renders_as_not_registered(factory, installation_id) -> None:
    with factory() as session:
        contact = primary_or_first_contact(session, installation_id=installation_id)
    assert contact is None
    assert format_contact(contact) == "não registado"
    assert contact_summary(contact) == {"registered": False, "name": None, "role": None, "phone": None, "email": None}


# --- an installation with a contact recorded ------------------------------------


def test_an_installation_with_a_contact_renders_its_details(factory, installation_id) -> None:
    with factory() as session, session.begin():
        add_contact(
            session, installation_id=installation_id, name="João Silva", role="Facilities",
            phone="+351 9xx xxx xxx", contact_type="facility_manager", is_primary=True, created_by="ops",
        )

    with factory() as session:
        contact = primary_or_first_contact(session, installation_id=installation_id)
    assert contact is not None
    assert contact.name == "João Silva"
    assert format_contact(contact) == "João Silva · Facilities\n+351 9xx xxx xxx"
    summary = contact_summary(contact)
    assert summary["registered"] is True and summary["phone"] == "+351 9xx xxx xxx"


def test_the_primary_contact_is_preferred_over_a_later_non_primary_one(factory, installation_id) -> None:
    with factory() as session, session.begin():
        add_contact(session, installation_id=installation_id, name="Segurança", phone="+351 1", created_by="ops")
        add_contact(
            session, installation_id=installation_id, name="João Silva", phone="+351 2",
            is_primary=True, created_by="ops",
        )

    with factory() as session:
        contact = primary_or_first_contact(session, installation_id=installation_id)
        all_contacts = contacts_for_installation(session, installation_id=installation_id)
    assert contact.name == "João Silva"
    assert len(all_contacts) == 2  # both still visible, not hidden by the primary


def test_batch_lookup_covers_installations_with_and_without_a_contact(factory, installation_id) -> None:
    with factory() as session, session.begin():
        other = Installation(display_name="Other plant", timezone_source="manual", created_at=utc(), updated_at=utc())
        session.add(other)
        session.flush()
        other_id = other.id
        add_contact(session, installation_id=installation_id, name="João Silva", phone="+351 9", created_by="ops")

    with factory() as session:
        by_installation = contacts_for_installations(session, installation_ids=[installation_id, other_id])
    assert len(by_installation[installation_id]) == 1
    assert by_installation[other_id] == []


def test_a_contact_needs_a_phone_or_an_email_to_be_recorded_at_all(factory, installation_id) -> None:
    with factory() as session, session.begin():
        with pytest.raises(ValueError):
            add_contact(session, installation_id=installation_id, name="Sem contacto", created_by="ops")


def test_the_database_itself_refuses_an_unreachable_contact(factory, installation_id) -> None:
    """The service layer's own validation is not the only guard -- proof
    against the real constraint, bypassing `add_contact`."""
    from nemsei.installations.models import InstallationContact

    with factory() as session, session.begin():
        session.add(
            InstallationContact(
                installation_id=installation_id, name="Sem contacto", contact_type="other",
                is_primary=False, created_by="ops", created_at=utc(), updated_at=utc(),
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
