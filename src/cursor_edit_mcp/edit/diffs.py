"""Compact unified diffs.

Responses carry a summary plus a bounded diff rather than the whole new file,
so that reading a 2,000-line file to confirm a three-line change stays cheap.
The cap is reported honestly: a diff that was cut says so and says how much
was dropped.
"""

from __future__ import annotations

import difflib

MAX_DIFF_LINES = 60


def unified_diff(
    before: str, after: str, path: str, max_lines: int = MAX_DIFF_LINES
) -> tuple[str, bool]:
    """Return a unified diff and whether it was truncated."""
    raw = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )
    if not raw:
        return "", False

    lines = raw.splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines), False

    kept = lines[:max_lines]
    dropped = len(lines) - max_lines
    kept.append(f"... diff truncated, {dropped} more line(s) omitted")
    return "\n".join(kept), True
