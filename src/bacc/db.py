"""SQLite connection handling and schema migration."""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2"

# Freshness bookkeeping lives in the metadata table, so it survives restarts and
# is shared by every process that opens the same database.
META_LAST_CHECK = 'last_check'
META_DEFER_UNTIL = 'defer_until'
DEFAULT_CHECK_INTERVAL = timedelta(hours=4)
DEFAULT_DEFER_ON_FAILURE = timedelta(minutes=10)
# Bad data fails identically when re-fetched, so back off much longer than for the network.
DEFAULT_DEFER_ON_INVALID = timedelta(hours=6)
# How long one process holds the right to refresh before others may try again.
CHECK_CLAIM_HOLD = timedelta(minutes=2)

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS remark_translations (
    original TEXT PRIMARY KEY,
    english TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual'
);
CREATE TABLE IF NOT EXISTS schools (
    school_id TEXT PRIMARY KEY,
    school_name TEXT NOT NULL,
    school_name_cn TEXT NOT NULL DEFAULT '',
    school_name_en TEXT NOT NULL DEFAULT '',
    logo TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS students (
    student_id TEXT PRIMARY KEY,
    given_en TEXT NOT NULL DEFAULT '',
    full_en TEXT NOT NULL DEFAULT '',
    school_id TEXT,
    FOREIGN KEY(school_id) REFERENCES schools(school_id)
);
CREATE TABLE IF NOT EXISTS variants (
    id INTEGER PRIMARY KEY,
    file_id TEXT NOT NULL,
    student_id TEXT NOT NULL,
    spine_id TEXT NOT NULL DEFAULT '',
    page_id TEXT NOT NULL DEFAULT '',
    given_en TEXT NOT NULL DEFAULT '',
    full_en TEXT NOT NULL DEFAULT '',
    skin_en TEXT NOT NULL DEFAULT '',
    spine_remark TEXT NOT NULL DEFAULT '',
    spine_remark_en TEXT NOT NULL DEFAULT '',
    display_name_en TEXT NOT NULL DEFAULT '',
    school_id TEXT NOT NULL DEFAULT '',
    school_name TEXT NOT NULL DEFAULT '',
    school_name_en TEXT NOT NULL DEFAULT '',
    source_json TEXT NOT NULL,
    FOREIGN KEY(student_id) REFERENCES students(student_id)
);
CREATE INDEX IF NOT EXISTS ix_variants_file_id ON variants(file_id COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_variants_student ON variants(student_id);
CREATE INDEX IF NOT EXISTS ix_variants_given ON variants(given_en COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_variants_display ON variants(display_name_en COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_variants_school ON variants(school_id);
'''

# Columns added after the first release; older databases are migrated in place.
_ADDED_COLUMNS = {
    'schools': ('school_name_cn', 'school_name_en', 'logo'),
    'variants': ('school_name_en',),
}


def initialize(con: sqlite3.Connection) -> None:
    """Create missing tables/indexes and add columns missing from older databases."""
    con.executescript(_SCHEMA)
    # Migrate databases created by older script versions without dropping data.
    for table, fields in _ADDED_COLUMNS.items():
        existing = {r['name'] for r in con.execute(f'PRAGMA table_info({table})')}
        for field in fields:
            if field not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")


def _enable_wal(con: sqlite3.Connection, timeout: float = 10.0) -> None:
    """Switch to WAL: any number of processes read while one writes, and readers
    keep seeing the previous snapshot until an import commits.

    The mode is stored in the file, so this only changes anything the first time.
    That one switch needs exclusive access and ignores ``busy_timeout``, so a
    second connection opening a brand-new database at the same moment is retried.
    SQLite does not raise when WAL is unavailable (an in-memory database, some
    filesystems); it just reports the mode it kept, and we carry on with that.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            mode = con.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower()
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
            continue
        if mode != 'wal':
            logger.warning('SQLite kept journal mode %r; concurrent readers may block writers', mode)
        return


def _open(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    try:
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA busy_timeout=10000')
        _enable_wal(con)
        con.execute('PRAGMA foreign_keys=ON')
        if con.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise sqlite3.DatabaseError('database disk image is malformed')
        initialize(con)
    except BaseException:
        con.close()
        raise
    return con


def _set_aside(path: Path) -> Path:
    """Move a damaged database (and its WAL files) out of the way, keeping it for inspection."""
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S')
    target = path.with_name(f'{path.name}.corrupt-{stamp}')
    for suffix in ('', '-wal', '-shm'):
        source = path.with_name(path.name + suffix)
        if source.exists():
            source.replace(target.with_name(target.name + suffix))
    return target


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) the catalog database with the schema applied.

    A database that is not valid SQLite is renamed to ``<name>.corrupt-<time>``
    and replaced by an empty one. Nothing is lost that cannot be rebuilt: the
    catalog is a cache of the upstream release, and the editable glossary lives
    in JSON. The next refresh repopulates it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _open(path)
    except sqlite3.DatabaseError as exc:
        # "not a database" / "malformed" are plain DatabaseError. Subclasses (locked,
        # read-only, I/O, integrity, programming errors) say nothing about the file.
        if type(exc) is not sqlite3.DatabaseError:
            raise
        moved = _set_aside(path)
        logger.warning('Catalog database %s is damaged (%s); moved to %s and starting fresh',
                       path, exc, moved)
        return _open(path)


def meta(con: sqlite3.Connection, key: str, default: str = '') -> str:
    """Read a value from the ``metadata`` key/value table."""
    row = con.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def set_meta(con: sqlite3.Connection, values: dict[str, str]) -> None:
    """Upsert metadata entries in the caller's transaction."""
    con.executemany(
        'INSERT INTO metadata(key,value) VALUES (?,?) '
        'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        values.items(),
    )


def _timestamp(value: str) -> datetime | None:
    """Parse a stored ISO timestamp; naive values are assumed to be UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def should_check(con: sqlite3.Connection, interval: timedelta) -> bool:
    """Whether the upstream catalog is due for a freshness check.

    A failed check parks a short defer window in the metadata table so a server
    that is down does not get hit again on every query; the window is honoured
    before the regular interval.
    """
    now = datetime.now(UTC)
    defer_until = _timestamp(meta(con, META_DEFER_UNTIL))
    if defer_until is not None and now < defer_until:
        return False
    last_check = _timestamp(meta(con, META_LAST_CHECK))
    return last_check is None or now - last_check > interval


def claim_check(
    con: sqlite3.Connection, interval: timedelta, hold: timedelta = CHECK_CLAIM_HOLD
) -> bool:
    """Atomically win the right to run the next check.

    Inside one write transaction this re-runs :func:`should_check` and, if the
    check is due, parks a short defer window. Another process or thread that
    asks meanwhile sees the window and skips, so only one of them downloads.
    The winner's :func:`mark_checked` (or :func:`defer_check`) replaces the window.
    """
    con.execute('BEGIN IMMEDIATE')
    try:
        due = should_check(con, interval)
        if due:
            set_meta(con, {META_DEFER_UNTIL: (datetime.now(UTC) + hold).isoformat()})
    except BaseException:
        con.rollback()
        raise
    con.commit()
    return due


def release_claim(con: sqlite3.Connection) -> None:
    """Drop the defer window a failed check left, so the very next check retries."""
    with con:
        con.execute('DELETE FROM metadata WHERE key=?', (META_DEFER_UNTIL,))


def mark_checked(con: sqlite3.Connection) -> None:
    """Record a successful check, which also clears any pending defer window."""
    with con:
        con.execute('DELETE FROM metadata WHERE key=?', (META_DEFER_UNTIL,))
        set_meta(con, {META_LAST_CHECK: datetime.now(UTC).isoformat()})


def defer_check(con: sqlite3.Connection, until: datetime) -> None:
    """Park the next check until ``until`` after a failed refresh."""
    with con:
        set_meta(con, {META_DEFER_UNTIL: until.isoformat()})
