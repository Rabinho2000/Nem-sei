from __future__ import annotations

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
