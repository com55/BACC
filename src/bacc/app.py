"""The catalog itself: one SQLite database, opened and queried through
:class:`BlueArchiveCharacterCatalog`."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

from .db import (
    DEFAULT_CHECK_INTERVAL,
    DEFAULT_DEFER_ON_FAILURE,
    DEFAULT_DEFER_ON_INVALID,
    SCHEMA_VERSION,
    claim_check,
    connect,
    defer_check,
    mark_checked,
    meta,
    release_claim,
    set_meta,
)
from .errors import InvalidDataError, NetworkError
from .importer import decode_rows
from .models import ImportResult, School, Stats, Student, UntranslatedRemark, Variant
from .naming import MODES, NameOverride, make_display
from .paths import data_dir as default_data_dir
from .paths import (
    default_db,
    default_name_overrides,
    default_release_dir,
    default_schools,
    default_translations,
)
from .queries import find_variants
from .remote import (
    download_schools,
    fetch_release_data,
    latest_release,
    save_release_files,
    save_schools,
)
from .schools import apply_schools, read_schools
from .translations import (
    merged_name_overrides,
    merged_translations,
    read_name_overrides,
    read_translations,
    save_name_overrides,
    save_translations,
    sync_translations,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Snapshot:
    """Everything parsed from one CSV, ready to be written in a single transaction."""

    schools: dict[str, tuple]
    students: dict[str, tuple]
    variants: list[tuple]
    untranslated: Counter
    sha256: str


def _prepare(
    blob: bytes,
    translations: dict[str, str],
    official_schools: dict[str, dict[str, str]],
    overrides: dict[str, NameOverride],
) -> _Snapshot:
    """Build every row up front, so validation failures never reach the database."""
    rows = decode_rows(blob)

    school_rows: dict[str, tuple] = {}
    student_rows: dict[str, tuple] = {}
    variant_rows = []
    missing: Counter = Counter()
    for index, row in enumerate(rows, 1):
        sid, school_id = row['student_id'], row['school_id']
        if school_id:
            school = official_schools.get(school_id, {})
            school_rows.setdefault(
                school_id,
                (
                    school_id,
                    row['school_name'],
                    school.get('name_cn', ''),
                    school.get('name_en', ''),
                    school.get('logo', ''),
                ),
            )
        student_rows.setdefault(
            sid, (sid, row['given_en'], row['full_en'], school_id or None)
        )
        remark = row['spine_remark']
        override = overrides.get(row['file_id'])
        english = translations.get(remark, '') if remark else ''
        if remark and not english:
            missing[remark] += 1
        variant_rows.append(
            (
                index, row['file_id'], sid, row['spine_id'], row['page_id'],
                (override.name if override else '') or row['given_en'], row['full_en'], row['skin_en'], remark, english,
                make_display(row, english, override), school_id, row['school_name'],
                official_schools.get(school_id, {}).get('name_en', ''),
                json.dumps(row, ensure_ascii=False, separators=(',', ':')),
            )
        )
    return _Snapshot(
        schools=school_rows,
        students=student_rows,
        variants=variant_rows,
        untranslated=missing,
        sha256=hashlib.sha256(blob).hexdigest(),
    )


class BlueArchiveCharacterCatalog:
    """A BA character catalog: a SQLite database plus its editable JSON glossaries.

    Args:
        data_dir: Directory for the database and the two JSON files. Defaults to
            ``$BACC_DATA_DIR`` or ``platformdirs.user_data_dir("BACC")``, so the
            catalog does not depend on the current working directory.
        db_path, translations_path, schools_path, name_overrides_path, release_dir: Per-file overrides.
            Each defaults to its environment variable (``BACC_DB``,
            ``BACC_TRANSLATIONS``, ``BACC_SCHOOLS``, ``BACC_NAME_OVERRIDES``, ``BACC_RELEASE_DIR``) and then
            to a file inside ``data_dir``. ``release_dir`` keeps every file of the
            last release zip, including ``LICENSE-DATA.txt``. Use
            ``db_path`` to keep the database off a network filesystem, where
            SQLite's WAL mode does not work.
        proxy: Optional HTTP/HTTPS proxy URL applied to every download.
        auto_update: Refresh the catalog from :meth:`search` when due.
        check_interval: Minimum time between upstream checks.

    ::

        catalog = BlueArchiveCharacterCatalog()
        catalog.search('mika', limit=3)[0].display_name_en  # 'Mika'

    The database is opened on first use and closed by :meth:`close`, or by leaving
    a ``with`` block. An instance belongs to the thread that first used it; give
    each thread its own instance. Any number of processes may share one database.
    """

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        db_path: str | Path | None = None,
        translations_path: str | Path | None = None,
        schools_path: str | Path | None = None,
        name_overrides_path: str | Path | None = None,
        release_dir: str | Path | None = None,
        proxy: str | None = None,
        auto_update: bool = True,
        check_interval: timedelta = DEFAULT_CHECK_INTERVAL,
    ) -> None:
        if data_dir is not None and Path(data_dir).suffix == '.db':
            raise ValueError(f'{data_dir!r} looks like a database file; pass db_path=... instead')
        self.data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
        self.db_path = Path(db_path) if db_path is not None else default_db(self.data_dir)
        self.translations_path = (
            Path(translations_path)
            if translations_path is not None
            else default_translations(self.data_dir)
        )
        self.schools_path = (
            Path(schools_path) if schools_path is not None else default_schools(self.data_dir)
        )
        self.name_overrides_path = (
            Path(name_overrides_path)
            if name_overrides_path is not None
            else default_name_overrides(self.data_dir)
        )
        self.release_dir = (
            Path(release_dir) if release_dir is not None else default_release_dir(self.data_dir)
        )
        self.proxy = proxy
        self.auto_update = auto_update
        self.check_interval = check_interval
        self._con: sqlite3.Connection | None = None
        # Serializes read-modify-write of the JSON files between this thread and
        # the background refresh. Always taken before any database write.
        self._files_lock = threading.Lock()
        # Held by the one background refresh at a time; a second request is skipped.
        self._background_lock = threading.Lock()
        self._background_thread: threading.Thread | None = None

    def __repr__(self) -> str:
        return f'BlueArchiveCharacterCatalog({str(self.db_path)!r})'

    # -- connection ---------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        """The open connection, created (and migrated) on first access."""
        if self._con is None:
            self._con = connect(self.db_path)
        return self._con

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- reading ------------------------------------------------------------

    def search(
        self,
        query: str,
        limit: int = 30,
        *,
        refresh: bool | None = None,
        update_background: bool = False,
    ) -> list[Variant]:
        """Match a name, file ID, school or original remark, best match first.

        Words separated by whitespace must all match (in any field), so
        ``'mika swimsuit'`` narrows ``'mika'``.

        Like bagfd's ``query()``, a search first refreshes the catalog when the
        check interval has elapsed. Set ``refresh=False`` to search strictly
        offline, or ``auto_update=False`` on the object to turn this off entirely.

        With ``update_background=True`` a due refresh runs on a daemon thread
        and the search answers from the catalog as it is now: possibly stale,
        or empty if it has never been populated. At most one background refresh
        runs per instance; requests made while one is in flight are skipped.
        """
        if refresh is None:
            refresh = self.auto_update
        if refresh:
            if update_background:
                self._refresh_in_background()
            else:
                self._refresh_if_due(self.connection)
        return find_variants(self.connection, query, limit)

    def variants(
        self, student_id: str | None = None, file_id: str | None = None
    ) -> list[Variant]:
        """Every variant, or the ones belonging to one student or file ID."""
        if student_id is not None and file_id is not None:
            raise ValueError('Pass at most one of student_id and file_id')
        column, value = (
            ('file_id', file_id) if file_id is not None else ('student_id', student_id)
        )
        if value is None:
            rows = self.connection.execute('SELECT * FROM variants ORDER BY id')
        else:
            rows = self.connection.execute(
                f'SELECT * FROM variants WHERE {column}=? COLLATE NOCASE ORDER BY id', (value,)
            )
        return [Variant.from_row(row) for row in rows]

    def students(self, school_id: str | None = None) -> list[Student]:
        """Every student, or those of one school, with the English school name."""
        sql = '''SELECT s.student_id, s.given_en, s.full_en, s.school_id,
                        COALESCE(sc.school_name_en, '') AS school_name_en
                 FROM students s LEFT JOIN schools sc ON sc.school_id = s.school_id'''
        if school_id is None:
            rows = self.connection.execute(sql + ' ORDER BY CAST(s.student_id AS INTEGER)')
        else:
            rows = self.connection.execute(
                sql + ' WHERE s.school_id=? ORDER BY CAST(s.student_id AS INTEGER)', (school_id,)
            )
        return [
            Student(
                student_id=r['student_id'],
                given_en=r['given_en'],
                full_en=r['full_en'],
                school_id=r['school_id'],
                school_name_en=r['school_name_en'],
            )
            for r in rows
        ]

    def schools(self) -> list[School]:
        """Every school referred to by the catalog."""
        return [
            School(
                school_id=r['school_id'],
                name=r['school_name'],
                name_cn=r['school_name_cn'],
                name_en=r['school_name_en'],
                logo=r['logo'],
            )
            for r in self.connection.execute('SELECT * FROM schools ORDER BY school_id')
        ]

    def untranslated(self, limit: int = 100) -> list[UntranslatedRemark]:
        """Remarks still lacking English, most used first."""
        rows = self.connection.execute(
            '''SELECT spine_remark AS remark, count(*) AS n FROM variants
               WHERE spine_remark<>'' AND spine_remark_en=''
               GROUP BY spine_remark ORDER BY n DESC, spine_remark LIMIT ?''',
            (limit,),
        )
        return [UntranslatedRemark(remark=r['remark'], count=r['n']) for r in rows]

    def stats(self) -> Stats:
        """Snapshot metadata and row counts."""
        con = self.connection
        return Stats(
            release_tag=meta(con, 'release_tag'),
            updated_at=meta(con, 'updated_at'),
            students=con.execute('SELECT count(*) FROM students').fetchone()[0],
            variants=con.execute('SELECT count(*) FROM variants').fetchone()[0],
            schools=con.execute('SELECT count(*) FROM schools').fetchone()[0],
            untranslated_rows=con.execute(
                "SELECT count(*) FROM variants WHERE spine_remark<>'' AND spine_remark_en=''"
            ).fetchone()[0],
            upstream_date=meta(con, 'upstream_date'),
            data_commit=meta(con, 'upstream_data_commit'),
        )

    def translations(self) -> dict[str, str]:
        """The glossary in effect: the packaged one overridden by your data-directory file."""
        return merged_translations(self.translations_path)

    # -- writing ------------------------------------------------------------

    def import_csv(
        self, blob: bytes, *, tag: str = 'local-import', source: str = ''
    ) -> ImportResult:
        """Replace the whole snapshot with a release CSV.

        The CSV, the glossary and schools.json are all validated before the first
        write, and the swap happens in one transaction, so a bad input leaves the
        previous catalog untouched. Raises :class:`~bacc.errors.InvalidDataError`
        for a bad CSV.
        """
        return self._import_csv(self.connection, blob, tag=tag, source=source)

    def _import_csv(
        self,
        con: sqlite3.Connection,
        blob: bytes,
        *,
        tag: str,
        source: str,
        extra: dict[str, str] | None = None,
    ) -> ImportResult:
        with self._files_lock:
            translations = merged_translations(self.translations_path)
            snapshot = _prepare(
                blob,
                translations,
                read_schools(self.schools_path),
                merged_name_overrides(self.name_overrides_path),
            )
            with con:
                con.execute('DELETE FROM variants')
                con.execute('DELETE FROM students')
                con.execute('DELETE FROM schools')
                con.executemany(
                    'INSERT INTO schools(school_id,school_name,school_name_cn,school_name_en,logo)'
                    ' VALUES (?,?,?,?,?)',
                    snapshot.schools.values(),
                )
                con.executemany(
                    'INSERT INTO students(student_id,given_en,full_en,school_id) VALUES (?,?,?,?)',
                    snapshot.students.values(),
                )
                con.executemany(
                    '''INSERT INTO variants (
                        id,file_id,student_id,spine_id,page_id,given_en,full_en,skin_en,
                        spine_remark,spine_remark_en,display_name_en,school_id,school_name,
                        school_name_en,source_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    snapshot.variants,
                )
                set_meta(
                    con,
                    {
                        'release_tag': tag,
                        'asset_url': source,
                        'sha256': snapshot.sha256,
                        'updated_at': datetime.now(UTC).isoformat(),
                        'row_count': str(len(snapshot.variants)),
                        'schema_version': SCHEMA_VERSION,
                        **(extra or {}),
                    },
                )
        return ImportResult(
            rows=len(snapshot.variants),
            students=len(snapshot.students),
            schools=len(snapshot.schools),
            untranslated_types=len(snapshot.untranslated),
            untranslated_rows=sum(snapshot.untranslated.values()),
            tag=tag,
            sha256=snapshot.sha256,
        )

    def update(self, force: bool = False) -> ImportResult | None:
        """Import the upstream release when its content changed.

        Returns ``None`` when the catalog was already current; school names and
        translations are still refreshed in that case, since both are edited
        locally between releases. The data is fetched even when the tag is
        unchanged, so a same-tag hotfix (new bytes, same release) is still
        picked up — the SHA-256 is what decides whether a rebuild is needed.

        The release zip is used when it validates, with the standalone CSV asset
        as the fallback. Raises :class:`~bacc.errors.NetworkError` when the
        release cannot be reached and :class:`~bacc.errors.InvalidDataError`
        when nothing it offers passes validation; the catalog is untouched either way.
        """
        return self._update(self.connection, force)

    def _update(self, con: sqlite3.Connection, force: bool) -> ImportResult | None:
        release = latest_release(self.proxy)
        data = fetch_release_data(release, self.proxy)
        try:
            schools = download_schools(self.proxy)
            with self._files_lock:
                save_schools(schools, self.schools_path)
        except (NetworkError, InvalidDataError) as exc:
            # The CSV is the catalog; school names are garnish. Keep the local copy.
            logger.warning('Could not refresh schools.json (%s); keeping the local copy', exc)
        sha256 = hashlib.sha256(data.csv).hexdigest()
        unchanged = (
            not force
            and meta(con, 'release_tag') == release.tag
            and meta(con, 'sha256') == sha256
        )
        if data.files and (not unchanged or not self.release_dir.exists()):
            try:
                with self._files_lock:
                    save_release_files(data.files, self.release_dir)
            except OSError as exc:  # the files are an archive, not the catalog
                logger.warning('Could not save the release files to %s (%s)', self.release_dir, exc)
        if unchanged:
            apply_schools(con, read_schools(self.schools_path))
            self._sync(con)
            mark_checked(con)
            return None
        result = self._import_csv(
            con, data.csv, tag=release.tag, source=data.source, extra=data.info
        )
        mark_checked(con)
        return result

    def clean(self) -> None:
        """Forget the imported snapshot and every freshness record.

        Empties ``variants``, ``students``, ``schools`` and ``metadata`` so the next
        refresh starts from scratch. The two JSON files (glossary and schools) are
        user data and are kept, and so are the saved release files.
        """
        con = self.connection
        with con:
            for table in ('variants', 'students', 'schools', 'metadata'):
                con.execute(f'DELETE FROM {table}')

    def _refresh_in_background(self) -> None:
        """Start :meth:`_refresh_if_due` on a daemon thread unless one is already running."""
        if not self._background_lock.acquire(blocking=False):
            return

        def run() -> None:
            try:
                con = connect(self.db_path)  # a connection cannot be shared across threads
                try:
                    self._refresh_if_due(con)
                finally:
                    con.close()
            except Exception:
                logger.exception('Background catalog refresh failed')
            finally:
                self._background_lock.release()

        self._background_thread = threading.Thread(target=run, name='bacc-update', daemon=True)
        self._background_thread.start()

    def _refresh_if_due(self, con: sqlite3.Connection) -> bool:
        """Refresh the catalog when the check interval has elapsed.

        Mirrors bagfd's ``_ensure_fresh``. :func:`claim_check` makes the decision
        atomic across threads and processes, so concurrent searches trigger one
        download, not several. A failed refresh defers the next attempt instead
        of raising, so searches keep serving the cached catalog: briefly after a
        network error, much longer after bad data. Only a catalog that has never
        been populated propagates the error, since there is nothing to fall back on.
        """
        if not claim_check(con, self.check_interval):
            return False
        try:
            self._update(con, force=False)
        except (OSError, ValueError, sqlite3.Error) as exc:
            if not meta(con, 'release_tag'):
                release_claim(con)  # stay loud: every search retries until one succeeds
                raise
            invalid = isinstance(exc, InvalidDataError)
            until = datetime.now(UTC) + (
                DEFAULT_DEFER_ON_INVALID if invalid else DEFAULT_DEFER_ON_FAILURE
            )
            defer_check(con, until)
            logger.warning(
                'Catalog refresh failed (%s); serving cached release %s '
                'and deferring the next check until %s',
                exc,
                meta(con, 'release_tag'),
                until.isoformat(timespec='seconds'),
            )
            return False
        return True

    def translate(self, original: str, english: str) -> int:
        """Record one glossary entry and refresh the derived names.

        The entry goes into your own glossary file in the data directory (created
        on the first call), so it survives future imports and wins over the
        packaged translation. An empty ``english`` removes your entry, letting the
        packaged translation, if any, apply again.
        """
        original = original.strip()
        if not original:
            raise ValueError('Original remark must not be empty')
        with self._files_lock:
            mine = read_translations(self.translations_path)
            if english.strip():
                mine[original] = english
            else:
                mine.pop(original, None)  # no entry of your own: the packaged one applies
            save_translations(self.translations_path, mine)
            return self._sync(self.connection)

    def sync_translations(self) -> int:
        """Recompute English remarks and display names from the glossary and name files."""
        return self._sync(self.connection)

    def _sync(self, con: sqlite3.Connection) -> int:
        return sync_translations(
            con,
            merged_translations(self.translations_path),
            merged_name_overrides(self.name_overrides_path),
        )

    def name_overrides(self) -> dict[str, NameOverride]:
        """The overrides in effect: the packaged ones overridden by your data-directory file."""
        return merged_name_overrides(self.name_overrides_path)

    def set_name_override(self, file_id: str, name: str, *, mode: str = 'base') -> int:
        """Record one name override and refresh the derived names.

        ``file_id`` is a file ID such as ``NP0121``; it applies to every variant
        row that carries it. ``mode='base'`` replaces only the base name, so labels
        are still appended (``Name (Swimsuit)``); ``mode='full'`` replaces the whole
        display name. The entry goes into your own file in the data directory
        (created on the first call), so it survives updates and wins over the
        packaged one. An empty ``name`` removes your entry.
        """
        file_id = file_id.strip()
        if not file_id:
            raise ValueError('File ID must not be empty')
        if mode not in MODES:
            raise ValueError(f'mode must be one of {MODES}, not {mode!r}')
        with self._files_lock:
            mine = read_name_overrides(self.name_overrides_path)
            if name.strip():
                mine[file_id] = NameOverride(name.strip(), mode)
            else:
                mine.pop(file_id, None)  # no entry of your own: the packaged one applies
            save_name_overrides(self.name_overrides_path, mine)
            return self._sync(self.connection)

    def sync_schools(self) -> int:
        """Refresh school names from schools.json without reimporting the CSV."""
        return apply_schools(self.connection, read_schools(self.schools_path))
