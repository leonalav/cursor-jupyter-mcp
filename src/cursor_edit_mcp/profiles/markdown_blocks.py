"""Markdown block map and section addressing.

Heading sections are resolved from a scanned block map rather than by matching
`^#` against every line, because a line starting with `#` inside a fenced code
block is code, not a heading. Getting this wrong means `edit_locate` returns a
range that deletes someone's example.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from ..edit.anchors import Span
from ..errors import EditMcpError, anchor_not_found

#: ATX heading: up to three leading spaces, 1-6 `#`, then a space or end.
_ATX = re.compile(r"^(?P<indent> {0,3})(?P<hashes>#{1,6})(?:\s+(?P<title>.*?))?\s*$")

#: Setext underline, which promotes the preceding line to a heading.
_SETEXT = re.compile(r"^(?P<indent> {0,3})(?P<underline>=+|-+)\s*$")

#: Fence opener. The backtick form may not contain a backtick in its info string.
_FENCE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>.*)$")

#: YAML front matter delimiter.
_FRONT_MATTER = "---"


@dataclass(frozen=True)
class Heading:
    """One heading and the section it owns."""

    level: int
    title: str
    start_line: int
    #: Section end when subsections are included (next heading of same/higher level).
    section_end: int
    #: Section end when subsections are excluded (next heading of any level).
    own_end: int
    in_front_matter: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "level": self.level,
            "title": self.title,
            "start_line": self.start_line,
            "end_line": self.section_end,
        }


@dataclass(frozen=True)
class Block:
    kind: str
    start_line: int
    end_line: int
    info: str = ""


def _match_fence(line: str) -> tuple[str, int, str] | None:
    """Return (character, length, info) if the line opens or closes a fence."""
    match = _FENCE.match(line)
    if not match:
        return None
    char = match.group("fence")[0]
    length = len(match.group("fence"))
    info = match.group("info")
    # An opening backtick fence may not have a backtick in its info string.
    if char == "`" and "`" in info:
        return None
    return char, length, info


def scan_blocks(lines: list[str]) -> tuple[list[Heading], list[Block]]:
    """Walk the file once, collecting headings and fenced blocks.

    Fences are tracked with a matching character and length, so a ``~~~``
    block can contain a ````` `` ``` `` line without ending early.
    """
    headings: list[Heading] = []
    blocks: list[Block] = []

    in_front_matter = False
    fence_char = ""
    fence_len = 0
    fence_start = 0
    front_matter_end = 0
    index = 0
    total = len(lines)

    if total and lines[0].strip() == _FRONT_MATTER:
        in_front_matter = True
        index = 1
        while index < total and lines[index].strip() != _FRONT_MATTER:
            index += 1
        if index < total:
            front_matter_end = index + 1
            blocks.append(Block("front_matter", 1, front_matter_end))
            in_front_matter = False
            index += 1

    while index < total:
        line_no = index + 1
        line = lines[index]

        if fence_char:
            closing = _match_fence(line)
            if (
                closing
                and closing[0] == fence_char
                and closing[1] >= fence_len
                and not closing[2].strip()
            ):
                blocks.append(Block("code", fence_start, line_no))
                fence_char = ""
            index += 1
            continue

        opening = _match_fence(line)
        if opening:
            fence_char, fence_len, info = opening
            fence_start = line_no
            index += 1
            continue

        heading = _heading_at(lines, index, in_front_matter)
        if heading:
            headings.append(heading)
            index += 1
            continue

        index += 1

    if fence_char:
        blocks.append(Block("code", fence_start, total))

    _close_sections(headings, total)
    return headings, blocks


def _heading_at(lines: list[str], index: int, in_front_matter: bool) -> Heading | None:
    """Detect an ATX or setext heading starting at this line."""
    line = lines[index]
    atx = _ATX.match(line)
    if atx:
        title = (atx.group("title") or "").strip()
        return Heading(
            level=len(atx.group("hashes")),
            title=title,
            start_line=index + 1,
            section_end=index + 1,
            own_end=index + 1,
            in_front_matter=in_front_matter,
        )

    # Setext: the underline turns the *previous* line into the heading.
    if index > 0 and not in_front_matter:
        underline = _SETEXT.match(line)
        if underline:
            previous = lines[index - 1].strip()
            if previous and not _ATX.match(lines[index - 1]):
                return Heading(
                    level=1 if underline.group("underline")[0] == "=" else 2,
                    title=previous,
                    start_line=index,
                    section_end=index + 1,
                    own_end=index + 1,
                    in_front_matter=False,
                )
    return None


def _close_sections(headings: list[Heading], total_lines: int) -> None:
    """Fill in section ends now that every heading is known.

    Headings are stored frozen, so rebuild the list with resolved ends.
    """
    resolved: list[Heading] = []
    for position, heading in enumerate(headings):
        section_end = total_lines
        own_end = total_lines

        for later in headings[position + 1 :]:
            # Excluding subsections: stop at the first heading of any level,
            # since the next one is either a subsection or a following sibling.
            if later.start_line - 1 < own_end:
                own_end = later.start_line - 1
            if later.level <= heading.level and later.start_line - 1 < section_end:
                section_end = later.start_line - 1
            if later.start_line - 1 >= section_end:
                break

        resolved.append(
            Heading(
                level=heading.level,
                title=heading.title,
                start_line=heading.start_line,
                section_end=max(section_end, heading.start_line),
                own_end=max(own_end, heading.start_line),
                in_front_matter=heading.in_front_matter,
            )
        )

    headings[:] = resolved


def _trim_trailing_blank(lines: list[str], start: int, end: int) -> int:
    """Pull the end back over blank lines.

    A section that a human means by "the Installation part" ends at the last
    content line, not at the blank lines separating it from the next heading.
    """
    while end > start and not lines[end - 1].strip():
        end -= 1
    return end


def resolve_heading(
    lines: list[str],
    *,
    text: str,
    path: str,
    include_subsections: bool = True,
) -> Span:
    """Resolve a heading to the section it owns.

    ``text`` may be the heading title alone or the full line including its
    hashes, since agents tend to have the latter copied from the file.
    """
    headings, _ = scan_blocks(lines)
    query = text.strip()
    # The PRD's own example passes "## Installation" with its hashes, while an
    # agent that copied a bare title passes "Installation". Both must work.
    normalized = _ATX.match(query)
    bare = (
        normalized.group("title").strip()
        if normalized and normalized.group("title")
        else query.lstrip("#").strip()
    )

    by_title = [h for h in headings if h.title.lower() == bare.lower()]
    if not by_title:
        by_title = [h for h in headings if bare and bare in h.title]

    if not by_title:
        raise _no_heading(path, query, headings)

    if len(by_title) > 1:
        exact = [h for h in by_title if h.title.lower() == bare.lower()]
        chosen_pool = exact or by_title
        if len(chosen_pool) > 1:
            levels = ", ".join(sorted({f"#{h.level} {h.title}" for h in chosen_pool}))
            raise anchor_not_found(
                path,
                f"heading {query!r} ({len(chosen_pool)} matches)",
                hint=(
                    f"Several headings match. Disambiguate with the full title: {levels}."
                ),
                candidates=[h.as_dict() for h in chosen_pool[:10]],
            )
        chosen = chosen_pool[0]
    else:
        chosen = by_title[0]

    end = chosen.section_end if include_subsections else chosen.own_end
    end = _trim_trailing_blank(lines, chosen.start_line, end)
    return Span(
        start_line=chosen.start_line,
        end_line=max(end, chosen.start_line),
        match=f"{'#' * chosen.level} {chosen.title}",
        occurrence=0,
    )


def _no_heading(path: str, query: str, headings: list[Heading]) -> EditMcpError:
    lowered = query.lower().lstrip("#").strip()
    close = [
        h
        for h in headings
        if lowered and (lowered in h.title.lower() or h.title.lower() in lowered)
    ]
    if not close and lowered:
        # Containment misses a typo like "Installaton", so fall back to edit
        # distance before giving up on suggestions.
        close = [
            h
            for h in headings
            if difflib.get_close_matches(lowered, [h.title.lower()], n=1, cutoff=0.7)
        ]
    if close:
        suggestions = ", ".join(sorted({f"#{h.level} {h.title}" for h in close})[:5])
        return anchor_not_found(
            path,
            f"heading {query!r}",
            hint=f"Did you mean: {suggestions}?",
            candidates=[h.as_dict() for h in close[:10]],
        )

    if headings:
        outline = ", ".join(f"#{h.level} {h.title}" for h in headings[:10])
        hint = f"This document has headings: {outline}."
    else:
        hint = "This document has no ATX or setext headings."
    return anchor_not_found(path, f"heading {query!r}", hint=hint)


def outline(lines: list[str]) -> list[dict[str, object]]:
    """All headings, for diagnostics and hints."""
    headings, _ = scan_blocks(lines)
    return [h.as_dict() for h in headings]


def unclosed_fences(lines: list[str]) -> list[Block]:
    """Fenced blocks that never close, a structural error worth reporting."""
    _, blocks = scan_blocks(lines)
    total = len(lines)
    return [b for b in blocks if b.kind == "code" and b.end_line == total]
