import logging
from typing import Callable, TypeVar

from sqlalchemy import create_engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker, declarative_base

from app.core.config import DATABASE_URL

logger = logging.getLogger(__name__)

# Neon (serverless Postgres) suspends idle computes and its proxy drops idle
# connections, so a pooled connection can be dead by the time we reuse it:
#   - pool_pre_ping validates a connection at checkout and transparently
#     reconnects a dead one.
#   - pool_recycle stays below Neon's idle timeout so we retire connections
#     before the proxy does it for us.
#   - TCP keepalives make the OS notice a half-open socket instead of blocking
#     until the default (~2h) system timeout.
#   - connect_timeout bounds a cold-start/unreachable-compute connect attempt
#     so a request fails fast instead of hanging.
# None of that covers a connection killed *after* a successful pre-ping and
# mid-query (compute suspend or restart lands in that window) — for that, see
# read_with_reconnect below.
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=180,
    connect_args={
        "connect_timeout": 10,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
        "application_name": "autogram",
    },
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def is_disconnect_error(exc: BaseException) -> bool:
    """True for a DB error SQLAlchemy recognised as a lost connection (as
    opposed to a query/constraint failure), meaning the connection was already
    invalidated and evicted from the pool."""
    return isinstance(exc, DBAPIError) and bool(exc.connection_invalidated)


T = TypeVar("T")


def read_with_reconnect(db: Session, query: Callable[[Session], T]) -> T:
    """Runs a **read-only** `query(db)`, retrying once on a lost connection.

    Only safe for reads: the retry re-runs `query` from scratch, so a write
    could apply twice. `rollback()` returns the invalidated connection to the
    pool, and the next statement checks out a fresh one.
    """
    try:
        return query(db)
    except DBAPIError as e:
        if not is_disconnect_error(e):
            raise
        logger.warning("Database connection dropped mid-query; retrying once on a fresh connection.")
        db.rollback()
        return query(db)
