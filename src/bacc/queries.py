"""Search over the imported catalog."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from .models import Variant

# AND applies inside one field, never across fields: a row matches when a single
# field contains every word. The CSV fields are searched through json_each, so
# only their values count: a word like "en" or "kivo" must not match the JSON keys
# and so every row.
_COLUMNS = ('display_name_en', 'spine_remark_en', 'school_name_en')


_ORDER_SQL = r"""
    ORDER BY CASE WHEN v.given_en = ? COLLATE NOCASE THEN 0
                  WHEN v.display_name_en = ? COLLATE NOCASE THEN 1
                  WHEN v.given_en LIKE ? ESCAPE '\' THEN 2
                  ELSE 3 END,
             v.given_en COLLATE NOCASE, v.student_id, v.file_id, v.id
    LIMIT ?
"""


def _escape(text: str) -> str:
    return text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def find_variants(con: sqlite3.Connection, query: str, limit: int = 30) -> list[Variant]:
    """Match variants across the translated columns and every original CSV field.

    Whitespace separates words; one field must contain all of them (AND within a
    field, not across fields).
    Results are ordered best match first, judged on the whole query: exact given
    name, exact display name, given-name prefix, then everything else.
    """
    words = query.split()
    if not words:
        return []
    whole = ' '.join(words)
    # Only placeholders vary with the word count; the text itself is always bound.
    patterns = ['%' + _escape(word) + '%' for word in words]
    like = r"{} LIKE ? ESCAPE '\'"
    clauses = [' AND '.join([like.format(col)] * len(words)) for col in _COLUMNS]
    clauses.append(
        'EXISTS (SELECT 1 FROM json_each(v.source_json) j WHERE '
        + ' AND '.join([like.format('j.value')] * len(words))
        + ')'
    )
    where = ' OR '.join(f'({clause})' for clause in clauses)
    params: list[str | int] = patterns * len(clauses)
    params.extend([whole, whole, _escape(whole) + '%', limit])
    rows = con.execute(f'SELECT v.* FROM variants v WHERE {where}{_ORDER_SQL}', params)
    results = (Variant.from_row(row) for row in rows)
    return [replace(v, matched=tuple(v.matches(whole))) for v in results]


def format_variant(variant: Variant, query: str) -> str:
    """One human-readable line per result, plus the fields that matched."""
    name = variant.display_name_en
    if variant.untranslated:
        name += f' [untranslated: {variant.spine_remark}]'
    where = f'{variant.file_id:<12} student={variant.student_id:<5} spine={variant.spine_id:<6}'
    lines = [f'{where}  {name}  | {variant.school}']
    for field, value in variant.matched or variant.matches(query):
        lines.append(f'    ↳ matched {field}: {value}')
    return '\n'.join(lines)


def render_search(results: list[Variant], query: str, limit: int) -> str:
    """Human-readable block for a list of results, disclosing matched fields."""
    if not results:
        return 'No matches.'
    lines = [format_variant(variant, query) for variant in results]
    lines.append(f'{len(results)} result(s), limit={limit}')
    return '\n'.join(lines)


def run_search(con: sqlite3.Connection, query: str, limit: int) -> None:
    """Print search results, disclosing which fields matched."""
    query = query.strip()
    if not query:
        print('Please enter a search term.')
        return
    print(render_search(find_variants(con, query, limit), query, limit))
