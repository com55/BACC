"""Exceptions raised by the package.

Each one also subclasses the builtin it replaces (``OSError`` / ``ValueError``),
so code that already catches those keeps working.
"""

from __future__ import annotations


class BaccError(Exception):
    """Base class for every error this package raises on purpose."""


class NetworkError(BaccError, OSError):
    """The upstream release could not be reached or downloaded.

    Transient: the cached catalog is still good, and the next check is retried
    after a short defer window.
    """


class InvalidDataError(BaccError, ValueError):
    """Downloaded or imported data failed validation (bad CSV, bad archive).

    The previous catalog is left untouched. Automatic refreshes back off for
    longer than they do after a :class:`NetworkError`, since the same bytes
    would fail again.
    """
