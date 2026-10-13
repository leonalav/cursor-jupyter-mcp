"""Relative indentation.

The agent writes ``new_text`` in whatever indentation it finds natural, usually
flush left, because it is thinking about the code rather than about the file's
left margin. This module removes the common margin from the incoming text and
re-applies the indentation of the surrounding block, so a method body sent
flush-left lands correctly inside its class.

``indent_mode: "absolute"`` turns this off for files where it does not apply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

INDENT_MODE_RELATIVE = "relative"
INDENT_MODE_ABSOLUTE = "absolute"
INDENT_MODES = (INDENT_MODE_RELATIVE, INDENT_MODE_ABSOLUTE)

#: A line that carries no indentation, used to infer the block's own margin.
_LEADING = re.compile(r"[ \t]*")

#: A one-unit step for the target indentation. Python infers this from the file
#: rather than assuming 4 spaces.
DEFAULT_INDENT_UNIT = "    "


@dataclass(frozen=True)
class IndentStyle:
    unit: str = DEFAULT_INDENT_UNIT

    @property
    def width(self) -> int:
        return len(self.unit)


def detect_style(lines: list[str], *, path: str = "") -> IndentStyle:
    """Infer the file's indentation unit from the most common step.

    Every non-blank line contributes a baseline, including flush-left ones:
    otherwise a file whose first indented line is the second line of the file
    would report no step at all and fall back to 4.
    """
    widths: list[int] = []
    previous: int | None = None
    for line in lines:
        if not line.strip():
            continue
        width = len(_LEADING.match(line).group(0))  # type: ignore[union-attr]
        if previous is not None and width > previous:
            widths.append(width - previous)
        previous = width

    if not widths:
        return IndentStyle()
    step = min(widths)
    return IndentStyle(" " * step if step else DEFAULT_INDENT_UNIT)


def _indent_of(line: str) -> str:
    return _LEADING.match(line).group(0)  # type: ignore[union-attr]


def _expand(indent: str) -> int:
    """Visual column width of an indent, treating a tab as one step.

    Enough to compare indentation levels; the file's own tabs and spaces are
    preserved verbatim in the output.
    """
    return indent.replace("\t", " " * 8).count(" ")


def common_margin(text: str) -> str:
    """The longest leading whitespace shared by every non-blank line."""
    margins = [_indent_of(line) for line in text.split("\n") if line.strip()]
    if not margins:
        return ""

    result = margins[0]
    for margin in margins[1:]:
        limit = min(len(result), len(margin))
        index = 0
        while index < limit and result[index] == margin[index]:
            index += 1
        result = result[:index]
        if not result:
            break
    return result


def strip_margin(text: str) -> str:
    """Remove the common margin, keeping relative indentation intact."""
    margin = common_margin(text)
    if not margin:
        return text
    out = []
    for line in text.split("\n"):
        out.append(line[len(margin) :] if line.startswith(margin) else line.lstrip())
    return "\n".join(out)


def reindent(
    new_text: str,
    lines: list[str],
    *,
    target_line: int,
    lines_count: int,
    style: IndentStyle | None = None,
) -> str:
    """Re-apply the target indentation to ``new_text``.

    The target margin is taken from ``target_line``, or from the line after the
    insertion point when inserting at the end of the file. Only the first line's
    existing margin is reused; relative steps inside the block are preserved.
    """
    style = style or detect_style(lines)
    target_margin = _margin_for(lines, target_line, lines_count)

    body = strip_margin(new_text)
    if not body.strip():
        return body

    return "\n".join(
        (target_margin + line) if line.strip() else "" for line in body.split("\n")
    )


def _margin_for(lines: list[str], target_line: int, lines_count: int) -> str:
    """Choose the margin to apply.

    Prefers an existing line inside the file so the result matches the file's own
    style, and falls back to one indent unit when inserting past the end.
    """
    if 1 <= target_line <= lines_count:
        return margin_of(lines[target_line - 1])

    if lines_count and lines[0].strip():
        # Appending after the last line: match that line's margin.
        return margin_of(lines[lines_count - 1])

    return ""


def margin_of(line: str) -> str:
    """The leading whitespace of a line."""
    return _LEADING.match(line).group(0)  # type: ignore[union-attr]


def reindent_to(
    text: str,
    *,
    margin: str,
    style: IndentStyle | None = None,
    path_is_tabbed: bool = False,
) -> str:
    """Re-apply ``margin`` to ``text`` after removing its own common margin.

    Tab-indented targets keep their tabs, so a tab-indented file does not end up
    with mixed indentation as a side effect of an edit.
    """
    if not text.strip():
        return text

    style = style or IndentStyle()
    body = strip_margin(text)

    applied = _as_margin(margin, style, path_is_tabbed)
    return "\n".join(
        (applied + line) if line.strip() else "" for line in body.split("\n")
    )


def _as_margin(margin: str, style: IndentStyle, tabbed: bool) -> str:
    """Express a margin in the target file's indentation character."""
    if not margin:
        return ""
    if not tabbed:
        return margin.replace("\t", " " * style.width)

    # Tab-indented file: round the visual width down to whole tabs.
    width = _expand(margin)
    return "\t" * (width // max(style.width, 1))


def detects_mixed_indentation(text: str) -> bool:
    """True when the text mixes tabs and spaces for indentation.

    The PRD requires rejecting this on a strict edit: it is almost always an
    accident, and Python refuses such a file outright.
    """
    tabbed = False
    spaced = False
    for line in text.split("\n"):
        if not line.strip():
            continue
        margin = _indent_of(line)
        if not margin:
            continue
        if "\t" in margin and " " in margin:
            return True
        tabbed |= "\t" in margin
        spaced |= " " in margin
    return tabbed and spaced


def uses_tabs(lines: list[str]) -> bool:
    """Whether the file indents with tabs."""
    return any(_indent_of(line).startswith("\t") for line in lines if line.strip())


def strip_for_output(text: str, style: IndentStyle) -> str:
    """Normalize an incoming block to spaces before re-indenting.

    Only applied when the target file uses spaces; a tab-indented file keeps its
    tabs so the edit does not create a mixed-indent file.
    """
    if "\t" not in text:
        return text
    return "\n".join(
        line.replace("\t", " " * style.width) if line.startswith("\t") else line
        for line in text.split("\n")
    )


def relative_depth(text: str) -> int:
    """Visual indent of the block's deepest line, relative to its margin."""
    depths = [
        _expand(_indent_of(line)) - _expand(common_margin(text))
        for line in text.split("\n")
        if line.strip()
    ]
    return max(depths) if depths else 0
