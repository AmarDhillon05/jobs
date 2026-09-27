"""Search terms for the adapters that have to ask the site to filter.

Most ATS APIs hand over a whole board, and the relevance filter decides what is
an internship. Some sites are too large for that - Amazon, JPMorgan, Microsoft
list thousands of roles - so the adapter must search, and a search can only find
what its keyword names.

One keyword is not enough, and that was measured, not assumed:

* Goldman Sachs titles its internships "Summer Analyst"; a search for "intern"
  finds one posting out of 282 campus roles.
* Morgan Stanley's "intern" search returns 262 roles and "summer analyst" 74
  more, largely disjoint (the Parametric summer cohorts, for example).

So these adapters run every term in :data:`DEFAULT_QUERIES` and the base class's
de-duplication merges the overlap. A company can override the list with
``provider_config["queries"]``; the older single-string keys (``query``,
``keyword``, ``keywords``) still work.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

DEFAULT_QUERIES: tuple[str, ...] = (
    "intern",
    "internship",
    "co-op",
    "summer analyst",
    "summer associate",
)


def configured_queries(
    config: Mapping[str, Any],
    *legacy_keys: str,
    default: Sequence[str] = DEFAULT_QUERIES,
) -> tuple[str, ...]:
    """The search terms for one company: ``queries`` first, then any legacy key."""
    for key in ("queries", *legacy_keys):
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            return (value.strip(),)
        if isinstance(value, (list, tuple)):
            terms = [str(item).strip() for item in value if str(item).strip()]
            if terms:
                return tuple(dict.fromkeys(terms))  # de-duplicated, order kept
    return tuple(default)


__all__: Sequence[str] = ("DEFAULT_QUERIES", "configured_queries")
