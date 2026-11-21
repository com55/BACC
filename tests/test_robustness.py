"""Data dir, proxy, background refresh, clean, AND search, typed errors and self-healing."""

import io
import json
import sqlite3
import threading
import zipfile
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from factory import CSV, SCHOOLS, VARIANTS

import bacc.app as app_module
from bacc import (
    BlueArchiveCharacterCatalog,
    InvalidDataError,
    NetworkError,
    connect,
    meta,
    read_translations,
    remote,
)
from bacc import translations as translations_module
from bacc.db import claim_check, defer_check, mark_checked, release_claim, should_check
from bacc.naming import NameOverride
from bacc.remote import Release, ReleaseData, fetch_release_data
from bacc.translations import packaged_name_overrides, packaged_translations, read_name_overrides

RELEASE = Release(tag='t1', csv_url='https://x/csv', zip_url='https://x/zip')
INTERVAL = timedelta(hours=4)


def make_zip(csv: bytes = CSV, metadata: bytes | None = b'{"updateDate": "2026-11-18", '
             b'"codeVersion": "v5.0.0", "dataCommitHash": "abc123"}', pad: int = 0) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('students_data.csv', csv + b'\n' * pad)
        if metadata is not None:
            archive.writestr('metadata.json', metadata)
        archive.writestr('LICENSE-DATA.txt', b'license text')
        archive.writestr('students.json', b'{"1": {}}')
    return out.getvalue()


@pytest.fixture(autouse=True)
def no_network():
    with patch.object(app_module, 'latest_release', side_effect=AssertionError('network in test')):
        yield


@pytest.fixture
def online(monkeypatch):
    """Pretend GitHub serves a good release (zip + schools); returns the call log."""
    calls = []

    def latest(proxy=None):
        calls.append(('latest', proxy))
        return RELEASE

    def fetch(release, proxy=None):
        calls.append(('fetch', proxy))
        return ReleaseData(csv=CSV, source=release.zip_url,
                           info={'upstream_date': '2026-11-18', 'upstream_data_commit': 'abc123'})

    monkeypatch.setattr(app_module, 'latest_release', latest)
    monkeypatch.setattr(app_module, 'fetch_release_data', fetch)
    monkeypatch.setattr(app_module, 'download_schools', lambda proxy=None: SCHOOLS)
    return calls


@pytest.fixture
def catalog(tmp_path):
    cat = BlueArchiveCharacterCatalog(tmp_path / 'data')
    yield cat
    cat.close()


# --- 1. data directory ------------------------------------------------------


def test_fresh_install_works_from_any_cwd(tmp_path, monkeypatch, online):
    """Empty data dir, unrelated cwd: the first search bootstraps everything."""
    data = tmp_path / 'bacc-data'
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv('BACC_DATA_DIR', str(data))
    for name in ('BACC_DB', 'BACC_TRANSLATIONS', 'BACC_SCHOOLS'):
        monkeypatch.delenv(name, raising=False)
    with BlueArchiveCharacterCatalog() as cat:
        assert cat.search('alpha', limit=1)[0].display_name_en.startswith('Alpha')
        assert cat.data_dir == data
    assert (data / 'ba_characters.db').is_file()
    assert (data / 'schools.json').is_file()
    assert not (data / 'spine_remark_translate.json').exists()  # nothing of your own to save
    assert list(elsewhere.iterdir()) == []  # nothing leaked into the cwd


def test_data_dir_derives_every_path(tmp_path, monkeypatch):
    for name in ('BACC_DB', 'BACC_TRANSLATIONS', 'BACC_SCHOOLS'):
        monkeypatch.delenv(name, raising=False)
    cat = BlueArchiveCharacterCatalog(tmp_path)
    assert cat.db_path == tmp_path / 'ba_characters.db'
    assert cat.translations_path == tmp_path / 'spine_remark_translate.json'
    assert cat.schools_path == tmp_path / 'schools.json'


def test_env_db_beats_data_dir_and_explicit_beats_env(tmp_path, monkeypatch):
    monkeypatch.setenv('BACC_DB', str(tmp_path / 'env.db'))
    assert BlueArchiveCharacterCatalog(tmp_path / 'd').db_path == tmp_path / 'env.db'
    explicit = BlueArchiveCharacterCatalog(tmp_path / 'd', db_path=tmp_path / 'x.db')
    assert explicit.db_path == tmp_path / 'x.db'


