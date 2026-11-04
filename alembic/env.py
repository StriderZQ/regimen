"""Alembic: the schema comes from the models, the URL from the service configuration."""

from logging.config import fileConfig

from alembic import context

from app.config import get_settings
from app.db import build_engine
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
config.set_main_option("sqlalchemy.url", get_settings().database_url)


def render_item(type_, obj, autogen_context):
    """Render the custom UtcDateTime as a plain DateTime.

    Migrations must not import application code: a migration file outlives a refactor of
    the models, and physically in SQLite it is the same DATETIME anyway.
    """
    if type_ == "type" and obj.__class__.__name__ == "UtcDateTime":
        return "sa.DateTime()"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,  # SQLite cannot ALTER in place — batch mode is required
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations with foreign keys TURNED OFF.

    SQLite cannot alter a table in place: alembic recreates it — new table, copy the data,
    drop the old one. With foreign keys enabled that drop cascades into the children.

    That is how EVERY ingredient of every recipe vanished: a migration added a column to
    `recipes`, the old table was dropped, and `ON DELETE CASCADE` took
    `recipe_ingredients` with it. Silently, with a successful exit code.

    Turning the keys off for the migration is the standard SQLite approach: relations
    inside one migration are not checked, and checking returns after `upgrade`.
    """
    connectable = build_engine()
    with connectable.connect() as connection:
        # The PRAGMA opens a transaction, and alembic no longer owns it. Without the
        # explicit commit below, SQLAlchemy rolls it back when the connection closes: the
        # table was created while the schema version went backwards, and the next run died
        # on "table already exists". Observed in production.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            render_item=render_item,
        )
        try:
            with context.begin_transaction():
                context.run_migrations()
            connection.commit()
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            # If a migration did orphan rows after all, we must hear about it at once
            broken = connection.exec_driver_sql(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if broken:
                raise RuntimeError(
                    f"The migration left broken relations in {len(broken)} rows: "
                    f"{broken[:5]}"
                )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
