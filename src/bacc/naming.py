"""Display-name composition for catalog variants."""

from __future__ import annotations

from dataclasses import dataclass

MODES = ('base', 'full')


@dataclass(frozen=True)
class NameOverride:
    """Your own name for a file ID.

    ``mode='base'`` replaces only the base name, so the costume and remark labels
    are still appended (``Name (Swimsuit)``); ``mode='full'`` replaces the whole
    display name, labels included.
    """

    name: str
    mode: str = 'base'


def make_display(
    row: dict[str, str], translated: str, override: NameOverride | None = None
) -> str:
    """Compose the display name: base English name plus costume/remark labels."""
    if override is not None and override.mode == 'full':
        return override.name
    # skin_en is usually an official costume; remark is a story/portrait variant.
    base = (override.name if override else '') or row['given_en'] or row['full_en'] or row['file_id']
    extras = []
    for label in (row['skin_en'], translated):
        if label and label.casefold() != 'original' and label.casefold() not in [
            x.casefold() for x in extras
        ]:
            extras.append(label)
    return base + (f" ({', '.join(extras)})" if extras else '')
