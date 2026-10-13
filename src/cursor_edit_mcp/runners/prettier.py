"""Prettier adapter.

Prettier is used only when the project configures it. It has no range mode, so
the pipeline keeps just the hunks overlapping the edited lines.

Prettier reads stdin, and ``--stdin-filepath`` makes it resolve config exactly
as it would for a real file, so no temporary file is needed.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

from .base import CallSpec, Resolver, RunOutcome, ToolStatus, run

PRETTIER = "prettier"


@dataclass(frozen=True)
class FormatResult:
    text: str
    changed: bool
    outcome: RunOutcome


def status(workspace_root: Path) -> ToolStatus:
    return Resolver(workspace_root).status(PRETTIER, ["--version"])


def format_markdown(
    text: str,
    *,
    path: Path,
    resolver: Resolver,
    timeout: float = 15.0,
) -> FormatResult:
    """Format Markdown with Prettier, whole file."""
    tool = resolver.status(PRETTIER, ["--version"])
    if not tool.available:
        return FormatResult(text, False, _unavailable(tool.reason))

    outcome = run(
        CallSpec(
            argv=[
                str(tool.executable),
                "--parser",
                "markdown",
                "--stdin-filepath",
                str(path),
            ],
            cwd=path.parent,
            stdin=text,
            timeout=timeout,
        )
    )
    if not outcome.usable:
        return FormatResult(text, False, outcome)

    formatted = outcome.stdout or text
    return FormatResult(formatted, formatted != text, outcome)


def hunk_filter(
    before: str, after: str, spans: list[tuple[int, int]], margin: int = 2
) -> str:
    """Keep only the parts of ``after`` that fall inside the edited ranges.

    ``spans`` are 1-based inclusive line ranges in the post-edit document.
    Prettier cannot be asked to format one region, so we format everything and
    then reassemble: hunks outside ``spans`` (plus a small margin) revert to the
    edited text. This is what makes "format the edit, not the file" true for
    tools without a range mode.
    """
    before_lines = before.split("\n")
    after_lines = after.split("\n")

    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    out: list[str] = []

    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.extend(after_lines[j1:j2])
            continue
        # 1-based inclusive range this hunk occupies in the *new* document.
        start = j1 + 1
        end = j2 if j2 else start
        if _overlaps(start, end, spans, margin):
            out.extend(after_lines[j1:j2])
        else:
            # Outside the edit: keep what the edit produced, unformatted.
            out.extend(before_lines[_i1:_i2])

    return "\n".join(out)


def _overlaps(start: int, end: int, spans: list[tuple[int, int]], margin: int) -> bool:
    for span_start, span_end in spans:
        if start <= span_end + margin and end >= span_start - margin:
            return True
    return False


def _unavailable(reason: str | None) -> RunOutcome:
    return RunOutcome(ran=False, reason=reason or "prettier is unavailable")
