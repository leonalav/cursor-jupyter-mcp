"""Batch semantics: addressing, ordering, overlap, and the expect guard."""

from __future__ import annotations

import random

import pytest

from cursor_edit_mcp.edit.batch import Edit, apply_batch, validate_batch
from cursor_edit_mcp.edit.document import TextDocument, detect_eol
from cursor_edit_mcp.errors import EditMcpError, ErrorCode

SAMPLE = TextDocument(text="l1\nl2\nl3\nl4\nl5\n")


def run(text, edits) -> str:
    """Apply edits the way read_document does, detecting the file's EOL style."""
    doc = TextDocument(text=text, eol=detect_eol(text))
    parsed = [Edit(**{**e, "index": i}) for i, e in enumerate(edits)]
    validate_batch(doc, parsed)
    return doc.render(apply_batch(doc, parsed)[0])


# --- addressing -----------------------------------------------------------


def test_replace_uses_inclusive_range():
    assert (
        run(
            "a\nb\nc\n",
            [{"op": "replace", "start_line": 2, "end_line": 3, "new_text": "X"}],
        )
        == "a\nX\n"
    )


def test_replace_single_line():
    assert (
        run(
            "a\nb\n", [{"op": "replace", "start_line": 1, "end_line": 1, "new_text": "Z"}]
        )
        == "Z\nb\n"
    )


def test_insert_after_line():
    assert (
        run("a\nc\n", [{"op": "insert", "after_line": 1, "new_text": "b"}]) == "a\nb\nc\n"
    )


def test_insert_at_top_of_file():
    assert run("b\n", [{"op": "insert", "after_line": 0, "new_text": "a"}]) == "a\nb\n"


def test_insert_at_end_of_file():
    assert run("a\n", [{"op": "insert", "after_line": 1, "new_text": "b"}]) == "a\nb\n"


def test_delete_range():
    assert run("a\nb\nc\n", [{"op": "delete", "start_line": 1, "end_line": 2}]) == "c\n"


def test_multi_line_inserted_text_splits_into_lines():
    assert (
        run("a\nd\n", [{"op": "insert", "after_line": 1, "new_text": "b\nc"}])
        == "a\nb\nc\nd\n"
    )


def test_inserted_text_with_trailing_newline_does_not_add_blank_line():
    assert run("b\n", [{"op": "insert", "after_line": 0, "new_text": "a\n"}]) == "a\nb\n"


# --- ordering -------------------------------------------------------------


def test_batch_applies_against_original_coordinates():
    result = run(
        "l1\nl2\nl3\nl4\nl5\n",
        [
            {"op": "replace", "start_line": 1, "end_line": 1, "new_text": "L1"},
            {"op": "replace", "start_line": 5, "end_line": 5, "new_text": "L5"},
        ],
    )
    assert result == "L1\nl2\nl3\nl4\nL5\n"


def test_insert_above_does_not_shift_lower_targets():
    result = run(
        "a\nb\nc\n",
        [
            {"op": "insert", "after_line": 0, "new_text": "top"},
            {"op": "replace", "start_line": 3, "end_line": 3, "new_text": "C"},
        ],
    )
    assert result == "top\na\nb\nC\n"


def test_inserts_at_same_seam_keep_listed_order():
    result = run(
        "x\n",
        [
            {"op": "insert", "after_line": 0, "new_text": "first"},
            {"op": "insert", "after_line": 0, "new_text": "second"},
        ],
    )
    assert result == "first\nsecond\nx\n"


# --- validation -----------------------------------------------------------


def test_overlapping_replacements_rejected():
    with pytest.raises(EditMcpError) as excinfo:
        run(
            "a\nb\nc\n",
            [
                {"op": "replace", "start_line": 1, "end_line": 2, "new_text": "X"},
                {"op": "replace", "start_line": 2, "end_line": 3, "new_text": "Y"},
            ],
        )
    assert excinfo.value.code == ErrorCode.OVERLAPPING_EDITS
    assert excinfo.value.details["conflicts"] == [{"edits": [0, 1]}]


def test_insert_conflicting_with_covered_line_rejected():
    with pytest.raises(EditMcpError) as excinfo:
        run(
            "a\nb\nc\n",
            [
                {"op": "replace", "start_line": 2, "end_line": 2, "new_text": "X"},
                {"op": "insert", "after_line": 2, "new_text": "Y"},
            ],
        )
    assert excinfo.value.code == ErrorCode.OVERLAPPING_EDITS


def test_insert_beside_a_range_edit_is_allowed():
    result = run(
        "a\nb\nc\nd\n",
        [
            {"op": "insert", "after_line": 1, "new_text": "NEW"},
            {"op": "replace", "start_line": 3, "end_line": 3, "new_text": "C"},
        ],
    )
    assert result == "a\nNEW\nb\nC\nd\n"


def test_range_beyond_file_rejected():
    with pytest.raises(EditMcpError) as excinfo:
        run("a\n", [{"op": "delete", "start_line": 1, "end_line": 5}])
    assert excinfo.value.code == ErrorCode.RANGE_OUT_OF_BOUNDS
    assert excinfo.value.details["line_count"] == 1


def test_line_zero_rejected():
    with pytest.raises(EditMcpError) as excinfo:
        run("a\n", [{"op": "replace", "start_line": 0, "end_line": 1, "new_text": "X"}])
    assert excinfo.value.code == ErrorCode.RANGE_OUT_OF_BOUNDS