def test_db_file_as_first_argument_is_rejected(tmp_path):
    with pytest.raises(ValueError, match='db_path'):
        BlueArchiveCharacterCatalog(tmp_path / 'old-style.db')


def test_offline_import_into_empty_dir(tmp_path):
    """No glossary file, no schools.json, no network: import still works."""
    with BlueArchiveCharacterCatalog(tmp_path) as cat:
        result = cat.import_csv(CSV)
        assert result.rows == VARIANTS
        assert cat.search('alpha', limit=1, refresh=False)
    assert not (tmp_path / 'spine_remark_translate.json').exists()


def test_packaged_glossary_applies_without_any_file(tmp_path):
    packaged = packaged_translations()
    assert packaged['泳装'] == 'Swimsuit' and len(packaged) > 50
    with BlueArchiveCharacterCatalog(tmp_path) as cat:
        assert cat.translations() == packaged
        cat.import_csv(CSV)
        assert cat.variants(file_id='TS0001')[1].display_name_en == 'Alpha (Swimsuit)'
    assert not (tmp_path / 'spine_remark_translate.json').exists()  # importing writes nothing


def test_data_dir_entry_wins_over_packaged(tmp_path):
    (tmp_path / 'spine_remark_translate.json').write_text('{"战损": "Mine"}', 'utf-8')
    with BlueArchiveCharacterCatalog(tmp_path) as cat:
        assert cat.translations()['战损'] == 'Mine'            # yours first
        assert cat.translations()['泳装'] == 'Swimsuit'         # packaged fills the rest
        cat.import_csv(CSV)
        assert cat.variants(file_id='TS0002')[0].display_name_en == 'Alpha (Mine)'


def test_empty_entry_in_data_dir_does_not_blank_packaged(tmp_path):
    (tmp_path / 'spine_remark_translate.json').write_text('{"泳装": ""}', 'utf-8')
    with BlueArchiveCharacterCatalog(tmp_path) as cat:
        assert cat.translations()['泳装'] == 'Swimsuit'


def test_translate_writes_only_your_entry_and_can_revert(tmp_path):
    path = tmp_path / 'spine_remark_translate.json'
    with BlueArchiveCharacterCatalog(tmp_path) as cat:
        cat.import_csv(CSV)
        cat.translate('战损', 'Mine')
        assert json.loads(path.read_text('utf-8')) == {'战损': 'Mine'}  # not a copy of the seed
        assert cat.variants(file_id='TS0002')[0].display_name_en == 'Alpha (Mine)'
        cat.translate('战损', '')                                       # drop it: packaged applies
        assert json.loads(path.read_text('utf-8')) == {}
        assert cat.variants(file_id='TS0002')[0].display_name_en == 'Alpha (Battle-Damaged)'


# --- 2. proxy ---------------------------------------------------------------


def test_proxy_uses_a_proxy_handler():
    with patch.object(remote.urllib.request, 'build_opener') as build:
        remote._open('https://example.org/x', {}, 'http://proxy.test:3128')
    handler = build.call_args.args[0]
    assert handler.proxies == {'http': 'http://proxy.test:3128', 'https': 'http://proxy.test:3128'}
    build.return_value.open.assert_called_once()


def test_no_proxy_uses_plain_urlopen():
    with patch.object(remote.urllib.request, 'urlopen') as plain:
        remote._open('https://example.org/x', {}, None)
    plain.assert_called_once()


def test_catalog_passes_its_proxy_to_every_download(tmp_path, online):
    with BlueArchiveCharacterCatalog(tmp_path, proxy='http://p:1') as cat:
        cat.update()
    assert online == [('latest', 'http://p:1'), ('fetch', 'http://p:1')]


# --- 3. background refresh ----------------------------------------------------


def test_background_search_does_not_block_and_serves_cached(catalog, online):
    catalog.import_csv(CSV, tag='old')
    release = threading.Event()
    real_update = catalog._update

    def slow_update(con, force):
        release.wait(5)
        return real_update(con, force)

    catalog._update = slow_update
    results = catalog.search('alpha', limit=1, update_background=True)
    assert results                               # answered from the cached catalog immediately
    assert catalog._background_thread.is_alive()  # ...while the refresh is still running
    first = catalog._background_thread
    catalog.search('alpha', limit=1, update_background=True)
    assert catalog._background_thread is first   # in-flight refresh: second request skipped
    release.set()
    first.join(5)
    assert not first.is_alive()
    assert meta(catalog.connection, 'release_tag') == 't1'


