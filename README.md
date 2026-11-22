# Blue Archive Characters Catalog (BACC)

Import the [`BA-characters-internal-id`](https://github.com/Agent-0808/BA-characters-internal-id)
release CSV into a searchable SQLite catalog. Python 3.11+; the only dependency is `platformdirs`.

## Layout

```
BACC/
├─ pyproject.toml          # uv lib project, uv_build backend
├─ src/bacc/
│  ├─ __init__.py          # public API
│  ├─ __main__.py          # python -m bacc
│  ├─ app.py               # BlueArchiveCharacterCatalog: DB handle + queries
│  ├─ models.py            # Variant, Student, School, Stats, ImportResult
│  ├─ cli.py               # argparse CLI
│  ├─ db.py                # connect (WAL, self-healing) / schema / metadata
│  ├─ errors.py            # BaccError, NetworkError, InvalidDataError
│  ├─ importer.py          # CSV decode + validation
│  ├─ naming.py            # display-name composition
│  ├─ queries.py           # search SQL + terminal formatting
│  ├─ translations.py      # remark glossary JSON
│  ├─ spine_remark_translate.json  # packaged glossary: the base translations
│  ├─ schools.py           # schools.json
│  ├─ remote.py            # GitHub release update
│  ├─ paths.py             # default file locations
│  └─ py.typed
└─ tests/                  # pytest; factory.py builds synthetic CSV/schools, no upstream data
```

## Install / run

```sh
uv add git+https://github.com/com55/BACC@main     # use it as a library in another project
uv tool install git+https://github.com/com55/BACC  # or just the `bacc` command
```

From a checkout:

```sh
uv sync                      # runtime deps (platformdirs) + editable install
uv sync --group dev          # adds pytest
uv run bacc stats                    # console script
uv run python -m bacc stats
```

## Commands

```sh
uv run bacc update                          # check releases/latest, skip unchanged tag
uv run bacc update --force                  # re-download and rebuild the same tag
uv run bacc import students_data.csv        # import a CSV you downloaded earlier (offline)
uv run bacc search "Mika"                   # base names and variants (checks upstream when due)
uv run bacc search "Mika" --offline         # never touch the network
uv run bacc search "Mika" --format json     # machine-readable output on stdout
uv run bacc search "NP0100"                 # internal file ID
uv run bacc search "圣三一"                   # untranslated source fields
uv run bacc untranslated                    # remarks that still need mapping
uv run bacc translate "流血" "Bleeding"       # save your own mapping + refresh derived names
uv run bacc sync                            # re-apply the glossary to the DB (offline)
uv run bacc sync-schools                    # refresh school names without reimporting
uv run bacc clean                           # forget the imported catalog (JSON files are kept)
uv run bacc stats
```

Everything (database, `schools.json`, the glossary) lives in one **data directory**:
`--data-dir` > `$BACC_DATA_DIR` > the per-user directory from
`platformdirs.user_data_dir("BACC")` (`~/.local/share/BACC` on Linux), so results do not depend on
the working directory. A fresh directory is fine: the first `search` or `update` downloads
everything and creates the JSON files. Global flags must be written **before** the subcommand:

```sh
uv run bacc --data-dir ./bacc-data search "Mika"
uv run bacc --proxy http://proxy:3128 update
uv run bacc --db ./custom.db --schools ./schools.json sync-schools
uv run bacc -q search "Mika"    # progress logs off; errors still go to stderr
```

Data goes to stdout and progress/warnings to stderr, so `bacc search … --format json` pipes
cleanly into `jq`.

## Python API

```python
from bacc import BlueArchiveCharacterCatalog

catalog = BlueArchiveCharacterCatalog()   # per-user data dir, independent of the cwd
catalog.search('mika', limit=3)[0].display_name_en
# 'Mika'
```

`BlueArchiveCharacterCatalog` is the whole API: it owns the database path and the two editable JSON files,
opens the connection on first use, and returns frozen dataclasses from
`bacc.models`. Pass `data_dir` to move everything at once, or override single files
(keyword-only, so a `.db` path can never be mistaken for a directory):

```python
catalog = BlueArchiveCharacterCatalog('./bacc-data', proxy='http://proxy:3128')
catalog = BlueArchiveCharacterCatalog(
    db_path='ba_characters.db',                      # e.g. off a network filesystem
    translations_path='my-glossary.json',
    schools_path='schools.json',
)

with BlueArchiveCharacterCatalog() as catalog:   # or call catalog.close()
    print(catalog.stats().variants)
```

### Queries

| Method | Returns |
|---|---|
| `search(query, limit=30, *, refresh=None, update_background=False)` | `list[Variant]`, best match first |
| `variants(student_id=None, file_id=None)` | `list[Variant]` |
| `students(school_id=None)` | `list[Student]` |
| `schools()` | `list[School]` |
| `untranslated(limit=100)` | `list[UntranslatedRemark]`, most used first |
| `stats()` | `Stats` |
| `translations()` | `dict[str, str]`, the glossary in effect (packaged + yours) |
| `revision()` | `Revision`, when the content last changed plus a digest of it |
| `is_current(revision)` | `bool`, whether the catalog still holds exactly that content |

Whitespace separates words and **one field must contain every word** (any order): AND
applies inside a field, not across fields. `search('mika swimsuit')` finds rows whose
display name (or another single field) contains both words, so it narrows `search('mika')`.

A `field:value` token searches that one field only (the value must be contained in it), and
`field=value` needs the **whole value to equal** it, ignoring case. Both combine with plain words
(AND):

```python
catalog.search('file_id:NP0018')                    # contains: any file ID with NP0018 in it
catalog.search('file_id=NP0018')                    # exactly NP0018
catalog.search('file_id=NP0018 swimsuit')           # scoped term AND a plain word
catalog.search('school_name_en:trinity mika')
catalog.search('"display_name_en=Oil Drum"')        # double quotes keep spaces in one token
catalog.search('display_name_en:"Oil Drum"')        # same thing
catalog.search('"Oil Drum"')                        # quoted plain phrase, inside one field
```

Fields are the CSV columns plus `display_name_en`, `spine_remark_en` and `school_name_en`
(lowercase). Text before the colon or equals sign that is not a field name (`12:30`) stays an
ordinary word, and an unbalanced quote is treated as plain text. Apostrophes and backslashes are
ordinary characters.

```python
catalog.search('NP0100')               # a file ID
catalog.search('圣三一')                # an untranslated Chinese field
catalog.variants(file_id='NP0018')     # file IDs are not unique upstream
catalog.variants(student_id='1')       # every costume/portrait of one student

mika = catalog.search('mika', limit=1)[0]
mika.untranslated                      # False — the remark has an English name
mika.school                            # 'Trinity General School'
mika.matched                           # (('full_en', 'Misono Mika'), ('given_en', 'Mika'), …) as in the CLI
mika.matches('mika')                   # the same, computed for any query
```

`Variant` carries `file_id`, `student_id`, `spine_id`, `page_id`, `given_en`, `full_en`,
`skin_en`, `spine_remark`, `spine_remark_en`, `display_name_en`, `school_id`, `school_name`,
`school_name_en`, plus `source` (every original CSV field). `matched` is filled by `search()` (empty from `variants()`); `matches()` and the properties
are computed, not stored.

### Imports and edits

| Method | Effect |
|---|---|
| `import_csv(blob, *, tag='local-import', source='')` | Replace the snapshot from CSV `bytes` → `ImportResult` |
| `update(force=False)` | Import the upstream release when its content changed; `None` if current |
| `translate(original, english)` | Save one entry of your own, then refresh the derived names; empty `english` removes it |
| `sync_translations()` | Recompute names from the glossary in effect |
| `set_name_override(file_id, name, *, mode='base')` | Save one name override of your own (`'base'` or `'full'`); an empty name removes it |
| `name_overrides()` | `dict[str, NameOverride]`, the overrides in effect (packaged + yours) |
| `sync_schools()` | Refresh school names without reimporting the CSV |
| `clean()` | Empty the imported catalog and freshness records (the JSON files are kept) |

```python
catalog.translate('战损', 'Battered')
catalog.variants(file_id='NP0041')[0].display_name_en   # 'Mika (Battered)'

from pathlib import Path
catalog.import_csv(Path('students_data.csv').read_bytes())
```

**Name overrides.** When upstream has no English name (a variant still shows `NP0121`), give the
file ID one, the same way as the remark glossary: a packaged `name_overrides.json` is the base,
your own file of the same name in the data directory overrides it, and importing never writes that
file. The key is the file ID, and the override applies to every row that carries it.
`mode='base'` (the default) replaces only the base name, so labels are still appended
(`Name (Swimsuit)`); `mode='full'` replaces the whole `display_name_en`:

```python
catalog.set_name_override('NP0121', 'Some Name')                 # 'Some Name (Swimsuit)'
catalog.set_name_override('NP0018', 'Oil Drum', mode='full')     # exactly 'Oil Drum'
catalog.set_name_override('NP0121', '')                          # remove yours; packaged applies
catalog.name_overrides()          # dict[str, NameOverride], packaged + yours
```

CLI: `bacc name-override NP0121 "Some Name" [--mode full]`. In the JSON, a plain string is base
mode and `{"name": "Oil Drum", "mode": "full"}` is full mode. The original value stays in
`Variant.source`. The package ships three entries (`NP0018`, `NP0081`, `NP0082`).

`import_csv` takes bytes, not a path: the CLI reads the file, and callers that already have
a download, an HTTP body or an archive entry pass it straight through. Everything is
validated before the first write and the snapshot swap is one transaction, so a bad input
leaves the previous catalog intact.

### Revision

A client that keeps what it read can ask later whether it is still current, without comparing
rows:

```python
rev = catalog.revision()       # Revision(changed_at='20261122T120000Z', digest='1a2b3c4d5e6f7a8b')
token = str(rev)               # '20261122T120000Z-1a2b3c4d5e6f7a8b': store it next to your data
...
catalog.is_current(token)      # False once the content has changed (a Revision works too)
```

The digest covers every row a query can return (variants, students, schools). It moves with an
import that brings new data, `translate`, `set_name_override`, `sync_*` and `clean` when they
change a row, and stays put when an update finds nothing new or an edit is repeated or undone.
`changed_at` is informational: only the digest is compared, so a database rebuilt with identical
content is still current. `stats().revision` and `bacc stats` show the token. The revision is
refreshed in the same transaction as the write, so a reader never sees new data under an old one.

### Freshness

`search()` keeps the catalog current on its own, the way bagfd's `query()` does, using three
throttles so a search never hangs or fails because of the network:

| Layer | Stored where | Default |
|---|---|---|
| check interval | `metadata.last_check` | 4 hours (`check_interval=`) |
| failure defer | `metadata.defer_until` | 10 minutes after a network error, 6 hours after bad data |
| opt out | constructor / call | `auto_update=False`, `refresh=False` |

```python
catalog = BlueArchiveCharacterCatalog()                 # auto-update on, 4-hour interval
catalog.search('mika')                                  # refreshes only when due
catalog.search('mika', refresh=False)                   # strictly offline
offline = BlueArchiveCharacterCatalog(auto_update=False)
throttled = BlueArchiveCharacterCatalog(check_interval=timedelta(hours=12))

# Non-blocking: answer from what is cached now, refresh on a daemon thread
catalog.search('mika', update_background=True)
```

A due check always re-fetches, because upstream can publish a same-tag hotfix; the **SHA-256**
of the CSV — not the tag — decides whether the snapshot is rebuilt. If the check fails while a
catalog already exists, the error is logged to stderr, a defer window is parked, and the
cached rows are served. Only a catalog with no `release_tag` yet propagates the error, since
there is nothing cached to fall back on.

`update_background=True` runs a due refresh on a daemon thread (one at a time per instance) and
answers from the catalog as it is: possibly stale, or **empty if it was never populated**.

### Errors and self-healing

Failures are typed, and each also subclasses the builtin it replaces (`OSError` / `ValueError`),
so existing `except` clauses still work:

| Error | Meaning | What happens automatically |
|---|---|---|
| `NetworkError` | release or download unreachable | cached catalog served, retry after 10 minutes |
| `InvalidDataError` | bad CSV / zip / `schools.json` | previous catalog untouched, retry after 6 hours |

Beyond that: the release zip is tried first and the standalone CSV asset is the fallback, so one
bad asset does not block an update; a zip member over the size limit is refused; a database file
that is not valid SQLite is renamed to `<name>.corrupt-<time>` and rebuilt on the next refresh
(the catalog is a cache; the glossary is packaged or stored in JSON, so nothing is lost).

### Several processes

The database runs in SQLite **WAL** mode: any number of processes can search at once, and
readers keep seeing the previous snapshot until an import commits. A due check is claimed inside
one write transaction, so concurrent searches trigger one download, not one each. Give each
thread its own `BlueArchiveCharacterCatalog`. WAL does not work on network filesystems; put
`db_path` on a local disk.

### Connection-level helpers

For code that wants SQL of its own, `catalog.connection` is a plain `sqlite3.Connection`
with `row_factory = sqlite3.Row` and foreign keys ON. The module-level functions take a
connection instead of a catalog instance:

```python
from bacc import connect, find_variants, meta
from bacc.queries import format_variant

con = connect('ba_characters.db')   # same WAL / self-healing open the class uses
for variant in find_variants(con, 'mika', limit=5):
    print(format_variant(variant, 'mika'))
```

### Path resolution

The data directory is `data_dir=` > `$BACC_DATA_DIR` >
`platformdirs.user_data_dir("BACC")`; it holds `ba_characters.db`, `schools.json`,
`spine_remark_translate.json`, `name_overrides.json` and the `release/` folder. Each can be moved on its own with
`db_path=`, `translations_path=`, `schools_path=`, `name_overrides_path=`, `release_dir=` or `BACC_DB`,
`BACC_TRANSLATIONS`, `BACC_SCHOOLS`, `BACC_NAME_OVERRIDES`, `BACC_RELEASE_DIR`
(explicit argument > environment variable > file inside the data directory).
**Glossary:** the translations packaged with the library
(`src/bacc/spine_remark_translate.json`) are the base and are never copied out. Your own
entries live in `spine_remark_translate.json` in the data directory, which is created only the
first time you `translate` something and holds just your entries. The two are merged with
yours first: a non-empty entry of yours wins; an empty one means "no override", so it never
blanks out a packaged translation. Importing writes no glossary file. A missing `schools.json`
is fetched by the first online update.

Upgrading from 0.5: the database and JSON files are no longer looked up in the working
directory. Copy them into the new data directory, or point `BACC_DATA_DIR` / `BACC_DB` at them.
A 0.5 glossary file is a full copy; it still works as your overrides, and its empty entries
are ignored.
The repository no longer carries upstream data files; `schools.json` and the CSV are downloaded
from BA-characters-internal-id on demand.

## Tables

- `students`: one row per `student_id`, base English name and school ID.
- `variants`: one row **per CSV record**, with `id` as primary key. `file_id` is **not unique**
  in the source. Fields include `file_id`, `student_id`, `spine_id`, `page_id`, English base and
  skin names, original and English remarks, and computed `display_name_en`. `source_json`
  preserves every original CSV field for future migration/search.
- `schools`: `school_id`, original Chinese `school_name` from the CSV, plus `school_name_cn`,
  `school_name_en` and `logo` from `schools.json`.
- `remark_translations`: an editable glossary, independent of the imported release snapshot.
- `metadata`: last imported tag, timestamp, SHA256 and asset URL.

## Update behaviour

1. Request GitHub REST `releases/latest` for the upstream repository.
2. Find the `BA-Characters-Internal-ID.zip` and `students_data.csv` assets in `assets[]`.
3. Download the zip, import `students_data.csv` and read `metadata.json` (upstream date, code
   version, data commit; shown by `stats`). **Every file of the zip is also kept verbatim** in
   `<data dir>/release/` (`LICENSE-DATA.txt`, `students.json`, `spines.json`, ...), replaced as a
   whole set on each import. If the zip is missing, corrupt or fails validation, fall back to
   the standalone CSV asset (which has no extra files, so `release/` is left as it is).
   Refresh `schools.json` from the upstream `data` branch.
4. Compare **tag and SHA-256** against `metadata`. Same tag + same bytes → refresh schools and
   translations, record the check, and stop without touching the snapshot. `--force` rebuilds anyway.
5. Otherwise validate required columns and records and refresh `students`, `schools`, `variants`
   in **one SQLite transaction**. Failed downloads/parsing leave the prior catalog intact.
6. Keep `remark_translations` across updates; `translate` recomputes names immediately.
7. Record `metadata.last_check`, which clears any `defer_until` window. A failure instead parks
   a defer window (see *Errors and self-healing*).

`import` records the tag as `local-import`, so the next online `update` imports the latest release.

**Known limits:** this is a catalog importer, not a translation engine; search uses substring
`LIKE` with no stemming or romanization. Missing required source fields are rejected rather than
silently dropped.

## Data license

Data comes from [Agent-0808/BA-characters-internal-id](https://github.com/Agent-0808/BA-characters-internal-id),
reported as **CC BY-SA 4.0**. See [THIRD_PARTY_DATA.md](THIRD_PARTY_DATA.md). If distributing a
SQLite database containing this data, provide attribution, a link to the license, note
modifications, and follow ShareAlike for derivatives. The package code is independent and
released under the [MIT License](LICENSE).
