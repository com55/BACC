"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import urllib.error
from pathlib import Path

from .app import BlueArchiveCharacterCatalog
from .importer import MAX_CSV_BYTES
from .queries import render_search


def set_translation(catalog: BlueArchiveCharacterCatalog, original: str, english: str) -> None:
    """Save one glossary entry, then refresh the derived names."""
    count = catalog.translate(original, english)
    print(
        f'Saved in {catalog.translations_path.name}: '
        f'{original} -> {english!r}; synced {count} rows'
    )


def _print_stats(catalog: BlueArchiveCharacterCatalog) -> None:
    stats = catalog.stats()
    print('Release:', stats.release_tag or '(none)')
    print('Last imported:', stats.updated_at or '(none)')
    if stats.upstream_date:
        print('Upstream data:', stats.upstream_date, stats.data_commit[:12])
    for table in ('students', 'variants', 'schools'):
        print(f'{table}: {getattr(stats, table)}')
    print('Untranslated rows:', stats.untranslated_rows)


def _print_untranslated(catalog: BlueArchiveCharacterCatalog, limit: int) -> None:
    remarks = catalog.untranslated(limit)
    for remark in remarks:
        print(f'{remark.remark}  ({remark.count})')
    print(f'{len(remarks)} untranslated remark(s) shown')


def _print_import(result) -> None:
    print(
        f'Imported {result.rows} variants / {result.students} students / '
        f'{result.schools} schools'
    )
    print(f'Source: {result.tag} | SHA256 {result.sha256[:16]}...')
    print(
        f'Untranslated remarks: {result.untranslated_types} distinct / '
        f'{result.untranslated_rows} rows'
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='bacc',
        description='Blue Archive Characters Catalog (BACC): import the BA-characters-internal-id release CSV into SQLite.',
    )
    p.add_argument(
        '--data-dir', type=Path, default=None,
        help='Directory for the database and JSON files '
             '(default: $BACC_DATA_DIR or the per-user data directory)',
    )
    p.add_argument('--db', type=Path, default=None, help='DB path (default: inside the data dir)')
    p.add_argument(
        '--translations', type=Path, default=None,
        help='Translation JSON path (default: inside the data dir)',
    )
    p.add_argument(
        '--schools', type=Path, default=None,
        help='Schools JSON path (default: inside the data dir)',
    )
    p.add_argument(
        '--release-dir', type=Path, default=None,
        help='Where the release zip files are kept (default: inside the data dir)',
    )
    p.add_argument('--proxy', default=None, help='HTTP/HTTPS proxy URL for downloads')
    p.add_argument(
        '-q', '--quiet', action='store_true',
        help='Suppress progress logs on stderr (errors still shown)',
    )
    sub = p.add_subparsers(dest='command', required=True)
    up = sub.add_parser('update', help='Check GitHub latest release and import if newer')
    up.add_argument('--force', action='store_true', help='Redownload even when tag and hash match')
    imp = sub.add_parser('import', help='Import local CSV (offline)')
    imp.add_argument('csv_file', type=Path)
    search = sub.add_parser('search', help='Search names, file IDs, schools, and variants')
    search.add_argument('query')
    search.add_argument('--limit', type=int, default=30)
    search.add_argument(
        '--format',
        choices=('text', 'json'),
        default='text',
        dest='format',
        help='Output format for search results (default text)',
    )
    search.add_argument(
        '--offline',
        action='store_true',
        help='Do not check the upstream release before searching',
    )
    sub.add_parser('stats', help='Display database metadata and row counts')
    sub.add_parser('clean', help='Forget the imported catalog (JSON files are kept)')
    sub.add_parser('sync', help='Apply edits from spine_remark_translate.json to DB (offline)')
    sub.add_parser('sync-schools', help='Import schools.json into existing DB without rebuilding CSV')
    rem = sub.add_parser('untranslated', help='Show untranslated original remarks')
    rem.add_argument('--limit', type=int, default=100)
    trans = sub.add_parser('translate', help='Add/update English translation for a remark')
    trans.add_argument('original')
    trans.add_argument('english')
    return p


def _configure_logging(quiet: bool) -> None:
    """Progress and warnings go to stderr; stdout stays for data only."""
    logging.basicConfig(
        level=logging.ERROR if quiet else logging.INFO,
        format='%(message)s',
        stream=sys.stderr,
        force=True,
    )


def _print_search_json(results, query: str) -> None:
    """Machine-readable results: a JSON array on stdout, one object per match."""
    payload = [
        {
            'file_id': v.file_id,
            'student_id': v.student_id,
            'spine_id': v.spine_id,
            'page_id': v.page_id,
            'given_en': v.given_en,
            'full_en': v.full_en,
            'skin_en': v.skin_en,
            'spine_remark': v.spine_remark,
            'spine_remark_en': v.spine_remark_en,
            'display_name_en': v.display_name_en,
            'school_id': v.school_id,
            'school_name': v.school_name,
            'school_name_en': v.school_name_en,
            'untranslated': v.untranslated,
            'matched': [{'field': f, 'value': val} for f, val in v.matches(query)],
        }
        for v in results
    ]
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.quiet)
    try:
        with BlueArchiveCharacterCatalog(
            args.data_dir,
            db_path=args.db,
            translations_path=args.translations,
            schools_path=args.schools,
            release_dir=args.release_dir,
            proxy=args.proxy,
        ) as cat:
            if args.command == 'update':
                result = cat.update(args.force)
                if result is None:
                    print(f'Already up to date: {cat.stats().release_tag}')
                else:
                    _print_import(result)
            elif args.command == 'import':
                blob = args.csv_file.read_bytes()
                if len(blob) > MAX_CSV_BYTES:
                    raise ValueError('CSV larger than allowed size')
                _print_import(cat.import_csv(blob, source=str(args.csv_file)))
            elif args.command == 'search':
                limit = max(1, min(args.limit, 1000))
                results = cat.search(args.query, limit, refresh=not args.offline)
                if args.format == 'json':
                    _print_search_json(results, args.query)
                else:
                    print(render_search(results, args.query, limit))
            elif args.command == 'translate':
                set_translation(cat, args.original, args.english)
            elif args.command == 'sync':
                print(
                    f'Synced {cat.sync_translations()} records from '
                    f'{cat.translations_path}'
                )
            elif args.command == 'sync-schools':
                print(
                    f'Synced {cat.sync_schools()} schools and refreshed English names '
                    'in variants'
                )
            elif args.command == 'stats':
                _print_stats(cat)
            elif args.command == 'clean':
                cat.clean()
                print(f'Cleared the catalog in {cat.db_path}')
            elif args.command == 'untranslated':
                _print_untranslated(cat, max(1, args.limit))
        return 0
    except (
        OSError,
        ValueError,
        sqlite3.Error,
        UnicodeError,
        urllib.error.URLError,
        json.JSONDecodeError,
    ) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 1
