from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import Config


class Base(DeclarativeBase):
    pass


_engine = None
_session_factory = None


def _make_engine(url: str):
    kwargs = {"echo": False, "future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


def init_db(url: str | None = None):
    global _engine, _session_factory
    url = url or Config().DATABASE_URL
    if _engine is None:
        _engine = _make_engine(url)
        if url.startswith("sqlite"):

            @event.listens_for(_engine, "connect")
            def _set_sqlite_pragma(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.close()

        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def create_all():
    init_db()
    Base.metadata.create_all(_engine)
    _run_sqlite_migrations(_engine)

_MIGRATIONS = [
    ("leads", "plan", "TEXT"),
    ("leads", "components", "TEXT"),
    ("trades", "sl_order_type", "VARCHAR(8)"),
    ("trades", "lifecycle_stage", "VARCHAR(16) DEFAULT 'placed'"),
    ("trades", "sl_source", "VARCHAR(8)"),
    ("trades", "last_broker_check_at", "DATETIME"),
    ("trades", "closure_cause", "VARCHAR(32)"),
]   

def _run_sqlite_migrations(engine):
    if engine is None or not engine.url.drivername.startswith("sqlite"):
        return
    with engine.begin() as conn:
        existing = {
            r[0]
            for r in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for table, column, coltype in _MIGRATIONS:
            if table not in existing:
                continue
            cols = {r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def drop_all():
    init_db()
    Base.metadata.drop_all(_engine)


def dispose():
    """Drop engine/session globals (used by tests for isolation)."""
    global _engine, _session_factory
    _engine = None
    _session_factory = None


@contextmanager
def session_scope():
    init_db()
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Session:
    init_db()
    return _session_factory()