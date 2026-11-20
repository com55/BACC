"""Display-name composition for catalog variants."""

from __future__ import annotations


def make_display(row: dict[str, str], translated: str) -> str:
    """Compose the display name: base English name plus costume/remark labels."""
    # skin_en is usually an official costume; remark is a story/portrait variant.
    base = row['given_en'] or row['full_en'] or row['file_id']
    extras = []
    for label in (row['skin_en'], translated):
        if label and label.casefold() != 'original' and label.casefold() not in [
            x.casefold() for x in extras
        ]:
            extras.append(label)
    return base + (f" ({', '.join(extras)})" if extras else '')
