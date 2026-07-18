"""One canonical, path-safe folder-alias contract for every component."""

from __future__ import annotations

import unicodedata

from .protocol import DriveRagError


def canonical_alias(
    value: object,
    *,
    code: str = "INVALID_ALIAS",
    require_canonical: bool = False,
) -> str:
    if not isinstance(value, str):
        raise DriveRagError("folder alias must be a string", code=code)
    canonical = unicodedata.normalize("NFC", value.strip())
    unsafe = (
        canonical in {"", ".", ".."}
        or "/" in canonical
        or "\\" in canonical
        or any(unicodedata.category(character).startswith("C") for character in canonical)
    )
    if unsafe:
        raise DriveRagError("folder alias is unsafe", code=code)
    if require_canonical and value != canonical:
        raise DriveRagError("folder alias is not canonical", code=code)
    return canonical


def alias_key(
    value: object,
    *,
    code: str = "INVALID_ALIAS",
    require_canonical: bool = False,
) -> str:
    return canonical_alias(
        value, code=code, require_canonical=require_canonical
    ).casefold()
