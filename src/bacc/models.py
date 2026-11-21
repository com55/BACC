"""Typed views of the catalog: query results and import outcomes."""

from __future__ import annotations

import json
import re
import shlex
import sqlite3
from dataclasses import dataclass, field
from typing import NamedTuple

from .importer import FIELDS

# Names accepted before the ``:`` or ``=`` in ``field:value`` searches: every CSV
# field plus the three columns derived by the catalog.
SEARCH_FIELDS = frozenset(FIELDS) | {'display_name_en', 'spine_remark_en', 'school_name_en'}


class ScopedTerm(NamedTuple):
    """One ``field:value`` (contains) or ``field=value`` (exactly equals) token."""

    field: str
    value: str
    exact: bool = False


def _tokens(query: str) -> list[str]:
    """Whitespace-separated tokens; double quotes keep spaces inside one token.

    ``"display_name_en:Oil Drum"`` and ``display_name_en:"Oil Drum"`` are both one
    token. Backslashes and apostrophes are ordinary characters, and an unbalanced
    quote falls back to plain whitespace splitting.
    """
    lexer = shlex.shlex(query, posix=True)
    lexer.whitespace_split = True
    lexer.quotes = '"'
    lexer.escape = ''
    lexer.commenters = ''
    try:
        return list(lexer)
    except ValueError:
        return query.split()


def parse_query(query: str) -> tuple[list[str], list[ScopedTerm]]:
    """Split a query into plain words and field-scoped terms.

    A token only counts as scoped when the text before the first ``:`` or ``=``
    names a known field and a value follows, so ``12:30`` or ``http://x`` stay
    ordinary words. A quoted token with spaces is one word (a phrase).
    """
    words: list[str] = []
    scoped: list[ScopedTerm] = []
    for token in _tokens(query):
        match = re.fullmatch(r'([a-z_]+)([:=])(.+)', token, re.DOTALL)
        if match and match.group(1) in SEARCH_FIELDS:
            scoped.append(ScopedTerm(match.group(1), match.group(3), match.group(2) == '='))
        elif token:
            words.append(token)
    return words, scoped


@dataclass(frozen=True)
class Variant:
    """One row of the ``variants`` table: a costume/portrait version of a character.

    ``file_id`` is not unique upstream, so several variants can share one.
    """

    file_id: str
    student_id: str
    given_en: str
    full_en: str
    skin_en: str
    spine_remark: str
    spine_remark_en: str
    display_name_en: str
    school_id: str
    school_name: str
    school_name_en: str
    spine_id: str = ''
    page_id: str = ''
    source: dict[str, str] = field(default_factory=dict, repr=False)
    matched: tuple[tuple[str, str], ...] = field(default=(), compare=False)
    # (field, value) pairs that satisfied the query; filled by search(), empty otherwise

    @property
    def untranslated(self) -> bool:
        """``True`` when the source remark still has no English translation."""
        return bool(self.spine_remark) and not self.spine_remark_en

    @property
    def school(self) -> str:
        """English school name when known, otherwise the value from the CSV."""
        return self.school_name_en or self.school_name

    def matches(self, query: str) -> list[tuple[str, str]]:
        """Explain a hit: the fields and values behind it.

        Plain words must all sit in one field; each ``field:value`` (contains) or
        ``field=value`` (equals) token names its own field.
        """
        words, scoped = parse_query(query)
        needles = [word.casefold() for word in words]
        fields: dict[str, str] = {
            **self.source,
            'given_en': self.given_en,
            'spine_remark_en': self.spine_remark_en,
            'display_name_en': self.display_name_en,
            'school_name_en': self.school_name_en,
        }
        found = [
            (name, str(value))
            for name, value in fields.items()
            if needles and value and all(needle in str(value).casefold() for needle in needles)
        ]
        for name, needle, exact in scoped:
            value = str(fields.get(name, ''))
            text, wanted = value.casefold(), needle.casefold()
            if value and (text == wanted if exact else wanted in text) and (name, value) not in found:
                found.append((name, value))
        return found

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Variant:
        return cls(
            file_id=row['file_id'],
            student_id=row['student_id'],
            given_en=row['given_en'],
            full_en=row['full_en'],
            skin_en=row['skin_en'],
            spine_remark=row['spine_remark'],
            spine_remark_en=row['spine_remark_en'],
            display_name_en=row['display_name_en'],
            school_id=row['school_id'],
            school_name=row['school_name'],
            school_name_en=row['school_name_en'],
            spine_id=row['spine_id'],
            page_id=row['page_id'],
            source=json.loads(row['source_json'] or '{}'),
        )


@dataclass(frozen=True)
class Student:
    """A character, with the school resolved to its English name."""

    student_id: str
    given_en: str
    full_en: str
    school_id: str | None = None
    school_name_en: str = ''


@dataclass(frozen=True)
class School:
    """A school, with original, simplified-Chinese and English names."""

    school_id: str
    name: str
    name_cn: str
    name_en: str
    logo: str = ''


@dataclass(frozen=True)
class UntranslatedRemark:
    """An original remark with no English yet, and how many variants use it."""

    remark: str
    count: int


@dataclass(frozen=True)
class Stats:
    """Metadata and row counts for the current snapshot."""

    release_tag: str
    updated_at: str
    students: int
    variants: int
    schools: int
    untranslated_rows: int
    upstream_date: str = ''
    data_commit: str = ''


@dataclass(frozen=True)
class ImportResult:
    """Outcome of importing a release CSV."""

    rows: int
    students: int
    schools: int
    untranslated_types: int
    untranslated_rows: int
    tag: str
    sha256: str
