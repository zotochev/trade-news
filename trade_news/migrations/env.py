from alembic import context

from trade_news.db import make_engine, metadata

config = context.config


def run_migrations_online() -> None:
    engine = make_engine(config.get_main_option("sqlalchemy.url"))
    with engine.connect() as conn:
        context.configure(
            connection=conn,
            target_metadata=metadata,
            render_as_batch=True,  # SQLite can't ALTER most things; batch mode recreates tables
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    context.configure(
        url=config.get_main_option("sqlalchemy.url"), target_metadata=metadata, render_as_batch=True
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    run_migrations_online()
