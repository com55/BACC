"""Synthetic catalog data for the tests.

Nothing here is copied from the upstream dataset: the names, schools and file IDs
are invented, and only the CSV *shape* (``bacc.importer.FIELDS``) matches it.
"""

from __future__ import annotations

import csv
import io
import json

from bacc.importer import FIELDS


def make_row(file_id, student_id, spine_id, given, family, *, skin='', remark='',
             school_id='2', school_name='测试学园甲'):
    row = dict.fromkeys(FIELDS, '')
    row.update(
        file_id=file_id, student_id=str(student_id), page_id=str(student_id),
        spine_id=str(spine_id), spine_remark=remark,
        full_en=f'{family} {given}', family_en=family, given_en=given, skin_en=skin,
        full_cn=f'测试{given}', school_id=school_id, school_name=school_name,
    )
    return row


ROWS = [
    make_row('TS0001', 1, 100, 'Alpha', 'Testa'),
    make_row('TS0001', 1, 101, 'Alpha', 'Testa', skin='Swimsuit', remark='泳装'),
    make_row('TS0002', 1, 102, 'Alpha', 'Testa', remark='战损'),
    make_row('TS0003', 1, 103, 'Alpha', 'Testa', remark='反转'),  # no English in GLOSSARY
    make_row('TS0010', 2, 110, 'Beta', 'Testb', school_id='3', school_name='测试学园乙'),
    make_row('TS0011', 2, 111, 'Beta', 'Testb', skin='Swimsuit', school_id='3',
             school_name='测试学园乙'),
    make_row('TS0020', 3, 120, 'Gamma', 'Testc'),
    make_row('TS0020', 3, 121, 'Gamma', 'Testc', remark='战损'),  # file_id is not unique
]

STUDENTS, VARIANTS, SCHOOLS_USED, UNTRANSLATED_TYPES = 3, len(ROWS), 2, 1

GLOSSARY = {'泳装': 'Swimsuit', '战损': 'Battered', '反转': ''}


def build_csv(rows=ROWS) -> bytes:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=FIELDS, lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode('utf-8')


def build_schools() -> bytes:
    schools = [
        {'id': 2, 'name': '测试学园甲', 'name_cn': '测试学园甲', 'name_en': 'Test Academy A', 'logo': ''},
        {'id': 3, 'name': '测试学园乙', 'name_cn': '测试学园乙', 'name_en': 'Test Academy B', 'logo': ''},
        {'id': 4, 'name': '测试学园丙', 'name_cn': '测试学园丙', 'name_en': 'Test Academy C', 'logo': ''},
    ]
    return json.dumps(schools, ensure_ascii=False).encode('utf-8')


def build_glossary() -> bytes:
    return json.dumps(GLOSSARY, ensure_ascii=False).encode('utf-8')


CSV = build_csv()
SCHOOLS = build_schools()