def test_background_on_empty_catalog_returns_nothing_then_fills(catalog, online, monkeypatch):
    gate = threading.Event()  # hold the download so the thread cannot win the race
    fetch = app_module.fetch_release_data

    def slow_fetch(release, proxy=None):
        gate.wait(30)
        return fetch(release, proxy)

    monkeypatch.setattr(app_module, 'fetch_release_data', slow_fetch)
    assert catalog.search('alpha', update_background=True) == []
    gate.set()
    catalog._background_thread.join(30)
    assert catalog.search('alpha', limit=1, refresh=False)


def test_background_failure_is_logged_not_raised(catalog, monkeypatch, caplog):
    monkeypatch.setattr(app_module, 'latest_release', MagicMock(side_effect=NetworkError('down')))
    assert catalog.search('alpha', update_background=True) == []
    catalog._background_thread.join(30)
    assert 'Background catalog refresh failed' in caplog.text


# --- 4. clean ---------------------------------------------------------------


def test_clean_empties_catalog_but_keeps_json(catalog):
    catalog.import_csv(CSV)
    catalog.translate('战损', 'Mine')
    catalog.clean()
    stats = catalog.stats()
    assert (stats.variants, stats.students, stats.schools, stats.release_tag) == (0, 0, 0, '')
    assert read_translations(catalog.translations_path) == {'战损': 'Mine'}
    assert should_check(catalog.connection, INTERVAL)  # next search refreshes again


# --- 5. typed errors and automatic handling -----------------------------------


def test_zip_is_preferred_and_metadata_kept(monkeypatch):
    blobs = {'https://x/zip': make_zip()}
    monkeypatch.setattr(remote, '_download', lambda url, proxy=None: blobs[url])
    data = fetch_release_data(RELEASE)
    assert data.source == 'https://x/zip'
    assert data.csv == CSV
    assert data.info == {'upstream_date': '2026-11-18', 'upstream_code_version': 'v5.0.0',
                         'upstream_data_commit': 'abc123'}


def test_bad_zip_falls_back_to_csv_asset(monkeypatch):
    blobs = {'https://x/zip': b'not a zip', 'https://x/csv': CSV}
    monkeypatch.setattr(remote, '_download', lambda url, proxy=None: blobs[url])
    data = fetch_release_data(RELEASE)
    assert data.source == 'https://x/csv'
    assert data.info == {}


def test_zip_with_corrupt_csv_falls_back(monkeypatch):
    blobs = {'https://x/zip': make_zip(csv=b'a,b\n1,2\n'), 'https://x/csv': CSV}
    monkeypatch.setattr(remote, '_download', lambda url, proxy=None: blobs[url])
    assert fetch_release_data(RELEASE).source == 'https://x/csv'


def test_zip_without_metadata_still_imports(monkeypatch):
    monkeypatch.setattr(remote, '_download', lambda url, proxy=None: make_zip(metadata=None))
    assert fetch_release_data(RELEASE).info == {}


def test_oversized_zip_member_is_refused(monkeypatch):
    monkeypatch.setattr(remote, 'MAX_CSV_BYTES', 1000)
    monkeypatch.setattr(remote, '_download', lambda url, proxy=None: make_zip(pad=5000))
    with pytest.raises(InvalidDataError, match='larger than allowed'):
        fetch_release_data(Release(tag='t', zip_url='https://x/zip'))


def test_every_source_bad_raises_invalid_data(monkeypatch):
    monkeypatch.setattr(remote, '_download', lambda url, proxy=None: b'garbage')
    with pytest.raises(InvalidDataError, match='No usable data'):
        fetch_release_data(RELEASE)


def test_network_error_wins_over_data_error(monkeypatch):
    def download(url, proxy=None):
        if url.endswith('zip'):
            raise NetworkError('down')
        return b'garbage'

    monkeypatch.setattr(remote, '_download', download)
    with pytest.raises(NetworkError):
        fetch_release_data(RELEASE)


def test_typed_errors_keep_builtin_bases():
    assert issubclass(NetworkError, OSError)
    assert issubclass(InvalidDataError, ValueError)


