"""The editable remark glossary (original Chinese remark -> English)."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from importlib.resources import files
from pathlib import Path

from .importer import FIELDS
from .naming import MODES, NameOverride, make_display
from .revision import refresh_revision

PACKAGED_GLOSSARY = 'spine_remark_translate.json'
PACKAGED_NAME_OVERRIDES = 'name_overrides.json'


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


def save_translations(path: Path, translations: dict) -> None:
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


def _parse_overrides(data: object) -> dict[str, NameOverride]:
    """``{"NP0018": "Name"}`` or ``{"NP0018": {"name": "Name", "mode": "full"}}``."""
    if not isinstance(data, dict):
        raise ValueError(  # noqa: TRY004
            'Name override JSON must be an object: {"NP0018": "English name"}'
        )
    result: dict[str, NameOverride] = {}
    for file_id, value in data.items():
        if not isinstance(file_id, str) or not file_id:
            raise ValueError('Every name override needs a nonempty file ID as its key')
        if isinstance(value, str):
            result[file_id] = NameOverride(value.strip())
        elif isinstance(value, dict) and isinstance(value.get('name'), str):
            mode = value.get('mode', 'base')
            if mode not in MODES:
                raise ValueError(f'{file_id}: mode must be one of {MODES}, not {mode!r}')
            result[file_id] = NameOverride(value['name'].strip(), mode)
        else:
            raise ValueError(  # noqa: TRY004
                f'{file_id}: expected a name, or an object with "name" and "mode"'
            )
    return result


def packaged_name_overrides() -> dict[str, NameOverride]:
    """The name overrides shipped inside the package."""
    text = files('bacc').joinpath(PACKAGED_NAME_OVERRIDES).read_text('utf-8')
    return _parse_overrides(json.loads(text))


def read_name_overrides(path: Path) -> dict[str, NameOverride]:
    """Your own entries: the data directory file, or ``{}`` when there is none yet."""
    path = Path(path)
    if not path.is_file():
        return {}
    return _parse_overrides(json.loads(path.read_text(encoding='utf-8-sig')))


def merged_name_overrides(path: Path) -> dict[str, NameOverride]:
    """The overrides in effect: the packaged ones, overridden by your non-empty entries.

    File IDs match regardless of case, so yours replaces a packaged one spelled differently.
    """
    mine = read_name_overrides(path)
    effective = {
        key.casefold(): (key, entry)
        for source in (packaged_name_overrides(), mine)
        for key, entry in source.items()
        if entry.name
    }
    return dict(effective.values())


def by_folded_id(overrides: dict[str, NameOverride]) -> dict[str, NameOverride]:
    """The same overrides keyed for case-insensitive lookup."""
    return {file_id.casefold(): entry for file_id, entry in overrides.items()}


def save_name_overrides(path: Path, overrides: dict[str, NameOverride]) -> None:
    """Atomic rewrite; a base-mode entry is a plain string, a full-mode one an object."""
    save_translations(
        path,
        {
            key: entry.name if entry.mode == 'base' else {'name': entry.name, 'mode': entry.mode}
            for key, entry in overrides.items()
        },
    )


def sync_extra_variants(con: sqlite3.Connection, overrides: dict[str, NameOverride]) -> None:
    """Give every override whose file ID the release lacks a row of its own.

    Runs in the caller's transaction and rebuilds the rows from scratch, so they follow
    the overrides and disappear once the release ships the file ID. They are marked by
    an empty ``student_id``, which imported rows can never have. Nothing is added to a
    catalog that holds no imported rows.
    """
    con.execute("DELETE FROM variants WHERE student_id=''")
    con.execute("DELETE FROM students WHERE student_id=''")
    known = {r['file_id'].casefold() for r in con.execute('SELECT file_id FROM variants')}
    if not known:
        return
    extra = sorted(
        (file_id, entry.name)
        for file_id, entry in overrides.items()
        if entry.name and file_id.casefold() not in known
    )
    if not extra:
        return
    con.execute("INSERT INTO students(student_id) VALUES ('')")
    con.executemany(
        'INSERT INTO variants(file_id, student_id, given_en, display_name_en, source_json)'
        " VALUES (?, '', ?, ?, ?)",
        [
            (
                file_id, name, name,
                json.dumps({**dict.fromkeys(FIELDS, ''), 'file_id': file_id},
                           separators=(',', ':')),
            )
            for file_id, name in extra
        ],
    )


def sync_translations(
    con: sqlite3.Connection,
    translations: dict[str, str],
    overrides: dict[str, NameOverride] | None = None,
) -> int:
    """Recompute display names from the original CSV metadata, without network.

    ``overrides`` rename the file IDs it lists, adding a row for an ID the release
    lacks; everything else is derived from the glossary and the source fields.
    Returns the number of imported rows.
    """
    overrides = overrides or {}
    folded = by_folded_id(overrides)
    updates = []
    for r in con.execute(
        "SELECT id, source_json, spine_remark FROM variants WHERE student_id<>''"
    ):
        source = json.loads(r['source_json'])
        translated = translations.get(r['spine_remark'], '').strip()
        override = folded.get(source['file_id'].casefold())
        given = (override.name if override else '') or source['given_en']
        updates.append((translated, make_display(source, translated, override), given, r['id']))
    with con:
        con.executemany(
            'UPDATE variants SET spine_remark_en=?, display_name_en=?, given_en=? WHERE id=?',
            updates,
        )
        sync_extra_variants(con, overrides)
        refresh_revision(con)
    return len(updates)
