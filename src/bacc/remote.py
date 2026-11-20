"""GitHub release lookup and download (the only network access in the package)."""

from __future__ import annotations

import http.client
import io
import json
import logging
import os
import shutil
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from .errors import InvalidDataError, NetworkError
from .importer import MAX_CSV_BYTES, decode_rows
from .paths import default_schools
from .schools import decode_schools

logger = logging.getLogger(__name__)

REPO = 'Agent-0808/BA-characters-internal-id'
LATEST_API = f'https://api.github.com/repos/{REPO}/releases/latest'
SCHOOLS_URL = f'https://raw.githubusercontent.com/{REPO}/data/output/schools.json'
USER_AGENT = 'bacc'
CSV_ASSET = 'students_data.csv'
ZIP_ASSET = 'BA-Characters-Internal-ID.zip'
MAX_ZIP_BYTES = 64 * 1024 * 1024  # all members together, once decompressed
_ASSET_PREFIX = f'https://github.com/{REPO}/releases/download/'
# metadata.json keys worth keeping, stored under the metadata table's own names.
_INFO_KEYS = {
    'updateDate': 'upstream_date',
    'codeVersion': 'upstream_code_version',
    'dataCommitHash': 'upstream_data_commit',
}


_WINDOWS_RESERVED = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                     *(f'LPT{i}' for i in range(1, 10))}


def _safe_member_name(name: str) -> bool:
    """True for a plain file name that is the same on every OS, so it cannot escape a folder."""
    if not name or name in {'.', '..'} or any(c in name for c in ':\\\0/') or name != name.strip():
        return False
    if PurePosixPath(name).name != name or PureWindowsPath(name).name != name:
        return False
    return name.split('.')[0].upper() not in _WINDOWS_RESERVED


@dataclass(frozen=True)
class Release:
    """The upstream release: its tag and the assets the catalog can be built from.

    The zip bundles the CSV with ``metadata.json``; the standalone CSV asset is
    the fallback when the zip is missing or unusable.
    """

    tag: str
    csv_url: str = ''
    zip_url: str = ''


@dataclass(frozen=True)
class ReleaseData:
    """A validated CSV plus whatever the chosen asset said about itself."""

    csv: bytes
    source: str
    info: dict[str, str] = field(default_factory=dict)
    # Every file of the zip by name (empty when the CSV asset was used instead).
    files: dict[str, bytes] = field(default_factory=dict, repr=False)


def _open(url: str, headers: dict[str, str], proxy: str | None) -> object:
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, **headers})
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({'http': proxy, 'https': proxy})
        )
        return opener.open(request, timeout=50)
    return urllib.request.urlopen(request, timeout=50)


def latest_release(proxy: str | None = None) -> Release:
    """Resolve ``releases/latest`` to its tag and the zip / CSV assets."""
    try:
        with _open(
            LATEST_API,
            {
                'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28',
            },
            proxy,
        ) as response:
            data = json.load(response)
    except (OSError, http.client.HTTPException) as exc:  # URLError, HTTPError, timeouts, truncated reads
        raise NetworkError(f'Could not reach {LATEST_API}: {exc}') from exc
    except ValueError as exc:
        raise InvalidDataError(f'Release API returned invalid JSON: {exc}') from exc
    if not isinstance(data, dict):
        raise InvalidDataError('Release API returned an unexpected payload')
    tag = str(data.get('tag_name') or '')
    if not tag:
        raise InvalidDataError('Latest release did not contain tag_name')

    def asset(name: str) -> str:
        found = [a for a in data.get('assets', []) if a.get('name') == name]
        if len(found) > 1:
            raise InvalidDataError(f'Expected at most one {name} release asset, found {len(found)}')
        if not found:
            return ''
        url = str(found[0].get('browser_download_url') or '')
        if not url.startswith(_ASSET_PREFIX):
            raise InvalidDataError('Unexpected release asset URL')
        return url

    release = Release(tag=tag, csv_url=asset(CSV_ASSET), zip_url=asset(ZIP_ASSET))
    if not (release.csv_url or release.zip_url):
        raise InvalidDataError(f'Release {tag} has neither {ZIP_ASSET} nor {CSV_ASSET}')
    return release