def test_bad_csv_import_raises_invalid_data_and_keeps_catalog(catalog):
    catalog.import_csv(CSV, tag='good')
    with pytest.raises(InvalidDataError):
        catalog.import_csv(b'\xff\xfe not utf8', tag='bad')
    with pytest.raises(InvalidDataError):
        catalog.import_csv(b'file_id,student_id\nA,1\n', tag='bad')
    assert catalog.stats().release_tag == 'good'
    assert catalog.stats().variants == VARIANTS


def test_invalid_data_defers_longer_than_network_error(catalog, monkeypatch):
    catalog.import_csv(CSV, tag='good')

    def deferred_hours(error):
        mark_checked(catalog.connection)
        with catalog.connection:
            catalog.connection.execute("DELETE FROM metadata WHERE key='last_check'")
        monkeypatch.setattr(app_module, 'latest_release', MagicMock(side_effect=error))
        assert catalog.search('alpha', limit=1)  # cached rows still served
        until = datetime.fromisoformat(meta(catalog.connection, 'defer_until'))
        return (until - datetime.now(UTC)).total_seconds() / 3600

    assert deferred_hours(NetworkError('down')) < 1
    assert deferred_hours(InvalidDataError('bad')) > 5


def test_empty_catalog_stays_loud_and_retries(catalog, monkeypatch):
    mock = MagicMock(side_effect=NetworkError('down'))
    monkeypatch.setattr(app_module, 'latest_release', mock)
    for _ in range(2):
        with pytest.raises(NetworkError):
            catalog.search('alpha')
    assert mock.call_count == 2  # no defer window swallowed the second attempt


def test_update_stores_upstream_metadata(catalog, online):
    catalog.update()
    stats = catalog.stats()
    assert (stats.release_tag, stats.upstream_date, stats.data_commit) == ('t1', '2026-11-18', 'abc123')
    assert meta(catalog.connection, 'asset_url') == 'https://x/zip'


def test_damaged_database_is_set_aside_and_rebuilt(tmp_path):
    db = tmp_path / 'catalog.db'
    db.write_bytes(b'this is not a sqlite database' * 100)
    con = connect(db)
    con.close()
    assert list(tmp_path.glob('catalog.db.corrupt-*'))
    with BlueArchiveCharacterCatalog(db_path=db) as cat:
        assert cat.stats().variants == 0


def test_memory_database_does_not_hang(tmp_path, caplog):
    """WAL is unavailable in memory: SQLite reports 'memory' instead of raising."""
    done = []
    worker = threading.Thread(target=lambda: done.append(connect(':memory:').close()), daemon=True)
    worker.start()
    worker.join(5)
    assert done, 'connect(":memory:") hung'
    assert 'kept journal mode' in caplog.text
    with BlueArchiveCharacterCatalog(db_path=':memory:') as cat:
        assert cat.stats().variants == 0


def test_integrity_error_is_not_mistaken_for_corruption(tmp_path):
    db = tmp_path / 'catalog.db'
    connect(db).close()
    failing = patch('bacc.db._open', side_effect=sqlite3.IntegrityError('constraint'))
    with failing, pytest.raises(sqlite3.IntegrityError):
        connect(db)
    assert not list(tmp_path.glob('*.corrupt-*'))


def test_schools_download_failure_does_not_block_the_update(tmp_path, online, monkeypatch):
    def broken(proxy=None):
        raise NetworkError('raw.githubusercontent.com down')

    monkeypatch.setattr(app_module, 'download_schools', broken)
    with BlueArchiveCharacterCatalog(tmp_path) as cat:
        assert cat.update().rows == VARIANTS
        assert {school.name_en for school in cat.schools()} == {''}  # no English names, import done


def test_locked_database_is_not_mistaken_for_corruption(tmp_path):
    db = tmp_path / 'catalog.db'
    connect(db).close()
    locked = patch('bacc.db._open', side_effect=sqlite3.OperationalError('database is locked'))
    with locked, pytest.raises(sqlite3.OperationalError):
        connect(db)
    assert not list(tmp_path.glob('*.corrupt-*'))


# --- 6. AND search ------------------------------------------------------------


def test_whitespace_separated_words_must_all_match(catalog):
    catalog.import_csv(CSV)
    alpha = catalog.search('alpha', limit=1000, refresh=False)
    narrowed = catalog.search('alpha swimsuit', limit=1000, refresh=False)
    assert 0 < len(narrowed) < len(alpha)
    for variant in narrowed:
        text = ' '.join(f'{k} {v}' for k, v in variant.matches('alpha swimsuit')).casefold()
        assert 'alpha' in text and 'swimsuit' in text
    assert catalog.search('alpha zzzznotaword', refresh=False) == []


