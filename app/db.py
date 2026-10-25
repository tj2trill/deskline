from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker | None = None


def engine():
    global _engine, _SessionLocal
    if _engine is None:
        url = get_settings().database_url
        kwargs = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs = {"connect_args": {"check_same_thread": False}}
        _engine = create_engine(url, **kwargs)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def reset_engine() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def SessionLocal() -> Session:
    engine()
    assert _SessionLocal is not None
    return _SessionLocal()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Own session for background work (never reuse a request session)."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(engine())
    add_missing_columns()


def add_missing_columns() -> list[str]:
    """Additive upgrades: columns added in newer versions get ALTER TABLE … ADD COLUMN.
    (Renames/drops are never automatic.)"""
    from sqlalchemy import inspect, text

    eng = engine()
    insp = inspect(eng)
    added = []
    with eng.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = col.type.compile(dialect=eng.dialect)
                # Existing rows get the model's default instead of NULL (a NULL flag reads as "off").
                default = ""
                if col.default is not None and getattr(col.default, "is_scalar", False):
                    v = col.default.arg
                    if isinstance(v, bool):
                        default = " DEFAULT " + (("TRUE" if v else "FALSE") if eng.dialect.name == "postgresql" else str(int(v)))
                    elif isinstance(v, (int, float)):
                        default = f" DEFAULT {v}"
                    elif isinstance(v, str):
                        default = " DEFAULT '" + v.replace("'", "''") + "'"
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}{default}'))
                added.append(f"{table.name}.{col.name}")
    return added
