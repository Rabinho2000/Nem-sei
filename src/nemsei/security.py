"""Authentication primitives independent from V2 route modules."""
from __future__ import annotations

from werkzeug.security import check_password_hash

from nemsei.config import Settings


def verify_administrator_password(settings: Settings, username: str, password: str) -> bool:
    if username != settings.admin_username or not settings.admin_password_hash:
        return False
    try:
        return check_password_hash(settings.admin_password_hash, password)
    except ValueError:
        return False
