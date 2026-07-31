"""
Database setup.

We use SQLite for this exercise because it requires zero external
infrastructure to run/review. The concurrency-safety approach used
throughout the codebase (conditional UPDATE ... WHERE with a version
column, plus a DB-level partial-unique index on active bookings) is
standard SQL and works identically on Postgres/MySQL — swapping the
DATABASE_URL below to a Postgres DSN is the only change needed for
production. See README.md "Why this generalizes to Postgres".
"""
import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get("SCHEDULER_DB_URL", "sqlite:///./clinzo_scheduler.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)

if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        # WAL allows concurrent readers while a writer is active, and gives
        # us a busy_timeout so concurrent writers queue instead of
        # immediately raising "database is locked".
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
