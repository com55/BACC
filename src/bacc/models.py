"""Typed views of the catalog: query results and import outcomes."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field


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
        """Explain a hit: the fields and values that contain every word of ``query``."""
        needles = query.casefold().split()
        fields: dict[str, str] = {
            **self.source,
            'spine_remark_en': self.spine_remark_en,
            'display_name_en': self.display_name_en,
            'school_name_en': self.school_name_en,
        }
        return [
            (name, str(value))
            for name, value in fields.items()
            if value and all(needle in str(value).casefold() for needle in needles)
        ]

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
