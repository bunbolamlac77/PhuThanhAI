"""SQLite WAL registry with versioned migrations.

Design constraints from the specification:

* seven operational tables, no speculative schema;
* WAL + ``busy_timeout`` + explicit ``BEGIN IMMEDIATE`` write transactions;
* ``PRAGMA user_version`` drives migrations, which must be idempotent;
* one writer at a time, many readers allowed (SQLite WAL semantics);
* a connection per thread, because FastAPI runs sync endpoints in a threadpool;
* backups use SQLite's consistent backup API, never a raw file copy.

Runtime data lives in ``~/Library/Application Support/PhuThanhAI/registry.sqlite3``,
never in the source tree.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 2

_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(name) > 0),
    root_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    filename TEXT NOT NULL,
    sort_key TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    dev INTEGER,
    ino INTEGER,
    duration_ms INTEGER,
    width INTEGER,
    height INTEGER,
    rotation INTEGER NOT NULL DEFAULT 0,
    fps_nominal TEXT,
    codec TEXT,
    has_audio INTEGER NOT NULL DEFAULT 0,
    vfr_hint INTEGER NOT NULL DEFAULT 0,
    color_transfer TEXT,
    preview_status TEXT NOT NULL DEFAULT 'unprobed',
    hash_status TEXT NOT NULL DEFAULT 'none',
    analysis_status TEXT NOT NULL DEFAULT 'not_started',
    source_state TEXT NOT NULL DEFAULT 'available',
    sha256 TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (project_id, path)
);
CREATE INDEX IF NOT EXISTS idx_assets_project_sort ON assets(project_id, sort_key);
CREATE INDEX IF NOT EXISTS idx_assets_project_filename ON assets(project_id, filename);
CREATE INDEX IF NOT EXISTS idx_assets_state ON assets(project_id, source_state);
CREATE INDEX IF NOT EXISTS idx_assets_analysis ON assets(project_id, analysis_status);

CREATE TABLE IF NOT EXISTS annotations (
    annotation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    asset_id TEXT REFERENCES assets(asset_id) ON DELETE CASCADE,
    scope TEXT NOT NULL CHECK (scope IN ('project', 'clip', 'time_range')),
    start_ms INTEGER,
    end_ms INTEGER,
    text TEXT NOT NULL,
    note_type TEXT NOT NULL DEFAULT 'context',
    source TEXT NOT NULL DEFAULT 'user_annotation',
    assertion_status TEXT NOT NULL DEFAULT 'user_asserted',
    wall_time_of_day TEXT,
    wall_date TEXT,
    wall_timezone TEXT,
    wall_precision TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_annotations_project ON annotations(project_id, asset_id);
CREATE INDEX IF NOT EXISTS idx_annotations_updated ON annotations(project_id, updated_at);

CREATE TABLE IF NOT EXISTS review_overlays (
    asset_id TEXT PRIMARY KEY REFERENCES assets(asset_id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'unreviewed',
    rating INTEGER,
    favorite INTEGER NOT NULL DEFAULT 0,
    labels TEXT NOT NULL DEFAULT '[]',
    selected_ranges TEXT NOT NULL DEFAULT '[]',
    last_playhead_ms INTEGER,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviews_project ON review_overlays(project_id, status);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    asset_id TEXT,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_project_state ON jobs(project_id, state);

CREATE TABLE IF NOT EXISTS cache_entries (
    path TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    atime INTEGER NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    source_sha256 TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_kind_atime ON cache_entries(kind, atime);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def natural_key(value: str) -> str:
    """Deterministic sortable key for filenames with embedded numbers.

    ``C0015`` sorts before ``C0016``; ``DJI_0002`` before ``DJI_0010``. Numbers are
    zero-padded so plain binary text comparison in SQLite gives natural order.
    """

    import re

    parts = re.split(r"(\d+)", value.lower())
    return "\x00".join(part.zfill(12) if part.isdigit() else part for part in parts)


def _run_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute a DDL script inside the caller's transaction.

    ``sqlite3.executescript`` issues an implicit COMMIT before running, which would
    silently end our ``BEGIN IMMEDIATE`` and make the surrounding COMMIT fail with
    "no transaction is active". These statements contain no semicolons inside
    literals, so splitting on ``;`` is safe and keeps the migration atomic.
    """

    for statement in script.split(";"):
        stripped = statement.strip()
        if stripped:
            conn.execute(stripped)


def _migration_v1(conn: sqlite3.Connection) -> None:
    _run_script(conn, _BASE_SCHEMA)
    # Register the collation so ad-hoc ORDER BY on filename is also natural.
    try:
        conn.create_collation("natural", lambda a, b: (natural_key(a) > natural_key(b)) - (natural_key(a) < natural_key(b)))
    except sqlite3.ProgrammingError:
        pass


def _migration_v2(conn: sqlite3.Connection) -> None:
    """S02: sampling state on assets (internal schema only, not portable)."""

    for statement in (
        "ALTER TABLE assets ADD COLUMN sampling_status TEXT NOT NULL DEFAULT 'not_sampled'",
        "ALTER TABLE assets ADD COLUMN sampling_profile TEXT",
        "ALTER TABLE assets ADD COLUMN sampling_fingerprint TEXT",
        "ALTER TABLE assets ADD COLUMN sampled_at TEXT",
    ):
        conn.execute(statement)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_assets_sampling ON assets(project_id, sampling_status)")


MIGRATIONS = (_migration_v1, _migration_v2)


class Database:
    """Thread-local connections over one WAL database file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path),
            timeout=10.0,
            isolation_level=None,  # explicit transactions only
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.create_collation(
            "natural",
            lambda a, b: (natural_key(a) > natural_key(b)) - (natural_key(a) < natural_key(b)),
        )
        return conn

    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        yield self.connection()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Serialise writers; SQLite allows only one writer even in WAL mode."""

        with self._write_lock:
            conn = self.connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def migrate(self) -> int:
        with self.write() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            for index, migration in enumerate(MIGRATIONS[version:], start=version + 1):
                migration(conn)
                conn.execute(f"PRAGMA user_version = {int(index)}")
                version = index
            return version

    def schema_version(self) -> int:
        return int(self.connection().execute("PRAGMA user_version").fetchone()[0])

    def backup_to(self, destination: Path) -> Path:
        """Consistent online backup via SQLite's backup API."""

        destination.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(destination))
        try:
            self.connection().backup(target)
        finally:
            target.close()
        return destination

    def close_current_thread(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
