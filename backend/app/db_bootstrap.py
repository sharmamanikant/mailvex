"""Prepare the database before the application services start.

The initial Alembic revision (``20260823_01``) builds the schema from the live
SQLAlchemy metadata instead of a frozen historical definition, so replaying the
revision chain against a brand-new database cannot work: the base revision
already materialises the current tables, and the later revisions then collide
with tables, columns and constraints that are already present. On a fresh
database 28 of the 43 revisions fail this way.

This module keeps the revision history intact and only intervenes where it is
actually needed:

* a genuinely empty database is created from the models and stamped at the
  current head, after which new revisions apply normally;
* any database that already carries application tables is left to the ordinary
  ``alembic upgrade head`` path;
* a database that has tables but no ``alembic_version`` table is ambiguous and
  is refused rather than guessed at.
"""

from __future__ import annotations

import logging
import os
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.core.database import engine
from app.models import Base, entities  # noqa: F401  (imported to register every model)

logger = logging.getLogger("db_bootstrap")

ALEMBIC_INI = "alembic.ini"


def _alembic_config() -> Config:
    """Build an Alembic config bound to the same database as the app engine."""
    config = Config(ALEMBIC_INI)
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        config.set_main_option("sqlalchemy.url", database_url)
    return config


def main() -> int:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    has_version_table = "alembic_version" in tables
    application_tables = tables - {"alembic_version"}

    if application_tables and not has_version_table:
        logger.error(
            "Refusing to continue: found %d table(s) but no alembic_version table. "
            "This database is not tracked by Alembic, so its schema state is "
            "unknown. Restore it from a backup or recreate it deliberately.",
            len(application_tables),
        )
        return 1

    config = _alembic_config()

    if application_tables:
        logger.info(
            "Existing database detected (%d tables); running alembic upgrade head.",
            len(application_tables),
        )
        command.upgrade(config, "head")
        logger.info("Database upgraded to head.")
        return 0

    logger.info("Empty database detected; creating the schema from the models.")
    Base.metadata.create_all(bind=engine, checkfirst=True)

    command.stamp(config, "head")
    logger.info("Schema created and stamped at head.")

    verify = set(inspect(engine).get_table_names())
    if not verify:
        logger.error("Bootstrap produced an empty schema; refusing to continue.")
        return 1
    logger.info("Bootstrap complete: %d table(s) present.", len(verify))
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    sys.exit(main())
