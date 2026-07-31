import os
import uuid
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app import models  # noqa: F401  ensure models are registered on Base


@pytest.fixture()
def db_engine(tmp_path):
    """A fresh file-backed SQLite DB per test (file-backed, not :memory:,
    so multiple threads/connections in the concurrency test see the same
    data -- :memory: databases are per-connection)."""
    db_path = tmp_path / f"test_{uuid.uuid4().hex}.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_connection, connection_record):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def SessionFactory(db_engine):
    return sessionmaker(bind=db_engine, autoflush=False, autocommit=False, future=True)


@pytest.fixture()
def db(SessionFactory):
    session = SessionFactory()
    yield session
    session.close()
