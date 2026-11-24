"""Deterministic JSON nesting guard.

Relying on CPython's ``RecursionError`` is not portable: the C scanner accepts
different depths on different builds/platforms. This check is explicit, cheap
and behaves identically everywhere.
"""

from __future__ import annotations

MAX_JSON_DEPTH = 200


class JsonTooDeep(ValueError):
    """Raised when a JSON document exceeds the allowed nesting depth."""


def ensure_json_depth(text: str, max_depth: int = MAX_JSON_DEPTH) -> None:
    """Reject JSON with more than ``max_depth`` nested arrays/objects.

    String contents are skipped so braces inside quoted values do not count.
    """
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > max_depth:
                raise JsonTooDeep(f"JSON nesting exceeds {max_depth} levels")
        elif character in "]}":
            depth -= 1
