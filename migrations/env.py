from __future__ import annotations

from alembic import context

from nemsei.config import Settings
from nemsei.db.base import Base
from nemsei.db.engine import build_engine
import nemsei.jobs.models  # noqa: F401 - register foundation metadata
import nemsei.assets.models  # noqa: F401 - register asset metadata
import nemsei.providers.models  # noqa: F401 - register provider metadata
import nemsei.sync.models  # noqa: F401 - register sync metadata
import nemsei.monitoring.models  # noqa: F401 - register canonical fact metadata
import nemsei.sources.models  # noqa: F401 - register source policy metadata
import nemsei.reporting.models  # noqa: F401 - register reporting metadata
import nemsei.reporting.commercial_models  # noqa: F401 - register tariff and billing metadata
import nemsei.portfolios.models  # noqa: F401 - register portfolio metadata


config = context.config
settings = Settings.from_environment().validate()
config.set_main_option("sqlalchemy.url", settings.sqlalchemy_database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=settings.sqlalchemy_database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = build_engine(settings)
    try:
        with connectable.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
