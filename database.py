"""Connection handling. Short-lived connections (Supabase pooler friendly); no secrets in logs."""
from __future__ import annotations
import logging, os
from contextlib import contextmanager
import psycopg

log = logging.getLogger("stackwealth.db")


class DatabaseUnavailable(RuntimeError):
    pass


def get_database_url(secrets=None) -> str:
    url = None
    try:
        url = secrets.get("DATABASE_URL") if secrets is not None else None
    except Exception:
        pass
    url = url or os.environ.get("DATABASE_URL")
    if not url:
        raise DatabaseUnavailable("DATABASE_URL is not configured (Streamlit secrets or environment).")
    return url


@contextmanager
def connect(url: str):
    """Yields a connection; commits on success, rolls back on error, always closes."""
    try:
        conn = psycopg.connect(url, connect_timeout=10, autocommit=False)
    except psycopg.Error as e:
        log.error("database connection failed: %s", type(e).__name__)
        raise DatabaseUnavailable("Could not connect to the database. Check DATABASE_URL, "
                                  "use the Supabase pooler string, and ensure the project is not paused.") from None
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
