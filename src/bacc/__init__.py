"""Blue Archive Characters Catalog — import the BA-characters-internal-id release CSV into SQLite.

Data: https://github.com/Agent-0808/BA-characters-internal-id
Dataset license: CC BY-SA 4.0. Preserve attribution when redistributing data.

Usage::

    >>> from bacc import BlueArchiveCharacterCatalog
    >>> catalog = BlueArchiveCharacterCatalog()          # per-user data dir, not the cwd
    >>> catalog.search('mika', limit=1)[0].display_name_en
    'Mika'
    >>> catalog.stats().variants
    670

Results are dataclasses from :mod:`bacc.models`; helpers that take a
raw ``sqlite3.Connection`` live in :mod:`bacc.queries`.
"""

from __future__ import annotations

from .app import BlueArchiveCharacterCatalog
from .db import (
    DEFAULT_CHECK_INTERVAL,
    DEFAULT_DEFER_ON_FAILURE,
    DEFAULT_DEFER_ON_INVALID,
    SCHEMA_VERSION,
    claim_check,
    connect,
    defer_check,
    initialize,
    mark_checked,
    meta,
    release_claim,
    set_meta,
    should_check,
)
from .errors import BaccError, InvalidDataError, NetworkError
from .importer import FIELDS, MAX_CSV_BYTES, decode_rows
from .models import (
    ImportResult,
    School,
    Stats,
    Student,
    UntranslatedRemark,
    Variant,
)
from .naming import NameOverride, make_display
from .paths import (
    data_dir,
    default_db,
    default_name_overrides,
    default_release_dir,
    default_schools,
    default_translations,
)
from .queries import find_variants, format_variant, run_search
from .remote import REPO, Release, ReleaseData, fetch_release_data, latest_release
from .revision import Revision
from .schools import apply_schools, decode_schools, read_schools
from .translations import (
    merged_name_overrides,
    merged_translations,
    packaged_name_overrides,
    packaged_translations,
    read_name_overrides,
    read_translations,
    save_name_overrides,
    save_translations,
    sync_translations,
)

__version__ = '0.7.0'

__all__ = [
    'DEFAULT_CHECK_INTERVAL',
    'DEFAULT_DEFER_ON_FAILURE',
    'DEFAULT_DEFER_ON_INVALID',
    'FIELDS',
    'MAX_CSV_BYTES',
    'REPO',
    'SCHEMA_VERSION',
    'BaccError',
    'BlueArchiveCharacterCatalog',
    'ImportResult',
    'InvalidDataError',
    'NameOverride',
    'NetworkError',
    'Release',
    'ReleaseData',
    'Revision',
    'School',
    'Stats',
    'Student',
    'UntranslatedRemark',
    'Variant',
    '__version__',
    'apply_schools',
    'claim_check',
    'connect',
    'data_dir',
    'decode_rows',
    'decode_schools',
    'default_db',
    'default_name_overrides',
    'default_release_dir',
    'default_schools',
    'default_translations',
    'defer_check',
    'fetch_release_data',
    'find_variants',
    'format_variant',
    'initialize',
    'latest_release',
    'make_display',
    'mark_checked',
    'merged_name_overrides',
    'merged_translations',
    'meta',
    'packaged_name_overrides',
    'packaged_translations',
    'read_name_overrides',
    'read_schools',
    'read_translations',
    'release_claim',
    'run_search',
    'save_name_overrides',
    'save_translations',
    'set_meta',
    'should_check',
    'sync_translations',
]
