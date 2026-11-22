"""A revision of the catalog content, so a client can tell whether its copy is current.

The revision is a UTC timestamp plus a digest of everything a query can return.
Only the digest decides whether two revisions are the same; the timestamp says
when the content last changed. It is stored in the ``metadata`` table and refreshed
inside the transaction of every write that can change the content, so a reader never
sees new data under an old revision.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from .db import meta, set_meta

META_AT = 'revision_at'
META_DIGEST = 'revision_digest'
_STAMP = '%Y%m%dT%H%M%SZ'
_TOKEN = re.compile(r'(\d{8}T\d{6}Z)-([0-9a-f]{16})')
# Every table a query reads, each ordered by its key so the digest is stable.
_TABLES = (
    ('variants', 'id'),
    ('students', 'student_id'),
    ('schools', 'school_id'),
)


@dataclass(frozen=True)
class Revision:
    """When the catalog content last changed, and a digest of that content.

    ``str(revision)`` is a token such as ``20261122T120000Z-1a2b3c4d5e6f7a8b`` that a
    client can store and later pass to :meth:`BlueArchiveCharacterCatalog.is_current`.
    """

    changed_at: str
    digest: str

    def __str__(self) -> str:
        return f'{self.changed_at}-{self.digest}'

    @classmethod
    def parse(cls, token: str) -> Revision:
        """Read back a token produced by ``str(revision)``."""
        match = _TOKEN.fullmatch(token.strip())
        if not match:
            raise ValueError(f'Not a revision token: {token!r}')
        return cls(match.group(1), match.group(2))


def content_digest(con: sqlite3.Connection) -> str:
    """Hash of every row a query can return (16 hex characters)."""
    hasher = hashlib.sha256()
    for table, key in _TABLES:
        hasher.update(table.encode() + b'\n')
        for row in con.execute(f'SELECT * FROM {table} ORDER BY {key}'):
            hasher.update(json.dumps(tuple(row), ensure_ascii=False).encode() + b'\n')
    return hasher.hexdigest()[:16]


def refresh_revision(con: sqlite3.Connection) -> Revision:
    """Recompute the revision in the caller's transaction; the timestamp moves only on change."""
    digest = content_digest(con)
    if meta(con, META_DIGEST) != digest or not meta(con, META_AT):
        set_meta(con, {META_DIGEST: digest, META_AT: datetime.now(UTC).strftime(_STAMP)})
    return Revision(meta(con, META_AT), digest)


def read_revision(con: sqlite3.Connection) -> Revision:
    """The stored revision; a database from before revisions existed gets one on first read."""
    digest, changed_at = meta(con, META_DIGEST), meta(con, META_AT)
    if digest and changed_at:
        return Revision(changed_at, digest)
    with con:
        return refresh_revision(con)
