"""Default file locations.

The library does not ship data files inside the wheel: the catalog database and
the editable JSON glossaries are user state that lives beside the project.
Everything lives in one per-user data directory by default, so the catalog does
not depend on the current working directory. Override with the environment
variables below or by passing explicit paths to the functions that accept one.
"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_dir

# The short names are the documented ones; the long names keep older setups working.
ENV_DATA_DIR = ('BACC_DATA_DIR', 'BLUEARCHIVECHARACTERSCATALOG_DATA_DIR')
ENV_DB = ('BACC_DB', 'BLUEARCHIVECHARACTERSCATALOG_DB')
ENV_TRANSLATIONS = ('BACC_TRANSLATIONS', 'BLUEARCHIVECHARACTERSCATALOG_TRANSLATIONS')
ENV_SCHOOLS = ('BACC_SCHOOLS', 'BLUEARCHIVECHARACTERSCATALOG_SCHOOLS')
ENV_RELEASE_DIR = ('BACC_RELEASE_DIR',)

_APP_NAME = 'BACC'
_DB_NAME = "ba_characters.db"
_TRANSLATIONS_NAME = "spine_remark_translate.json"
_SCHOOLS_NAME = "schools.json"
_RELEASE_DIR_NAME = "release"


def _env(names: tuple[str, ...]) -> str | None:
    """First of ``names`` that is set and non-empty, newest spelling first."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def data_dir() -> Path:
    """Directory holding the database and the editable JSON files.

    Priority: ``BACC_DATA_DIR`` > ``platformdirs.user_data_dir("BACC")``
    (``XDG_DATA_HOME/BACC`` on Linux, ``%LOCALAPPDATA%\\BACC`` on Windows).
    """
    override = _env(ENV_DATA_DIR)
    return Path(override) if override else Path(user_data_dir(_APP_NAME))


def default_db(base: Path | str | None = None) -> Path:
    """SQLite catalog database path (``BACC_DB`` > ``base``/ba_characters.db)."""
    override = _env(ENV_DB)
    return Path(override) if override else (Path(base) if base else data_dir()) / _DB_NAME


def default_translations(base: Path | str | None = None) -> Path:
    """Editable JSON glossary: original remark -> English."""
    override = _env(ENV_TRANSLATIONS)
    return Path(override) if override else (Path(base) if base else data_dir()) / _TRANSLATIONS_NAME


def default_schools(base: Path | str | None = None) -> Path:
    """Official school metadata JSON."""
    override = _env(ENV_SCHOOLS)
    return Path(override) if override else (Path(base) if base else data_dir()) / _SCHOOLS_NAME


def default_release_dir(base: Path | str | None = None) -> Path:
    """Directory holding the files of the last release zip, verbatim."""
    override = _env(ENV_RELEASE_DIR)
    return Path(override) if override else (Path(base) if base else data_dir()) / _RELEASE_DIR_NAME
