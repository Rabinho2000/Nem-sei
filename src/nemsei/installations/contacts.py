"""Who to call about one installation -- read/write, never invented.

Telegram O&M redesign, req 8: an alert must be able to show a local contact
when one is operationally useful, and say "não registado" honestly when it
is not. This module is the only place that decides which contact a renderer
should lead with (`primary_or_first_contact`); the renderer itself never
touches `InstallationContact` rows directly.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nemsei.installations.models import CONTACT_TYPES, InstallationContact
from nemsei.shared.clock import utc_now


def required_text(value: str | None, field: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{field} is required.")
    return text


def add_contact(
    session: Session,
    *,
    installation_id: int,
    name: str,
    created_by: str,
    role: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    contact_type: str = "other",
    is_primary: bool = False,
    notes: str | None = None,
) -> InstallationContact:
    if contact_type not in CONTACT_TYPES:
        raise ValueError("Invalid contact type.")
    phone_value = (phone or "").strip() or None
    email_value = (email or "").strip() or None
    if phone_value is None and email_value is None:
        raise ValueError("A contact needs a phone or an email -- otherwise it is not reachable.")
    now = utc_now()
    contact = InstallationContact(
        installation_id=installation_id,
        name=required_text(name, "Contact name"),
        role=(role or "").strip() or None,
        phone=phone_value,
        email=email_value,
        contact_type=contact_type,
        is_primary=is_primary,
        notes=(notes or "").strip() or None,
        created_by=required_text(created_by, "created_by"),
        created_at=now,
        updated_at=now,
    )
    session.add(contact)
    session.flush()
    return contact


def contacts_for_installation(session: Session, *, installation_id: int) -> list[InstallationContact]:
    """Primary first, then whatever order they were recorded in."""
    return list(
        session.scalars(
            select(InstallationContact)
            .where(InstallationContact.installation_id == installation_id)
            .order_by(InstallationContact.is_primary.desc(), InstallationContact.id)
        )
    )


def contacts_for_installations(
    session: Session, *, installation_ids: list[int]
) -> dict[int, list[InstallationContact]]:
    """Batch form of `contacts_for_installation`, for a briefing/digest pass
    over many installations at once -- one query, not one per installation."""
    if not installation_ids:
        return {}
    rows = session.scalars(
        select(InstallationContact)
        .where(InstallationContact.installation_id.in_(installation_ids))
        .order_by(InstallationContact.installation_id, InstallationContact.is_primary.desc(), InstallationContact.id)
    )
    result: dict[int, list[InstallationContact]] = {installation_id: [] for installation_id in installation_ids}
    for contact in rows:
        result[contact.installation_id].append(contact)
    return result


def primary_or_first_contact(session: Session, *, installation_id: int) -> InstallationContact | None:
    """The one contact a Telegram message should lead with, or `None`.

    `None` is a real, common answer -- most installations have never had a
    contact recorded -- and the renderer is required to say "não registado"
    for it, never to fall back to a guessed or blank number.
    """
    contacts = contacts_for_installation(session, installation_id=installation_id)
    return contacts[0] if contacts else None


def format_contact(contact: InstallationContact | None) -> str:
    """The compact `"Nome · Papel\\nTelefone"` shape the Telegram renderer
    embeds verbatim -- kept here so every caller (immediate alert, reminder,
    morning briefing) renders a contact identically."""
    if contact is None:
        return "não registado"
    label = contact.name
    if contact.role:
        label = f"{contact.name} · {contact.role}"
    lines = [label]
    if contact.phone:
        lines.append(contact.phone)
    if contact.email:
        lines.append(contact.email)
    return "\n".join(lines)


def contact_summary(contact: InstallationContact | None) -> dict[str, Any]:
    """Structured form, for callers that build a message piece by piece
    instead of embedding `format_contact`'s pre-joined text."""
    if contact is None:
        return {"registered": False, "name": None, "role": None, "phone": None, "email": None}
    return {
        "registered": True,
        "name": contact.name,
        "role": contact.role,
        "phone": contact.phone,
        "email": contact.email,
        "contact_type": contact.contact_type,
    }