def test_words_match_values_not_json_keys(catalog):
    """'kivo', 'school_id' ... are CSV column names: they must not match every row."""
    catalog.import_csv(CSV)
    assert len(catalog.search('alpha', limit=100, refresh=False)) == 4
    for key in ('kivo', 'school_id', 'given_en', 'spine_remark'):
        assert catalog.search(f'alpha {key}', limit=100, refresh=False) == []
    only_100 = catalog.search('100', limit=100, refresh=False)  # a value (spine_id)
    assert [v.spine_id for v in only_100] == ['100']


def test_and_applies_within_one_field_not_across_fields(catalog):
    catalog.import_csv(CSV)
    # 'alpha' is in the name, '100' only in spine_id: no single field has both.
    assert catalog.search('alpha 100', limit=100, refresh=False) == []
    # both words live in the display name, e.g. "Alpha (Swimsuit)".
    hits = catalog.search('alpha swimsuit', limit=100, refresh=False)
    assert hits and all(
        any('alpha' in v.casefold() and 'swimsuit' in v.casefold() for _, v in x.matches('alpha swimsuit'))
        for x in hits
    )


def test_word_order_and_extra_spaces_do_not_matter(catalog):
    catalog.import_csv(CSV)
    a = catalog.search('alpha   swimsuit', limit=1000, refresh=False)
    b = catalog.search(' swimsuit alpha ', limit=1000, refresh=False)
    assert {(v.file_id, v.spine_id) for v in a} == {(v.file_id, v.spine_id) for v in b} != set()
    assert catalog.search('   ', refresh=False) == []


# --- 7. WAL and cross-process claim -------------------------------------------


def test_wal_lets_a_reader_proceed_during_a_write(tmp_path):
    db = tmp_path / 'catalog.db'
    writer, reader = connect(db), connect(db)
    assert writer.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    writer.execute('BEGIN IMMEDIATE')
    writer.execute("INSERT INTO metadata VALUES ('k', 'v')")
    assert meta(reader, 'k') == ''  # reader neither blocks nor sees uncommitted data
    writer.commit()
    assert meta(reader, 'k') == 'v'
    writer.close()
    reader.close()


def test_claim_check_lets_exactly_one_contender_win(tmp_path):
    db = tmp_path / 'catalog.db'
    first, second = connect(db), connect(db)
    assert claim_check(first, INTERVAL) is True
    assert claim_check(second, INTERVAL) is False   # the claim window blocks the other process
    release_claim(first)
    assert claim_check(second, INTERVAL) is True
    mark_checked(second)
    assert claim_check(first, INTERVAL) is False    # fresh now: nothing is due
    defer_check(first, datetime.now(UTC) - timedelta(seconds=1))
    first.close()
    second.close()


def test_concurrent_searches_download_once(tmp_path, online):
    cats = [BlueArchiveCharacterCatalog(tmp_path) for _ in range(4)]
    connect(tmp_path / 'ba_characters.db').close()  # schema first, so the threads race only for the claim
    barrier = threading.Barrier(len(cats))
    errors = []

    def run(cat):
        try:
            barrier.wait()
            cat.search('alpha')
        except Exception as exc:  # noqa: BLE001 - any failure in a worker must fail the test
            errors.append(exc)
        finally:
            cat.close()

    threads = [threading.Thread(target=run, args=(cat,)) for cat in cats]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    assert [c for c in online if c[0] == 'latest'] == [('latest', None)]


def test_search_results_carry_the_fields_that_matched(catalog):
    catalog.import_csv(CSV)
    hits = catalog.search('alpha swimsuit', limit=100, refresh=False)
    assert hits
    for hit in hits:
        assert hit.matched and list(hit.matched) == hit.matches('alpha swimsuit')
    assert all(not v.matched for v in catalog.variants(student_id='1'))  # no query, no matches


# --- release files ------------------------------------------------------------


def test_every_zip_member_is_kept_verbatim(monkeypatch):
    monkeypatch.setattr(remote, '_download', lambda url, proxy=None: make_zip())
    data = fetch_release_data(Release(tag='t', zip_url='https://x/zip'))
    assert set(data.files) == {'students_data.csv', 'metadata.json', 'LICENSE-DATA.txt', 'students.json'}
    assert data.files['LICENSE-DATA.txt'] == b'license text'


