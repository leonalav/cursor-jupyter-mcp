"""Batch edit semantics.

Every edit in a batch is addressed against the snapshot the agent read, not
against a moving target. That is enforced two ways:

* all edits are validated against the original text before any is applied, so
  an out-of-range or overlapping batch changes nothing; and
* the batch is applied bottom-up, so an edit near the top never shifts the
  line numbers of an edit below it.

Applying bottom-up is what makes the property test hold: a batch produces the
same file as applying the same edits one at a time in descending line order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..errors import EditMcpError, ErrorCode
from .document import TextDocument

Op = Literal["replace", "insert", "delete"]


@dataclass(frozen=True)
class Edit:
    """One edit, addressed in 1-based inclusive coordinates."""

    op: Op
    start_line: int | None = None
    end_line: int | None = None
    after_line: int | None = None
    new_text: str | None = None
    expect: str | None = None
    index: int = 0

    @property
    def is_insert(self) -> bool:
        return self.op == "insert"

    def anchor(self) -> int:
        """The line this edit is keyed on, used for ordering and conflict checks."""
        return self.after_line if self.is_insert else int(self.start_line or 0)

    def target_text(self, document: TextDocument) -> str:
        """The current text this edit will replace or verify."""
        if self.is_insert:
            return document.line(self.after_line) if self.after_line else ""
        return document.slice_text(int(self.start_line or 0), self.end_line)


def _validate_range(edit: Edit, document: TextDocument) -> None:
    count = document.line_count
    start, end = edit.start_line, edit.end_line
    if start is None or end is None:
        raise EditMcpError(
            ErrorCode.INVALID_EDIT,
            f"Edit {edit.index} ('{edit.op}') needs start_line and end_line.",
            hint="Give a 1-based inclusive range, e.g. start_line 10, end_line 12.",
            details={"edit_index": edit.index, "op": edit.op},
        )
    if start < 1 or end > count or start > end:
        raise EditMcpError(
            ErrorCode.RANGE_OUT_OF_BOUNDS,
            f"Edit {edit.index} targets lines {start}-{end}, outside the file.",
            hint=f"The file has {count} line(s); use 1-based inclusive numbering.",
            details={
                "edit_index": edit.index,
                "start_line": start,
                "end_line": end,
                "line_count": count,
            },
        )


def validate_batch(document: TextDocument, edits: list[Edit]) -> None:
    """Check every edit against the original snapshot before applying any."""
    count = document.line_count

    for edit in edits:
        if edit.is_insert:
            after = edit.after_line
            if after is None or after < 0 or after > count:
                raise EditMcpError(
                    ErrorCode.RANGE_OUT_OF_BOUNDS,
                    f"Edit {edit.index} inserts after line {after}, outside the file.",
                    hint=(
                        f"after_line must be 0 (start of file) to {count} (end of file)."
                    ),
                    details={
                        "edit_index": edit.index,
                        "after_line": after,
                        "line_count": count,
                    },
                )
            if edit.new_text is None or edit.new_text == "":
                raise EditMcpError(
                    ErrorCode.INVALID_EDIT,
                    f"Edit {edit.index} ('insert') has no new_text.",
                    hint="Provide the text to insert.",
                    details={"edit_index": edit.index},
                )
        else:
            _validate_range(edit, document)
            if edit.op == "delete" and edit.new_text:
                raise EditMcpError(
                    ErrorCode.INVALID_EDIT,
                    f"Edit {edit.index} ('delete') must not carry new_text.",
                    hint="A delete removes the range; drop new_text or use op 'replace'.",
                    details={"edit_index": edit.index},
                )

        if edit.expect is not None:
            if edit.is_insert:
                # An insert has no target range, so the guard is meaningless.
                raise EditMcpError(
                    ErrorCode.INVALID_EDIT,
                    f"Edit {edit.index} ('insert') cannot use expect.",
                    hint="expect guards a replace or delete range; omit it on inserts.",
                    details={"edit_index": edit.index},
                )
            current = edit.target_text(document)
            if edit.expect not in current:
                raise EditMcpError(
                    ErrorCode.EXPECTED_TEXT_MISMATCH,
                    (
                        f"Edit {edit.index} expected text not found in lines "
                        f"{edit.start_line}-{edit.end_line}."
                    ),
                    hint=(
                        "The range changed since it was read. "
                        "Use the current_text below, or re-read the file."
                    ),
                    details={
                        "edit_index": edit.index,
                        "expected": edit.expect,
                        "current_text": current,
                    },
                )

    _check_overlaps(edits)


def _check_overlaps(edits: list[Edit]) -> None:
    """Reject edits whose effects are order-dependent.

    Two ranges conflict when they share a line. An insert conflicts with a range
    that covers either of the lines it sits between, because the result would
    depend on which ran first. Inserts at the same seam are kept in the order
    the agent listed them.
    """
    ranges: list[tuple[int, int, int]] = []
    seams: dict[int, int] = {}

    for edit in edits:
        if edit.is_insert:
            seam = int(edit.after_line or 0)
            seams.setdefault(seam, edit.index)
            continue
        ranges.append((int(edit.start_line or 0), int(edit.end_line or 0), edit.index))

    conflicts: list[dict[str, object]] = []
    for i, (start_a, end_a, index_a) in enumerate(ranges):
        for start_b, end_b, index_b in ranges[i + 1 :]:
            if start_a <= end_b and start_b <= end_a:
                conflicts.append({"edits": [index_a, index_b]})
        for seam, insert_index in seams.items():
            if start_a <= seam <= end_a:
                conflicts.append({"edits": [index_a, insert_index]})

    if conflicts:
        pairs = ", ".join(str(c["edits"]) for c in conflicts)
        raise EditMcpError(
            ErrorCode.OVERLAPPING_EDITS,
            f"Edits {pairs} touch the same lines.",
            hint=(
                "Combine them into a single edit, or target disjoint ranges. "
                "All edits must be disjoint because they apply to one snapshot."
            ),
            details={"conflicts": conflicts},
        )


def apply_batch(
    document: TextDocument, edits: list[Edit]
) -> tuple[list[str], list[tuple[int, int]]]:
    """Apply a validated batch bottom-up.

    Returns the resulting lines **and** the ranges the edits occupy in those
    new lines. Validation needs the latter: range-contained formatting and safe
    autofix must know which lines the edit actually produced, in the
    coordinates of the text they will be validated against.
    """
    lines = document.lines

    # Bottom-up by anchor line. Descending index breaks ties so that inserts at
    # the same seam reappear in the order the agent wrote them.
    ordered = sorted(edits, key=lambda e: (e.anchor(), e.index), reverse=True)

    for edit in ordered:
        if edit.is_insert:
            block = _split_text(edit.new_text or "")
            after = int(edit.after_line or 0)
            lines[after:after] = block
            continue

        start, end = int(edit.start_line or 0), int(edit.end_line or 0)
        replacement = [] if edit.op == "delete" else _split_text(edit.new_text or "")
        lines[start - 1 : end] = replacement

    touched = _touched_ranges(edits, lines)
    return lines, touched


def _touched_ranges(edits: list[Edit], new_lines: list[str]) -> list[tuple[int, int]]:
    """Line ranges each edit occupies in the post-edit document.

    An edit's address in the original document is not its address in the result.
    Two things shift it:

    - an edit *above* it that changed the line count moves it down or up;
    - for an ``insert`` after line *n*, the new text starts at *n+1*, whereas
      a ``replace`` of *n..m* starts back at *n*.

    Below an edit, only edits strictly above can have moved it, since
    overlapping edits are rejected before this runs.

    Returns ranges sorted and merged, in 1-based inclusive coordinates.
    """
    # (anchor, lines_added_relative_to_original, lines_the_edit_produced)
    geometry: list[tuple[int, int, int]] = []
    for edit in edits:
        if edit.is_insert:
            produced = len(_split_text(edit.new_text or ""))
            geometry.append((int(edit.after_line or 0), produced, produced))
        else:
            start = int(edit.start_line or 0)
            end = int(edit.end_line or 0)
            original = end - start + 1
            produced = 0 if edit.op == "delete" else len(_split_text(edit.new_text or ""))
            geometry.append((start, produced - original, produced))

    spans: list[tuple[int, int]] = []
    for edit, (anchor, _shift, produced) in zip(edits, geometry, strict=True):
        # An insert lands after its anchor; a range edit starts on it.
        base = anchor + 1 if edit.is_insert else anchor
        delta = sum(shift for other, shift, _ in geometry if other < anchor)
        start = base + delta

        if edit.op == "delete":
            # A zero-width seam at the point the content was removed, so a
            # formatter still treats that position as touched.
            spans.append((start, start))
        elif produced:
            spans.append((start, start + produced - 1))

    return _merge(spans, len(new_lines))


def _merge(spans: list[tuple[int, int]], total: int) -> list[tuple[int, int]]:
    """Sort, clip to the document, and merge overlapping or adjacent ranges."""
    cleaned = [(max(1, start), min(total, end)) for start, end in spans if start <= total]
    if not cleaned:
        return []
    cleaned.sort()

    merged = [cleaned[0]]
    for start, end in cleaned[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + 1:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _split_text(text: str) -> list[str]:
    """Split inserted text into lines using the target file's ending style.

    A single trailing newline is treated as line termination rather than as an
    extra blank line. Agents naturally end ``new_text`` with ``\\n`` because that
    is how the line reads in the file they copied it from; without this, every
    such insert would add a stray blank line and shift every line number after
    it. To insert a genuinely blank line, pass two trailing newlines.
    """
    if text == "":
        return []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.removesuffix("\n")
    return normalized.split("\n")
