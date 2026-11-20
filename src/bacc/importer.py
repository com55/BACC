"""Parsing and validating the release CSV before it reaches the database."""

from __future__ import annotations

import csv
import io

from .errors import InvalidDataError

MAX_CSV_BYTES = 20 * 1024 * 1024

FIELDS = [
    'file_id', 'student_id', 'page_id', 'spine_id', 'spine_remark',
    'full_kivo', 'family_kivo', 'given_kivo', 'skin_kivo',
    'full_jp', 'family_jp', 'given_jp', 'skin_jp',
    'full_kr', 'family_kr', 'given_kr', 'skin_kr',
    'full_en', 'family_en', 'given_en', 'skin_en',
    'full_tw', 'family_tw', 'given_tw', 'skin_tw',
    'full_cn', 'family_cn', 'given_cn', 'skin_cn',
    'school_id', 'school_name',
]


def decode_rows(blob: bytes) -> list[dict[str, str]]:
    """Parse the release CSV, rejecting malformed or incomplete records."""
    try:
        text = blob.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise InvalidDataError(f'CSV is not valid UTF-8: {exc}') from exc
    reader = csv.DictReader(io.StringIO(text, newline=''))
    try:
        fieldnames = reader.fieldnames
    except csv.Error as exc:
        raise InvalidDataError(f'Malformed CSV header: {exc}') from exc
    if not fieldnames:
        raise InvalidDataError('CSV missing header')
    missing = set(FIELDS) - set(fieldnames)
    if missing:
        raise InvalidDataError(f'CSV missing required fields: {sorted(missing)}')
    rows = []
    try:
        records = list(enumerate(reader, start=2))
    except csv.Error as exc:
        raise InvalidDataError(f'Malformed CSV: {exc}') from exc
    for line_no, item in records:
        if None in item:
            raise InvalidDataError(f'Malformed CSV row {line_no}: extra fields')
        row = {k: (v or '').strip() for k, v in item.items()}
        if not row['file_id'] or not row['student_id']:
            raise InvalidDataError(f'Missing file_id or student_id at CSV row {line_no}')
        rows.append(row)
    if not rows:
        raise InvalidDataError('CSV has no data rows')
    return rows
