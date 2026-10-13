"""Anchor resolution: literal text and regular expressions.

Anchors are the generic addressing mode. They work in any file type, which
matters because the alternative — counting lines by eye — is exactly the error
class this server exists to prevent.

An anchor resolves to a line range. Matching text may span lines, in which case
the range covers every line the match touches.

Character offsets are converted to line numbers through a start-offset table
built from the text itself. Deriving line numbers from a list of lines without
their terminators silently drifts by one character per line, which lands an
anchor on the wrong line rather than failing loudly.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from ..errors import EditMcpError, anchor_not_found

#: Candidate listings are capped so a large file cannot blow up the response.
_MAX_CANDIDATES = 10


@dataclass(frozen=True)
class Span:
    """A resolved, 1-based inclusive line range."""

    start_line: int
    end_line: int
    match: str
    occurrence: int

    def as_dict(self) -> dict[str, object]:
        return {
            "start_line": self.start_line,
            "end_line": self.end_line,
            "match": self.match,
            "occurrence": self.occurrence,
        }


class LineIndex:
    """Maps character offsets to 1-based line numbers.

    Shared with the runners, which receive (row, column) pairs from external
    tools and need to turn them back into offsets.
    """

    __slots__ = ("_starts", "_text")

    def __init__(self, text: str) -> None:
        self._text = text
        starts = [0]
        for index, char in enumerate(text):
            if char == "\n":
                starts.append(index + 1)
        self._starts = starts

    @property
    def line_count(self) -> int:
        return len(self._starts)

    def offset(self, line: int, column: int) -> int:
        """Character offset of a 1-based (line, column), as external tools report."""
        if line < 1:
            return 0
        if line > len(self._starts):
            return len(self._text)
        start = self._starts[line - 1]
        return min(start + max(column - 1, 0), self._line_end(line))

    def _line_end(self, line: int) -> int:
        end = self._starts[line] if line < len(self._starts) else len(self._text) + 1
        return max(end - 1, self._starts[line - 1])

    def line_of(self, offset: int) -> int:
        """1-based line containing ``offset``."""
        low, high = 0, len(self._starts) - 1
        while low < high:
            mid = (low + high + 1) // 2
            if self._starts[mid] <= offset:
                low = mid
            else:
                high = mid - 1
        return low + 1

    def text_of(self, line: int) -> str:
        start = self._starts[line - 1]
        end = self._starts[line] if line < len(self._starts) else len(self._text) + 1
        return self._text[start : end - 1].rstrip("\r")

    def range_text(self, start_line: int, end_line: int) -> str:
        parts = [self.text_of(n) for n in range(start_line, end_line + 1)]
        return "\n".join(parts).strip()


def _span_for(index: LineIndex, start: int, end: int) -> tuple[int, int]:
    """Line range covering a half-open character span."""
    start_line = index.line_of(start)
    end_line = index.line_of(max(start, end - 1))
    return start_line, max(end_line, start_line)


def _match_candidates(
    index: LineIndex, spans: list[tuple[int, int]]
) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for position, (start, end) in enumerate(spans[:_MAX_CANDIDATES]):
        start_line, end_line = _span_for(index, start, end)
        out.append(
            {
                "index": position,
                "start_line": start_line,
                "end_line": end_line,
                "text": index.range_text(start_line, end_line)[:200],
            }
        )
    return out


def _similar_lines(index: LineIndex, query: str) -> list[dict[str, object]]:
    """Closest lines to the query, for typo recovery.

    A misspelled anchor is the common failure. Showing the nearest lines turns
    "not found" into a one-step correction.
    """
    needle = query.strip().splitlines()[0].strip() if query.strip() else ""
    if not needle:
        return []

    scored: list[tuple[float, int]] = []
    for line in range(1, index.line_count + 1):
        content = index.text_of(line)
        if not content.strip():
            continue
        ratio = difflib.SequenceMatcher(None, needle, content.strip()).ratio()
        if ratio >= 0.6:
            scored.append((ratio, line))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {"start_line": line, "end_line": line, "text": index.text_of(line).strip()[:200]}
        for _, line in scored[:5]
    ]


def _locate(
    spans: list[tuple[int, int]],
    index: LineIndex,
    text: str,
    *,
    path: str,
    kind: str,
    query: str,
    occurrence: int | None,
) -> tuple[int, int, int]:
    """Pick one span, or explain why no unambiguous choice exists."""
    if not spans:
        similar = _similar_lines(index, query)
        raise anchor_not_found(
            path,
            f"{kind} {query!r}",
            hint=(
                "Use text exactly as it appears in the file, or switch to a regex."
                + (" These lines look closest:" if similar else "")
            ),
            candidates=similar,
        )

    if occurrence is None:
        if len(spans) == 1:
            return (*spans[0], 0)
        located = ", ".join(str(c["start_line"]) for c in _match_candidates(index, spans))
        raise anchor_not_found(
            path,
            f"{kind} {query!r} ({len(spans)} matches)",
            hint=(
                f"The anchor is ambiguous. It matches at lines {located}. "
                "Pass occurrence: 0 for the first one."
            ),
            candidates=_match_candidates(index, spans),
        )

    if occurrence >= len(spans):
        raise anchor_not_found(
            path,
            f"{kind} {query!r} at occurrence {occurrence}",
            hint=(
                f"This anchor has {len(spans)} match(es), so valid occurrence "
                f"values are 0 to {len(spans) - 1}."
            ),
            occurrence=occurrence,
        )

    return (*spans[occurrence], occurrence)


def resolve_text_anchor(
    text: str, *, query: str, path: str, occurrence: int | None = None
) -> Span:
    """Resolve a literal string, optionally selecting the *n*-th occurrence.

    ``occurrence`` is 0-based. Leaving it unset while the text appears more than
    once is treated as a mistake rather than a guess: silently taking the first
    match would be the same failure mode as guessing line numbers.
    """
    index = LineIndex(text)
    spans: list[tuple[int, int]] = []
    cursor = 0
    while True:
        found = text.find(query, cursor)
        if found == -1:
            break
        spans.append((found, found + len(query)))
        cursor = found + 1  # overlapping occurrences are legitimate

    start, end, chosen = _locate(
        spans, index, text, path=path, kind="text", query=query, occurrence=occurrence
    )
    start_line, end_line = _span_for(index, start, end)
    return Span(
        start_line=start_line,
        end_line=end_line,
        match=text[start:end][:200],
        occurrence=chosen,
    )


def resolve_regex_anchor(
    text: str,
    *,
    pattern: str,
    path: str,
    occurrence: int | None = None,
    flags: int = re.MULTILINE,
) -> Span:
    """Resolve a regular expression.

    The first capture group is reported as the ``match`` when there is one, since
    that is normally the interesting part of such a pattern.
    """
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        raise EditMcpError(
            "INVALID_EDIT",
            f"Could not compile the anchor regex: {exc}.",
            hint="Check the pattern for unbalanced brackets or an invalid escape.",
            details={"pattern": pattern, "reason": str(exc)},
        ) from exc

    index = LineIndex(text)
    matches = list(compiled.finditer(text))
    spans = [m.span() for m in matches]

    start, end, chosen = _locate(
        spans, index, text, path=path, kind="regex", query=pattern, occurrence=occurrence
    )

    found = matches[chosen]
    group = (
        found.group(1)
        if found.lastindex and found.group(1) is not None
        else found.group(0)
    )
    start_line, end_line = _span_for(index, start, end)
    return Span(
        start_line=start_line,
        end_line=end_line,
        match=(group or "")[:200],
        occurrence=chosen,
    )
