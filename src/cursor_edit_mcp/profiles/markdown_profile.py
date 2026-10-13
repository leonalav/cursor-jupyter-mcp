"""Markdown structural validation.

These checks are always on and need no configuration. They are deliberately
independent of markdownlint: markdownlint is a style checker with rules the
project can weaken, while these catch *broken* Markdown, which is what makes a
document unrenderable.

The PRD calls for ``markdown-it-py`` as the parser. It is used here for the
block map (where accurate line ranges come from its token stream), but the
checks below are direct scans, for a specific reason: CommonMark parses an
unclosed fence as an ordinary paragraph rather than an error, so the parser
cannot answer the questions we most need answered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..validate.diagnostics import Diagnostic
from .markdown_blocks import _match_fence, scan_blocks

#: A table delimiter row: pipes, dashes, and optional colons.
_DELIMITER_ROW = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")

#: A list bullet with its indent and marker.
_BULLET = re.compile(r"^(?P<indent> *)(?P<marker>[-*+]|\d{1,9}[.)])(?=\s)")

#: A line that looks like the start of a table body row.
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")

_FENCE_OPEN = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})")


@dataclass(frozen=True)
class StructuralIssue:
    """A broken structure, with enough context to fix it.

    ``is_syntax`` marks issues that stop the document rendering at all. Those
    are what ``strict`` mode blocks on; a style-level inconsistency is reported
    but does not stop a write.
    """

    code: str
    message: str
    line: int
    severity: str = "error"
    is_syntax: bool = False


def check(text: str) -> list[StructuralIssue]:
    """Run every structural check over the whole document."""
    lines = text.split("\n")
    issues: list[StructuralIssue] = []
    issues += _check_fences(lines)
    issues += _check_tables(lines)
    issues += _check_front_matter(lines)
    issues += _check_list_indentation(lines)
    return issues


def check_as_diagnostics(text: str) -> tuple[Diagnostic, ...]:
    """Structural issues as diagnostics, so they flow through the same
    baseline-diffing path as lint results."""
    return tuple(
        Diagnostic(
            tool="markdown",
            code=issue.code,
            message=issue.message,
            severity=issue.severity,  # type: ignore[arg-type]
            line=issue.line,
            column=1,
            source_line="",
            is_syntax=issue.is_syntax,
        )
        for issue in check(text)
    )


# --- fences ---------------------------------------------------------------


def _check_fences(lines: list[str]) -> list[StructuralIssue]:
    """Every fenced block must close with a matching fence.

    An unclosed fence swallows the rest of the document: everything after it
    renders as code.
    """
    issues: list[StructuralIssue] = []
    open_fence: str | None = None
    open_line = 0
    open_char = ""
    open_len = 0

    for number, line in enumerate(lines, start=1):
        match = _FENCE_OPEN.match(line)
        if open_fence is None:
            if not match:
                continue
            fence = match.group("fence")
            info = line[match.end("fence") :]
            # A backtick fence's info string may not contain a backtick.
            if fence[0] == "`" and "`" in info:
                continue
            open_fence = fence
            open_char = fence[0]
            open_len = len(fence)
            open_line = number
            continue

        if not match:
            continue
        fence = match.group("fence")
        info = line[match.end("fence") :].strip()
        if fence[0] == open_char and len(fence) >= open_len and not info:
            open_fence = None

    if open_fence is not None:
        issues.append(
            StructuralIssue(
                "MD-UNCLOSED-FENCE",
                f"Unclosed code fence opened here with {open_len} "
                f"'{open_char}' character(s). Everything after it renders as code.",
                open_line,
                is_syntax=True,
            )
        )
    return issues


# --- tables ---------------------------------------------------------------


def _check_tables(lines: list[str]) -> list[StructuralIssue]:
    """A table's delimiter row must be valid and its rows must agree on columns.

    A mismatched column count makes the table render as plain text, which is
    the usual way a Markdown table breaks.
    """
    issues: list[StructuralIssue] = []
    index = 0
    total = len(lines)

    while index < total:
        line = lines[index]
        # A header row followed by a delimiter row is what starts a table.
        if (
            _TABLE_ROW.match(line)
            and index + 1 < total
            and _DELIMITER_ROW.match(lines[index + 1])
            and "|" in lines[index + 1]
        ):
            header_line = index + 1
            columns = _column_count(line)

            # Two separate ways a delimiter row fails: it is not shaped like a
            # delimiter row at all, or it is one, but declares a different
            # number of columns than the header above it.
            if not _valid_delimiter_row(lines[index + 1]):
                issues.append(
                    StructuralIssue(
                        "MD-BAD-TABLE-DELIMITER",
                        "The row under the table header is not a valid delimiter "
                        "row (needs pipes and dashes).",
                        header_line + 1,
                        is_syntax=True,
                    )
                )
            elif _column_count(lines[index + 1]) != columns:
                issues.append(
                    StructuralIssue(
                        "MD-BAD-TABLE-DELIMITER",
                        f"The delimiter row declares "
                        f"{_column_count(lines[index + 1])} column(s) but the "
                        f"header has {columns}. The table will not render.",
                        header_line + 1,
                        is_syntax=True,
                    )
                )

            cursor = index + 2
            while cursor < total and _TABLE_ROW.match(lines[cursor]):
                row_columns = _column_count(lines[cursor])
                if row_columns != columns:
                    issues.append(
                        StructuralIssue(
                            "MD-TABLE-COLUMN-MISMATCH",
                            f"Table row has {row_columns} column(s) but the "
                            f"header has {columns}.",
                            cursor + 1,
                            is_syntax=True,
                        )
                    )
                cursor += 1
            index = cursor
            continue
        index += 1

    return issues


def _column_count(line: str) -> int:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and len(stripped) > 1:
        stripped = stripped[:-1]
    return len(stripped.split("|"))


def _valid_delimiter_row(line: str) -> bool:
    if "-" not in line:
        return False
    cells = [cell for cell in line.strip().strip("|").split("|")]
    return all(re.fullmatch(r"\s*:?-{1,}:?\s*", cell or "x") for cell in cells)


# --- front matter ---------------------------------------------------------


def _check_front_matter(lines: list[str]) -> list[StructuralIssue]:
    """Front matter must open and close on the first line.

    An unterminated block makes the YAML render as a horizontal rule followed
    by body text, which is nearly impossible to notice by eye.
    """
    if not lines or lines[0].strip() != "---":
        return []

    for number in range(1, min(len(lines), 200)):
        if lines[number].strip() == "---":
            return []
        if lines[number].strip() == "...":
            return []

    return [
        StructuralIssue(
            "MD-UNTERMINATED-FRONT-MATTER",
            "Front matter opens with '---' on line 1 but is never closed.",
            1,
            is_syntax=True,
        )
    ]


# --- lists ----------------------------------------------------------------


def _check_list_indentation(lines: list[str]) -> list[StructuralIssue]:
    """List items in one list must use a consistent indent step.

    A single mis-indented bullet silently breaks a list into two, or nests a
    sibling under its predecessor.
    """
    issues: list[StructuralIssue] = []
    previous_width: int | None = None
    fence_char = ""
    fence_len = 0

    for number, line in enumerate(lines, start=1):
        fence = _match_fence(line)
        if fence:
            char, length, info = fence
            if fence_char:
                # Inside a fence: only a matching, bare closer ends it.
                if char == fence_char and length >= fence_len and not info.strip():
                    fence_char = ""
            else:
                fence_char, fence_len = char, length
            previous_width = None
            continue

        if fence_char or not line.strip():
            previous_width = None
            continue

        bullet = _BULLET.match(line)
        if not bullet:
            previous_width = None
            continue

        width = len(bullet.group("indent"))
        if previous_width is not None and width != previous_width:
            issues.append(
                StructuralIssue(
                    "MD-LIST-INDENT-INCONSISTENT",
                    f"List item is indented {width} space(s) but the previous "
                    f"item in this list used {previous_width}. Inconsistent "
                    "indentation splits one list into two.",
                    number,
                    severity="warning",
                )
            )
        previous_width = width

    return issues


def block_map(text: str) -> list[dict[str, object]]:
    """Headings, fences, and front matter with their line ranges."""
    lines = text.split("\n")
    headings, blocks = scan_blocks(lines)
    out: list[dict[str, object]] = [h.as_dict() for h in headings]
    out.extend(
        {"kind": b.kind, "start_line": b.start_line, "end_line": b.end_line}
        for b in blocks
    )
    out.sort(key=lambda item: int(item["start_line"]))  # type: ignore[arg-type]
    return out