def test_zip_members_outside_the_top_level_are_ignored(monkeypatch):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        archive.writestr('students_data.csv', CSV)
        archive.writestr('../evil.txt', b'x')
        archive.writestr('sub/inner.txt', b'x')
        for bad in ('C:evil.txt', 'sub\\inner.txt', 'NUL', 'con.txt', 'a:b', ' x'):
            archive.writestr(bad, b'x')
    monkeypatch.setattr(remote, '_download', lambda url, proxy=None: out.getvalue())
    data = fetch_release_data(Release(tag='t', zip_url='https://x/zip'))
    assert set(data.files) == {'students_data.csv'}


def test_update_saves_release_files_and_swaps_them_whole(catalog, online, monkeypatch):
    def fetch(release, proxy=None):
        return ReleaseData(csv=CSV, source='z', files={'students_data.csv': CSV, 'LICENSE-DATA.txt': b'v1'})

    monkeypatch.setattr(app_module, 'fetch_release_data', fetch)
    catalog.update()
    assert (catalog.release_dir / 'LICENSE-DATA.txt').read_bytes() == b'v1'
    assert catalog.release_dir == catalog.data_dir / 'release'

    def fetch2(release, proxy=None):
        return ReleaseData(csv=CSV, source='z', files={'students_data.csv': CSV, 'NEW.json': b'{}'})

    monkeypatch.setattr(app_module, 'fetch_release_data', fetch2)
    catalog.update(force=True)
    assert sorted(p.name for p in catalog.release_dir.iterdir()) == ['NEW.json', 'students_data.csv']
    assert [p.name for p in catalog.data_dir.iterdir() if '.tmp' in p.name or '.old' in p.name] == []


def test_csv_fallback_leaves_saved_release_files_alone(catalog, online):
    catalog.update()  # online fixture returns no files: nothing is written
    assert not catalog.release_dir.exists()
    catalog.release_dir.mkdir(parents=True)
    (catalog.release_dir / 'LICENSE-DATA.txt').write_bytes(b'keep me')
    catalog.update(force=True)
    assert (catalog.release_dir / 'LICENSE-DATA.txt').read_bytes() == b'keep me'


def test_clean_keeps_release_files(catalog):
    catalog.release_dir.mkdir(parents=True)
    (catalog.release_dir / 'LICENSE-DATA.txt').write_bytes(b'x')
    catalog.clean()
    assert (catalog.release_dir / 'LICENSE-DATA.txt').exists()


def test_save_release_files_refuses_unsafe_names(tmp_path):
    with pytest.raises(InvalidDataError):
        remote.save_release_files({'../x': b'1'}, tmp_path / 'release')
    assert list(tmp_path.iterdir()) == []


# --- name overrides (packaged + yours, like the glossary) ---------------------


def _display(cat, file_id):
    return sorted((v.spine_id, v.display_name_en) for v in cat.variants(file_id=file_id))


def test_base_mode_replaces_only_the_base_name(catalog):
    catalog.import_csv(CSV)
    catalog.set_name_override('TS0001', 'Renamed')
    assert _display(catalog, 'TS0001') == [('100', 'Renamed'), ('101', 'Renamed (Swimsuit)')]
    first = catalog.variants(file_id='TS0001')[0]
    assert first.given_en == 'Renamed' and first.source['given_en'] == 'Alpha'  # original kept
    assert catalog.search('renamed', refresh=False)
    assert catalog.name_overrides()['TS0001'] == NameOverride('Renamed', 'base')


def test_full_mode_replaces_the_whole_display_name(catalog):
    catalog.import_csv(CSV)
    catalog.set_name_override('TS0001', 'Whole Name', mode='full')
    assert _display(catalog, 'TS0001') == [('100', 'Whole Name'), ('101', 'Whole Name')]
    assert catalog.name_overrides()['TS0001'].mode == 'full'


def test_every_row_sharing_a_file_id_gets_the_override(catalog):
    catalog.import_csv(CSV)
    catalog.set_name_override('TS0020', 'Gamma Two')
    assert dict(_display(catalog, 'TS0020')) == {'120': 'Gamma Two', '121': 'Gamma Two (Battle-Damaged)'}


