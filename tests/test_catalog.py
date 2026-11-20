import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from factory import CSV, SCHOOLS, STUDENTS, UNTRANSLATED_TYPES, VARIANTS, build_glossary

import bacc.app as app_module
from bacc import (
    BlueArchiveCharacterCatalog,
    apply_schools,
    meta,
    read_schools,
    read_translations,
)
from bacc.db import defer_check, mark_checked, should_check
from bacc.queries import run_search


@pytest.fixture(autouse=True)
def no_network():
    """Fail loudly if a test path reaches GitHub without asking for it.

    ``AssertionError`` deliberately escapes ``_refresh_if_due``'s defer logic, so
    an accidental online search is a test failure rather than a quiet defer.
    Tests that exercise the refresh call patch ``latest_release`` themselves.
    """
    with patch.object(app_module, 'latest_release', side_effect=AssertionError('network in test')):
        yield


@pytest.fixture
def catalog(tmp_path):
    """A catalog on a fresh database, with copies of the editable JSON files."""
    translations = tmp_path / 'spine_remark_translate.json'
    translations.write_bytes(build_glossary())
    schools = tmp_path / 'schools.json'
    schools.write_bytes(SCHOOLS)
    cat = BlueArchiveCharacterCatalog(db_path=tmp_path / 'test.db', translations_path=translations, schools_path=schools)
    cat.import_csv(CSV, tag='fixture', source='local')
    yield cat
    cat.close()


def test_import_counts(catalog):
    result = catalog.import_csv(CSV, tag='fixture', source='local')
    assert (result.rows, result.students) == (VARIANTS, STUDENTS)
    assert result.schools == 2  # schools actually referenced by the CSV
    assert result.untranslated_types == UNTRANSLATED_TYPES
    assert result.tag == 'fixture'
    stats = catalog.stats()
    assert (stats.variants, stats.students) == (VARIANTS, STUDENTS)
    assert stats.release_tag == 'fixture'
    assert meta(catalog.connection, 'schema_version') == '2'


def test_display_names(catalog):
    names = {v.display_name_en for v in catalog.variants(student_id='1')}
    assert names == {'Alpha', 'Alpha (Swimsuit)', 'Alpha (Battered)'}


def test_schools_synced(catalog):
    schools = {s.school_id: s for s in catalog.schools()}
    assert schools['2'].name_en == 'Test Academy A'
    assert catalog.variants(file_id='TS0010')[0].school_name_en == 'Test Academy B'


def test_edit_glossary_then_sync(catalog):
    data = json.loads(catalog.translations_path.read_text('utf8'))
    data['战损'] = 'Wounded'
    catalog.translations_path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf8')
    catalog.sync_translations()
    assert catalog.variants(file_id='TS0002')[0].display_name_en == 'Alpha (Wounded)'
    catalog.import_csv(CSV, tag='fixture2')
    assert catalog.variants(file_id='TS0002')[0].display_name_en == 'Alpha (Wounded)'


def test_missing_translation_is_empty(catalog):
    assert read_translations(catalog.translations_path)['反转'] == ''


def test_corrupt_csv_keeps_db(catalog):
    with pytest.raises(ValueError):
        catalog.import_csv(b'x,y\na,b\n', tag='bad')
    assert catalog.stats().release_tag == 'fixture'


def test_search_discloses_matched_field(catalog):
    variant = catalog.variants(file_id='TS0010')[0]
    assert ('family_en', 'Testb') in variant.matches('testb')


def test_search_finds_by_original_field(catalog, capsys):
    run_search(catalog.connection, '测试学园乙', 5)
    out = capsys.readouterr().out
    assert 'student=2' in out
    assert 'result(s)' in out


# --- freshness / auto-update -------------------------------------------------


def test_failed_refresh_keeps_serving_cached_rows(catalog, monkeypatch):
    """A due check that fails must not break the search or lose the catalog."""
    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise OSError('network down')

    monkeypatch.setattr(app_module, 'latest_release', boom)
    assert should_check(catalog.connection, timedelta(hours=4))  # never checked yet

    results = catalog.search('alpha', limit=1)
    assert calls == [1]                                  # a check really was attempted
    assert results[0].display_name_en.startswith('Alpha')  # cached rows still served
    assert meta(catalog.connection, 'defer_until')        # failure parked a defer window
    assert not should_check(catalog.connection, timedelta(hours=4))


def test_search_respects_check_interval(catalog):
    mark_checked(catalog.connection)
    assert not should_check(catalog.connection, timedelta(hours=4))
    with patch.object(app_module, 'latest_release', side_effect=AssertionError('checked too soon')):
        assert catalog.search('alpha', limit=1)


def test_refresh_false_never_touches_the_network(catalog):
    with patch.object(app_module, 'latest_release', side_effect=AssertionError('went online')):
        assert catalog.search('alpha', limit=1, refresh=False)
        assert catalog.search('alpha', limit=1, refresh=False)


def test_auto_update_can_be_disabled_on_the_object(tmp_path):
    cat = BlueArchiveCharacterCatalog(db_path=tmp_path / 'off.db', auto_update=False)
    with patch.object(app_module, 'latest_release', side_effect=AssertionError('went online')):
        assert cat.search('anything') == []
    cat.close()


def test_defer_window_then_success_clears_it(catalog):
    con = catalog.connection
    assert should_check(con, timedelta(hours=4)) is True
    defer_check(con, datetime.now(UTC) + timedelta(minutes=10))
    assert should_check(con, timedelta(hours=4)) is False
    mark_checked(con)                       # a success clears the defer window...
    assert not meta(con, 'defer_until')
    assert should_check(con, timedelta(hours=4)) is False   # ...and throttles by interval
    assert should_check(con, timedelta(seconds=0)) is True


def test_duplicate_file_ids_preserved(catalog):
    assert len(catalog.variants(file_id='TS0020')) == 2


def test_translate_writes_json_and_db(catalog):
    catalog.translate('战损', 'Wounded')
    assert read_translations(catalog.translations_path)['战损'] == 'Wounded'
    assert catalog.variants(file_id='TS0002')[0].spine_remark_en == 'Wounded'


def test_translate_rejects_blank_original(catalog):
    with pytest.raises(ValueError):
        catalog.translate('   ', 'x')


def test_sync_schools_without_reimport(catalog):
    catalog.connection.execute("UPDATE schools SET school_name_en='x' WHERE school_id='2'")
    catalog.connection.commit()
    apply_schools(catalog.connection, read_schools(catalog.schools_path))
    schools = {s.school_id: s for s in catalog.schools()}
    assert schools['2'].name_en == 'Test Academy A'


def test_untranslated_ordered_by_count(catalog):
    remarks = catalog.untranslated(limit=5)
    assert remarks and remarks[0].count >= remarks[-1].count
    assert all(r.remark and r.count > 0 for r in remarks)


def test_connection_is_reused_and_closable(tmp_path):
    cat = BlueArchiveCharacterCatalog(db_path=tmp_path / 'lazy.db')
    assert cat.connection is cat.connection   # opened once, schema created on first use
    assert cat.stats().variants == 0
    cat.close()
    assert cat._con is None


def test_context_manager_closes(tmp_path):
    with BlueArchiveCharacterCatalog(db_path=tmp_path / 'ctx.db') as cat:
        assert cat.stats().variants == 0
    assert cat._con is None


def test_explicit_paths_win(tmp_path):
    cat = BlueArchiveCharacterCatalog(db_path=tmp_path / 'a.db', translations_path=tmp_path / 't.json')
    assert cat.db_path == tmp_path / 'a.db'
    assert cat.translations_path == tmp_path / 't.json'
    cat.close()