def test_replace_without_range_rejected():
    with pytest.raises(EditMcpError) as excinfo:
        run("a\n", [{"op": "replace", "new_text": "X"}])
    assert excinfo.value.code == ErrorCode.INVALID_EDIT


def test_insert_without_text_rejected():
    with pytest.raises(EditMcpError) as excinfo:
        run("a\n", [{"op": "insert", "after_line": 0}])
    assert excinfo.value.code == ErrorCode.INVALID_EDIT


def test_delete_with_text_rejected():
    with pytest.raises(EditMcpError) as excinfo:
        run("a\nb\n", [{"op": "delete", "start_line": 1, "end_line": 1, "new_text": "x"}])
    assert excinfo.value.code == ErrorCode.INVALID_EDIT


# --- expect guard ---------------------------------------------------------


def test_expect_passes_when_text_present():
    result = run(
        "def fetch():\n",
        [
            {
                "op": "replace",
                "start_line": 1,
                "end_line": 1,
                "expect": "def fetch",
                "new_text": "def fetch_v2():",
            }
        ],
    )
    assert result == "def fetch_v2():\n"


def test_expect_mismatch_returns_current_text():
    with pytest.raises(EditMcpError) as excinfo:
        run(
            "def fetch_user(self):\n",
            [
                {
                    "op": "replace",
                    "start_line": 1,
                    "end_line": 1,
                    "expect": "def something_else",
                    "new_text": "X",
                }
            ],
        )
    error = excinfo.value
    assert error.code == ErrorCode.EXPECTED_TEXT_MISMATCH
    assert error.details["current_text"] == "def fetch_user(self):"


def test_expect_on_insert_rejected():
    with pytest.raises(EditMcpError) as excinfo:
        run("a\n", [{"op": "insert", "after_line": 0, "new_text": "x", "expect": "a"}])
    assert excinfo.value.code == ErrorCode.INVALID_EDIT


# --- line-ending preservation --------------------------------------------


def test_crlf_file_stays_crlf_after_edit():
    result = run(
        "a\r\nb\r\nc\r\n",
        [{"op": "replace", "start_line": 2, "end_line": 2, "new_text": "B"}],
    )
    assert result == "a\r\nB\r\nc\r\n"


def test_lf_file_stays_lf_after_edit():
    result = run("a\nb\nc\n", [{"op": "insert", "after_line": 1, "new_text": "x\r\ny"}])
    assert result == "a\nx\ny\nb\nc\n"


# --- equivalence properties ------------------------------------------------


def _disjoint_replacements(rng: random.Random, line_count: int, count: int) -> list[dict]:
    """Generate provably disjoint single-line replacements.

    Disjointness is a precondition of the property, not something to filter for
    afterwards: the engine rejects overlapping batches outright, so the generator
    has to produce valid batches by construction.
    """
    lines = list(range(1, line_count + 1))
    rng.shuffle(lines)
    return [
        {"op": "replace", "start_line": line, "end_line": line, "new_text": f"X{line}"}
        for line in sorted(lines[:count])
    ]


@pytest.mark.parametrize("seed", range(25))
def test_batch_equals_sequential_application_in_reverse(seed):
    """A batch must equal applying the same edits one at a time, bottom-up.

    This is what lets the agent trust that edit coordinates refer to the file it
    read, not to some intermediate state.
    """
    original = "a\nb\nc\nd\ne\n"
    rng = random.Random(seed)
    edits = _disjoint_replacements(rng, line_count=5, count=rng.randint(1, 5))

    batched = run(original, edits)

    current = original
    for edit in sorted(edits, key=lambda e: e["start_line"], reverse=True):
        current = run(current, [edit])

    assert batched == current


@pytest.mark.parametrize("seed", range(25))
def test_mixed_operation_batch_equals_sequential(seed):
    """A batch mixing inserts and replaces behaves like sequential application."""
    original = "a\nb\nc\nd\ne\n"
    rng = random.Random(seed)
    edits = _disjoint_replacements(rng, line_count=5, count=2)

    free_seam = next(
        line
        for line in range(6)
        if all(not (e["start_line"] <= line <= e["end_line"]) for e in edits)
    )
    edits.append({"op": "insert", "after_line": free_seam, "new_text": "NEW"})

    batched = run(original, edits)

    def anchor(edit: dict) -> int:
        return edit["after_line"] if edit["op"] == "insert" else edit["start_line"]

    current = original
    for edit in sorted(edits, key=anchor, reverse=True):
        current = run(current, [edit])

    assert batched == current


@pytest.mark.parametrize("seed", range(10))
def test_replace_then_inverse_restores_original_text(seed):
    """A replace followed by its inverse restores the file exactly.

    This is the round-trip guarantee behind clean diffs: an edit has to be
    expressible as a minimal change, not a rewrite of the whole file.
    """
    original = "alpha\nbeta\ngamma\ndelta\n"
    original_lines = original.split("\n")[:-1]

    rng = random.Random(seed)
    start = rng.randint(1, 3)
    end = rng.randint(start, 4)
    new_text = "NEW-1\nNEW-2"

    after = run(
        original,
        [{"op": "replace", "start_line": start, "end_line": end, "new_text": new_text}],
    )
    restored = run(
        after,
        [
            {
                "op": "replace",
                "start_line": start,
                "end_line": start + 1,
                "new_text": "\n".join(original_lines[start - 1 : end]),
            }
        ],
    )

    assert restored == original
