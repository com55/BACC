"""Official school metadata (schools.json) and its application to the database."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from .errors import InvalidDataError
from .paths import default_schools
from .revision import refresh_revision

logger = logging.getLogger(__name__)


def decode_schools(blob: bytes) -> dict[str, dict[str, str]]:
    """Parse and validate a schools.json payload, keyed by string school ID."""
    try:
        data = json.loads(blob.decode('utf-8-sig'))
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
        raise InvalidDataError(f'schools.json is not valid JSON: {exc}') from exc
    if not isinstance(data, list) or not data:
        raise InvalidDataError('schools.json must be a nonempty array')
    schools = {}
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get('id'), int) or item['id'] <= 0:
            raise InvalidDataError('Invalid school record / id')
        sid = str(item['id'])
        if sid in schools or not isinstance(item.get('name_en'), str) or not item['name_en'].strip():
            raise InvalidDataError(f'Duplicate school ID or missing English name: {sid}')
        schools[sid] = {
            'name': str(item.get('name') or '').strip(),
            'name_cn': str(item.get('name_cn') or '').strip(),
            'name_en': item['name_en'].strip(),
            'logo': str(item.get('logo') or '').strip(),
        }
    return schools


def read_schools(path: Path | None = None) -> dict[str, dict[str, str]]:
    """Load schools.json from ``path`` (default: the data directory)."""
    path = Path(path) if path is not None else default_schools()
    if not path.is_file():
        # A fresh data directory has none until the first online update fetches it.
        logger.warning('%s not found; school names stay empty until the next update', path)
        return {}
    return decode_schools(path.read_bytes())


def apply_schools(con: sqlite3.Connection, schools: dict[str, dict[str, str]]) -> int:
    """Update school names without rebuilding variants or translations."""
    with con:
        for sid, school in schools.items():
            con.execute(
                '''INSERT INTO schools(school_id,school_name,school_name_cn,school_name_en,logo)
                VALUES (?,?,?,?,?) ON CONFLICT(school_id) DO UPDATE SET
                school_name=excluded.school_name,
                school_name_cn=excluded.school_name_cn,
                school_name_en=excluded.school_name_en,
                logo=excluded.logo''',
                (sid, school['name'], school['name_cn'], school['name_en'], school['logo']),
            )
        con.execute(
            '''UPDATE variants SET school_name_en = COALESCE(
                (SELECT school_name_en FROM schools WHERE schools.school_id=variants.school_id), '')'''
        )
        refresh_revision(con)
    return len(schools)