def test_override_survives_reimport_and_removal_restores_the_source(catalog):
    catalog.import_csv(CSV)
    catalog.set_name_override('TS0010', 'Mine')
    catalog.import_csv(CSV)
    assert _display(catalog, 'TS0010') == [('110', 'Mine')]
    catalog.set_name_override('TS0010', '')
    assert _display(catalog, 'TS0010') == [('110', 'Beta')]
    assert catalog.name_overrides() == packaged_name_overrides()


def test_overrides_leave_other_file_ids_alone(catalog):
    catalog.import_csv(CSV)
    before = _display(catalog, 'TS0011')
    catalog.set_name_override('TS0010', 'Mine')
    assert _display(catalog, 'TS0011') == before


def test_yours_file_stores_base_as_text_and_full_as_object(catalog):
    catalog.set_name_override('A1', 'Plain')
    catalog.set_name_override('B2', 'Whole', mode='full')
    assert json.loads(catalog.name_overrides_path.read_text()) == {
        'A1': 'Plain', 'B2': {'name': 'Whole', 'mode': 'full'},
    }
    assert read_name_overrides(catalog.name_overrides_path) == {
        'A1': NameOverride('Plain'), 'B2': NameOverride('Whole', 'full'),
    }


def test_packaged_overrides_are_the_base_and_yours_win(catalog, monkeypatch):
    packaged = {'TS0010': NameOverride('Packaged B', 'full'), 'TS0020': NameOverride('Packaged C')}
    monkeypatch.setattr(translations_module, 'packaged_name_overrides', lambda: packaged)
    catalog.import_csv(CSV)
    assert not catalog.name_overrides_path.exists()  # importing never writes the file
    assert dict(_display(catalog, 'TS0010')) == {'110': 'Packaged B'}
    catalog.set_name_override('TS0010', 'Mine')
    assert catalog.name_overrides() == {'TS0010': NameOverride('Mine'), 'TS0020': packaged['TS0020']}
    assert set(read_name_overrides(catalog.name_overrides_path)) == {'TS0010'}  # only yours on disk
    assert dict(_display(catalog, 'TS0010')) == {'110': 'Mine'}
    catalog.set_name_override('TS0010', '')  # removing yours brings the packaged one back
    assert dict(_display(catalog, 'TS0010')) == {'110': 'Packaged B'}


def test_empty_user_name_does_not_blank_a_packaged_one(catalog, monkeypatch):
    monkeypatch.setattr(translations_module, 'packaged_name_overrides',
                        lambda: {'TS0010': NameOverride('Packaged B')})
    catalog.name_overrides_path.parent.mkdir(parents=True, exist_ok=True)
    catalog.name_overrides_path.write_text('{"TS0010": ""}')
    catalog.import_csv(CSV)
    assert dict(_display(catalog, 'TS0010')) == {'110': 'Packaged B'}


def test_the_shipped_overrides_are_the_three_agreed_entries():
    assert packaged_name_overrides() == {
        'NP0018': NameOverride('Oil Drum', 'full'),
        'NP0081': NameOverride('Asuna (School)', 'full'),
        'NP0082': NameOverride('Karin (School)', 'full'),
    }


@pytest.mark.parametrize('text', ['["x"]', '{"A": 1}', '{"A": {"mode": "full"}}',
                                  '{"A": {"name": "x", "mode": "half"}}', '{"": "x"}'])
def test_bad_overrides_file_is_rejected(catalog, text):
    catalog.name_overrides_path.parent.mkdir(parents=True, exist_ok=True)
    catalog.name_overrides_path.write_text(text)
    with pytest.raises(ValueError):
        catalog.name_overrides()


def test_bad_arguments_are_rejected(catalog):
    with pytest.raises(ValueError):
        catalog.set_name_override('  ', 'x')
    with pytest.raises(ValueError):
        catalog.set_name_override('A', 'x', mode='half')


# --- field:value search -------------------------------------------------------


def _ids(hits):
    return sorted((v.file_id, v.spine_id) for v in hits)


def test_field_scoped_search_hits_only_that_field(catalog):
    catalog.import_csv(CSV)
    hits = catalog.search('file_id:TS0001', limit=100, refresh=False)
    assert _ids(hits) == [('TS0001', '100'), ('TS0001', '101')]
    assert all(v.matched == (('file_id', 'TS0001'),) for v in hits)
    # the same text as a plain word also finds rows through other fields; scoped must not
    assert catalog.search('file_id:Alpha', refresh=False) == []
    assert catalog.search('given_en:Alpha', limit=100, refresh=False)


