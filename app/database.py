import sqlite3
import threading
from contextlib import contextmanager
from typing import Generator
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from app.config import DATABASE_URL, DB_PATH

# SQLite Engine configuration with WAL and busy timeout
engine = create_engine(
    DATABASE_URL,
    connect_args={
        "check_same_thread": False,
        "timeout": 20.0
    },
    pool_pre_ping=True
)

@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA busy_timeout=20000;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# In-memory thread re-entrant lock to serialize critical write paths (such as scanning and returning)
# across multiple FastAPI threads, completely preventing SQLite concurrency lock contention.
SCAN_WRITE_LOCK = threading.RLock()

def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@contextmanager
def db_session() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def verify_database_state() -> dict:
    """
    Verifies database existence and health safely.
    Detects whether the installation is genuinely fresh, operational, or in an error state.
    Prevents destructive re-initialization if an existing database is locked or temporarily unavailable.
    """
    db_exists = DB_PATH.exists()
    file_size = DB_PATH.stat().st_size if db_exists else 0

    if db_exists and file_size > 0:
        try:
            with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10.0) as conn:
                res = conn.execute("PRAGMA integrity_check").fetchone()
                if not res or res[0] != "ok":
                    return {
                        "status": "CORRUPT",
                        "error": f"Database integrity check failed: {res[0] if res else 'Unknown error'}",
                        "db_path": str(DB_PATH)
                    }
                tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                if "users" in tables:
                    return {
                        "status": "OPERATIONAL",
                        "tables": tables,
                        "db_path": str(DB_PATH)
                    }
                else:
                    return {
                        "status": "INITIALIZING",
                        "tables": tables,
                        "db_path": str(DB_PATH)
                    }
        except sqlite3.OperationalError as ex:
            return {
                "status": "UNAVAILABLE",
                "error": f"Existing database file exists at {DB_PATH} but could not be accessed: {ex}",
                "db_path": str(DB_PATH)
            }
        except Exception as ex:
            return {
                "status": "UNAVAILABLE",
                "error": f"Database verification error: {ex}",
                "db_path": str(DB_PATH)
            }

    return {
        "status": "FRESH",
        "db_path": str(DB_PATH)
    }


