from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from nemsei.config import Settings
from nemsei.db.base import Base
import nemsei.jobs.models  # noqa: F401 - register foundation metadata
import nemsei.assets.models  # noqa: F401 - register asset metadata
import nemsei.providers.models  # noqa: F401 - register provider metadata


config = context.config
settings = Settings.from_environment().validate()
config.set_main_option("sqlalchemy.url", settings.sqlalchemy_database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=settings.sqlalchemy_database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    settings.data_root.mkdir(parents=True, exist_ok=True)
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
