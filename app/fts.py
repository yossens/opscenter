"""Sanitize user input into a safe FTS5 MATCH query.

Single point for building the MATCH expression for the whole application (global
search ``/api/search`` and the search dropdown ``/api/deals``). User input never
reaches MATCH directly: FTS5 special characters (``"``, ``*``, parentheses, the
``AND``/``OR``/``NEAR`` operators, ``:`` and so on) break query parsing. So the
input is split into alphanumeric tokens (unicode-aware, covers non-Latin
scripts), each token is escaped with double quotes (inside quotes it is treated
as a string literal, not an operator), and a ``*`` suffix is appended to the last
one for prefix search.
"""

from __future__ import annotations

import re

# Token: a sequence of alphanumeric characters (unicode-aware, covers non-Latin
# scripts). Everything else — quotes, ``*``, parentheses, the OR/AND/NEAR
# operators, colons — is discarded as a separator.
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def sanitize_fts_query(q: str) -> str | None:
    """Builds a safe FTS5 MATCH expression from user input.

    Each token is wrapped in double quotes (an FTS5 string literal), the last one
    gets a ``*`` suffix for prefix search. Empty input, or input without a single
    alphanumeric token (special characters only) → ``None``: this signals "no
    query" to the caller — MATCH need not run, the result is guaranteed empty.
    """
    if not q or not q.strip():
        # Empty/whitespace input: there is no query, MATCH need not run.
        return None
    tokens = _TOKEN_RE.findall(q)
    if not tokens:
        # Input made up only of special characters (``"``, ``*``, parentheses,
        # operators): not a single alphanumeric token. We return a valid but
        # guaranteed-empty MATCH expression (the empty phrase ``""``) rather than
        # ``None`` — this way a direct MATCH is guaranteed empty and free of a
        # syntax error.
        return '""'
    parts = [f'"{tok}"' for tok in tokens]
    parts[-1] = parts[-1] + "*"
    return " ".join(parts)