def test_scoped_and_plain_tokens_combine_with_and(catalog):
    catalog.import_csv(CSV)
    hits = catalog.search('file_id:TS0001 swimsuit', limit=100, refresh=False)
    assert _ids(hits) == [('TS0001', '101')]
    assert catalog.search('file_id:TS0001 zzznotaword', refresh=False) == []
    both = catalog.search('file_id:TS0020 spine_remark:战损', limit=100, refresh=False)
    assert _ids(both) == [('TS0020', '121')]


def test_scoped_search_reaches_csv_fields_and_derived_columns(catalog):
    catalog.import_csv(CSV)
    assert _ids(catalog.search('full_cn:测试Alpha', limit=100, refresh=False))  # source_json field
    assert catalog.search('display_name_en:swimsuit', limit=100, refresh=False)
    catalog.set_name_override('TS0010', 'Mine')
    assert _ids(catalog.search('given_en:Mine', refresh=False)) == [('TS0010', '110')]


def test_unknown_field_prefix_is_an_ordinary_word(catalog):
    catalog.import_csv(CSV)
    assert catalog.search('nosuchfield:TS0001', refresh=False) == []  # a word nobody contains
    assert catalog.search('file_id:', refresh=False) == []            # no value: plain word too
    assert catalog.search('"; DROP TABLE variants; --', refresh=False) == []
    assert catalog.stats().variants == 8


def test_scoped_value_wildcards_are_literal(catalog):
    catalog.import_csv(CSV)
    assert catalog.search('file_id:%', refresh=False) == []
    assert catalog.search('file_id:TS_001', refresh=False) == []


# --- quoted tokens and exact (field=value) search -----------------------------


def test_quotes_keep_spaces_inside_one_token(catalog):
    catalog.import_csv(CSV)
    catalog.set_name_override('TS0010', 'Oil Drum', mode='full')
    for query in ('"display_name_en:Oil Drum"', 'display_name_en:"Oil Drum"'):
        assert _ids(catalog.search(query, refresh=False)) == [('TS0010', '110')], query
    # a quoted plain phrase must sit in one field in that order
    assert _ids(catalog.search('"Oil Drum"', refresh=False)) == [('TS0010', '110')]
    assert catalog.search('"Drum Oil"', refresh=False) == []


def test_unbalanced_quote_and_apostrophes_are_plain_text(catalog):
    catalog.import_csv(CSV)
    assert catalog.search('"alpha', limit=100, refresh=False) == []  # literal quote, no row has it
    assert catalog.search("don't", refresh=False) == []
    assert catalog.search('alpha', limit=100, refresh=False)


def test_exact_search_needs_the_whole_value(catalog):
    catalog.import_csv(CSV)
    assert _ids(catalog.search('file_id=TS0001', limit=100, refresh=False)) == [
        ('TS0001', '100'), ('TS0001', '101')]
    assert catalog.search('file_id=TS000', refresh=False) == []       # prefix is not exact
    assert catalog.search('file_id:TS000', limit=100, refresh=False)   # but it is contained
    assert _ids(catalog.search('FILE_ID=ts0010', refresh=False)) == []  # field names are lowercase
    assert _ids(catalog.search('file_id=ts0010', refresh=False)) == [('TS0010', '110')]  # NOCASE


def test_exact_works_on_csv_fields_derived_columns_and_with_quotes(catalog):
    catalog.import_csv(CSV)
    catalog.set_name_override('TS0010', 'Oil Drum', mode='full')
    assert _ids(catalog.search('"display_name_en=Oil Drum"', refresh=False)) == [('TS0010', '110')]
    assert catalog.search('"display_name_en=Oil"', refresh=False) == []
    assert _ids(catalog.search('full_cn=测试Beta', limit=100, refresh=False))  # source_json field


def test_exact_matched_reports_only_the_exact_field(catalog):
    catalog.import_csv(CSV)
    hit = catalog.search('file_id=TS0010', refresh=False)[0]
    assert hit.matched == (('file_id', 'TS0010'),)
    assert hit.matches('file_id=TS001') == []  # not equal, so not a match


def test_exact_value_is_bound_not_interpolated(catalog):
    catalog.import_csv(CSV)
    assert catalog.search('file_id=%', refresh=False) == []
    assert catalog.search("file_id=x' OR '1'='1", refresh=False) == []
    assert catalog.stats().variants == 8