def _read_zip(blob: bytes) -> ReleaseData:
    """Read every file of the release zip; ``students_data.csv`` is required."""
    files: dict[str, bytes] = {}
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            for info in archive.infolist():
                name = info.filename
                if info.is_dir():
                    continue
                if not _safe_member_name(name):
                    logger.warning('Ignoring %r in %s: only top-level files are kept', name, ZIP_ASSET)
                    continue
                total += info.file_size  # header value, so also bound the read below
                if info.file_size > MAX_CSV_BYTES or total > MAX_ZIP_BYTES:
                    raise InvalidDataError(f'{name} is larger than allowed size')
                with archive.open(info) as handle:
                    data = handle.read(MAX_CSV_BYTES + 1)
                if len(data) > MAX_CSV_BYTES:
                    raise InvalidDataError(f'{name} is larger than allowed size')
                files[name] = data
    except zipfile.BadZipFile as exc:
        raise InvalidDataError(f'{ZIP_ASSET} is not a valid zip: {exc}') from exc
    csv_blob = files.get(CSV_ASSET)
    if csv_blob is None:
        raise InvalidDataError(f'{ZIP_ASSET} has no {CSV_ASSET}')
    info_map: dict[str, str] = {}
    if meta_blob := files.get('metadata.json'):
        try:
            raw = json.loads(meta_blob.decode('utf-8-sig'))
        except ValueError:
            raw = {}  # provenance only: a bad metadata.json must not block the import
        if isinstance(raw, dict):
            info_map = {
                ours: str(raw[theirs]) for theirs, ours in _INFO_KEYS.items() if theirs in raw
            }
    return ReleaseData(csv=csv_blob, source='', info=info_map, files=files)


def fetch_release_data(release: Release, proxy: str | None = None) -> ReleaseData:
    """Download and validate the catalog CSV, trying the zip first, then the CSV asset.

    A source that cannot be downloaded or fails validation is logged and skipped;
    the error is raised only when every source has failed. A network error wins
    over a data error, because the former is the one worth retrying soon.
    """
    errors: list[Exception] = []
    attempts = []
    if release.zip_url:
        attempts.append((release.zip_url, True))
    if release.csv_url:
        attempts.append((release.csv_url, False))
    for url, zipped in attempts:
        try:
            blob = _download(url, proxy)
            data = _read_zip(blob) if zipped else ReleaseData(csv=blob, source='')
            decode_rows(data.csv)  # validate here so a bad zip can still fall back to the CSV
        except (NetworkError, InvalidDataError) as exc:
            logger.warning('Release source %s unusable (%s)', url.rsplit('/', 1)[-1], exc)
            errors.append(exc)
            continue
        return ReleaseData(csv=data.csv, source=url, info=data.info, files=data.files)
    for exc in errors:
        if isinstance(exc, NetworkError):
            raise exc
    raise InvalidDataError(
        f'No usable data in release {release.tag}: ' + '; '.join(str(e) for e in errors)
    )


def download_schools(proxy: str | None = None) -> bytes:
    """Fetch ``schools.json``; it is not a release asset but lives on the data branch."""
    return _download(SCHOOLS_URL, proxy)


def _download(url: str, proxy: str | None = None) -> bytes:
    try:
        with _open(url, {}, proxy) as response:
            size = response.headers.get('Content-Length')
            if size and int(size) > MAX_CSV_BYTES:
                raise InvalidDataError('Downloaded file larger than allowed size')
            blob = response.read(MAX_CSV_BYTES + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise NetworkError(f'Could not download {url}: {exc}') from exc
    if len(blob) > MAX_CSV_BYTES:
        raise InvalidDataError('Downloaded file larger than allowed size')
    return blob


def save_schools(blob: bytes, path: Path | None = None) -> Path:
    """Validate ``schools.json`` and write the local copy atomically."""
    decode_schools(blob)  # refuse to overwrite the good copy with a bad download
    path = Path(path) if path is not None else default_schools()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(blob)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def save_release_files(files: dict[str, bytes], directory: Path) -> Path:
    """Replace ``directory`` with ``files`` (flat names), swapping the whole set at once."""
    directory = Path(directory)
    directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=directory.name + '.', suffix='.tmp', dir=directory.parent))
    old = staging.with_suffix('.old')
    try:
        for name, data in files.items():
            if not _safe_member_name(name):
                raise InvalidDataError(f'Unsafe release file name {name!r}')
            (staging / name).write_bytes(data)
        if directory.exists():
            os.replace(directory, old)
        os.replace(staging, directory)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)
    return directory
