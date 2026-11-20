"""The editable remark glossary (original Chinese remark -> English)."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from importlib.resources import files
from pathlib import Path

from .naming import make_display

PACKAGED_GLOSSARY = 'spine_remark_translate.json'


def packaged_translations() -> dict[str, str]:
    """The glossary shipped inside the package (the seed for a new data directory)."""
    return _validate(json.loads(files('bacc').joinpath(PACKAGED_GLOSSARY).read_text('utf-8')))


def read_translations(path: Path) -> dict[str, str]:
    """Your own entries: the data directory file, or ``{}`` when there is none yet.

    This is only the overrides. The glossary actually applied is
    :func:`merged_translations`.
    """
    path = Path(path)
    if not path.is_file():
        return {}
    return _validate(json.loads(path.read_text(encoding='utf-8-sig')))


def merged_translations(path: Path) -> dict[str, str]:
    """The glossary in effect: the packaged one, overridden by the file at ``path``.

    A non-empty entry in your file wins over the packaged one. An empty entry
    means "no override", so it never blanks out a packaged translation.
    """
    return {
        **packaged_translations(),
        **{remark: english for remark, english in read_translations(path).items() if english},
    }


def _validate(data: object) -> dict[str, str]:
    if not isinstance(data, dict):
        # Bad file content is bad data, not a programming error: callers catch ValueError.
        raise ValueError(  # noqa: TRY004
            'Translation JSON must be an object: {"Chinese": "English"}'
        )
    for zh, en in data.items():
        if not isinstance(zh, str) or not zh or not isinstance(en, str):
            raise ValueError(
                'Every translation must have a nonempty text key and text value '
                '(empty English is allowed)'
            )
    return data


def save_translations(path: Path, translations: dict[str, str]) -> None:
    """Atomic rewrite. Preserve existing keys/values, add new terms only."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(dict(sorted(translations.items())), out, ensure_ascii=False, indent=2)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def sync_translations(con: sqlite3.Connection, translations: dict[str, str]) -> int:
    """Recompute display names from the original CSV metadata, without network."""
    updates = []
    for r in con.execute('SELECT id, source_json, spine_remark FROM variants'):
        source = json.loads(r['source_json'])
        translated = translations.get(r['spine_remark'], '').strip()
        updates.append((translated, make_display(source, translated), r['id']))
    with con:
        con.executemany(
            'UPDATE variants SET spine_remark_en=?, display_name_en=? WHERE id=?', updates
        )
    return len(updates)
