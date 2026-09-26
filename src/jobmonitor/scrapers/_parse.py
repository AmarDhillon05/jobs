"""Small tolerant accessors shared by the provider adapters.

Adapters read *other people's* JSON. Fields move, get renamed between API
versions, arrive as ``null`` instead of absent, or come back as a bare string
where an object is documented. These helpers absorb that variation so each
adapter reads as a description of the payload rather than a pile of
``isinstance`` checks - and so a single renamed field degrades one column instead
of raising and losing the whole response.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from jobmonitor.models.job import clean_text


def as_mapping(value: Any) -> Mapping[str, Any]:
    """``value`` if it is a mapping, else an empty one."""
    return value if isinstance(value, Mapping) else {}


def as_sequence(value: Any) -> Sequence[Any]:
    """A list-like view of ``value``, treating strings and mappings as scalars."""
    if value is None:
        return ()
    if isinstance(value, (str, bytes, Mapping)):
        return (value,)
    if isinstance(value, Sequence):
        return value
    if isinstance(value, Iterable):
        return tuple(value)
    return (value,)


def text(value: Any) -> str | None:
    """Best-effort string, or ``None`` for anything unusable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, (int, float)):
        return str(value)
    return None


def first_of(data: Any, *keys: str) -> Any:
    """First key present with a non-empty value, walking dotted paths.

    >>> first_of({"a": {"b": "x"}}, "missing", "a.b")
    'x'
    """
    mapping = as_mapping(data)
    for key in keys:
        current: Any = mapping
        for part in key.split("."):
            current = as_mapping(current).get(part) if not isinstance(current, str) else None
            if current is None:
                break
        if current not in (None, "", [], {}):
            return current
    return None


def text_of(data: Any, *keys: str) -> str | None:
    return text(first_of(data, *keys))


def label_of(value: Any, *keys: str) -> str | None:
    """Read a field that may be a plain string or a ``{"label"/"name": ...}`` object."""
    if isinstance(value, str):
        return clean_text(value)
    mapping = as_mapping(value)
    if not mapping:
        return None
    return text_of(mapping, *(keys or ("label", "name", "text", "value")))


def join_locations(parts: Iterable[str | None], *, separator: str = "; ") -> str | None:
    """Join distinct, non-empty location strings, preserving first-seen order."""
    seen: dict[str, None] = {}
    for part in parts:
        cleaned = clean_text(part)
        if cleaned:
            seen.setdefault(cleaned, None)
    return separator.join(seen) if seen else None


def compose_location(*components: Any) -> str | None:
    """Build "City, Region, Country" from whatever subset is present."""
    pieces = [clean_text(text(component)) for component in components]
    present = [piece for piece in pieces if piece]
    return ", ".join(dict.fromkeys(present)) if present else None


__all__: Sequence[str] = (
    "as_mapping",
    "as_sequence",
    "compose_location",
    "first_of",
    "join_locations",
    "label_of",
    "text",
    "text_of",
)
