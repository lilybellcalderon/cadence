"""SQLite connection + schema management. Standard library only."""
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DEFAULT_DB = ROOT / "var" / "cadence.db"
SCHEMA_FILE = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 2


def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    """Autocommit connection (isolation_level=None); use transaction() for atomic multi-statement writes."""
    memory = str(path) == ":memory:"
    if not memory:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if not memory:
        conn.execute("PRAGMA journal_mode = WAL")   # operator UI reads while agents write
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE so two workers cannot interleave read-then-write decisions (e.g. claiming a task)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_schema(conn: sqlite3.Connection) -> None:
    """Create the schema on an empty database; refuse to touch one built by a different version."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if version != 0:
        raise RuntimeError(f"database schema is v{version}, code expects v{SCHEMA_VERSION}; migrate or reseed with --reset")
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def reset_files(path: str | Path) -> None:
    """Delete the database file, its WAL/SHM side files and the uploads stored beside it (local runtime data only)."""
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    shutil.rmtree(Path(path).parent / "uploads", ignore_errors=True)
