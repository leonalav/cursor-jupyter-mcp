"""Tool-level behaviour against a real workspace on disk."""

from __future__ import annotations

import pytest

from cursor_edit_mcp.errors import EditMcpError, ErrorCode
from cursor_edit_mcp.tools.edit_apply import EditSpec, edit_apply
from cursor_edit_mcp.tools.file_read import file_read


def specs(*items) -> list[EditSpec]:
    return [EditSpec.model_validate(item) for item in items]


def replace(start, end, text, **kw):
    return {
        "op": "replace",
        "range": {"start_line": start, "end_line": end},
        "new_text": text,
        **kw,
    }


# --- file_read ------------------------------------------------------------


def test_read_whole_file(settings, make_file):
    make_file("a.py", "one\ntwo\nthree\n")
    result = file_read("a.py", settings=settings)

    assert result["ok"] is True
    assert result["content"] == "one\ntwo\nthree\n"
    assert result["line_count"] == 3
    assert result["partial"] is False
    assert result["hash"].startswith("sha256:")


def test_read_line_range(settings, make_file):
    make_file("a.py", "one\ntwo\nthree\nfour\n")
    result = file_read("a.py", start_line=2, end_line=3, settings=settings)

    assert result["content"] == "two\nthree"
    assert result["line_count"] == 4
    assert result["partial"] is True


def test_read_range_hash_is_whole_file_hash(settings, make_file):
    make_file("a.py", "one\ntwo\nthree\n")
    whole = file_read("a.py", settings=settings)
    ranged = file_read("a.py", start_line=1, end_line=1, settings=settings)
    assert ranged["hash"] == whole["hash"]


def test_read_missing_file(settings):
    with pytest.raises(EditMcpError) as excinfo:
        file_read("nope.py", settings=settings)
    assert excinfo.value.code == ErrorCode.FILE_NOT_FOUND


def test_read_reports_eol_style(settings, make_file):
    make_file("a.txt", "a\r\nb\r\n")
    assert file_read("a.txt", settings=settings)["eol"] == "crlf"


# --- edit_apply -----------------------------------------------------------


def test_edit_writes_and_returns_diff(settings, make_file):
    path = make_file("a.py", "one\ntwo\nthree\n")
    digest = file_read("a.py", settings=settings)["hash"]

    result = edit_apply(
        "a.py", specs(replace(2, 2, "TWO")), base_hash=digest, settings=settings
    )

    assert result["ok"] is True
    assert result["written"] is True
    assert result["changed"] is True
    assert path.read_text(encoding="utf-8") == "one\nTWO\nthree\n"
    assert "-two" in result["diff"]
    assert result["line_count"] == {"before": 3, "after": 3}


def test_edit_hash_updates_after_write(settings, make_file):
    make_file("a.py", "one\ntwo\n")
    digest = file_read("a.py", settings=settings)["hash"]
    result = edit_apply(
        "a.py", specs(replace(1, 1, "1")), base_hash=digest, settings=settings
    )

    reread = file_read("a.py", settings=settings)
    assert result["new_hash"] == reread["hash"]


def test_stale_hash_is_rejected(settings, make_file):
    path = make_file("a.py", "one\ntwo\n")
    digest = file_read("a.py", settings=settings)["hash"]

    path.write_text("changed\nby\nsomeone\n", encoding="utf-8")

    with pytest.raises(EditMcpError) as excinfo:
        edit_apply("a.py", specs(replace(1, 1, "X")), base_hash=digest, settings=settings)

    error = excinfo.value
    assert error.code == ErrorCode.STALE_HASH
    assert error.details["current_hash"].startswith("sha256:")
    assert "Re-read" in error.hint
    assert path.read_text(encoding="utf-8") == "changed\nby\nsomeone\n"


def test_hash_prefix_is_optional(settings, make_file):
    make_file("a.py", "one\n")
    digest = file_read("a.py", settings=settings)["hash"]
    bare = digest.split(":", 1)[1]
    result = edit_apply(
        "a.py", specs(replace(1, 1, "1")), base_hash=bare, settings=settings
    )
    assert result["ok"] is True


def test_dry_run_does_not_write(settings, make_file):
    path = make_file("a.py", "one\ntwo\n")
    digest = file_read("a.py", settings=settings)["hash"]

    result = edit_apply(
        "a.py",
        specs(replace(2, 2, "TWO")),
        base_hash=digest,
        dry_run=True,
        settings=settings,
    )

    assert result["dry_run"] is True
    assert result["written"] is False
    assert "+TWO" in result["diff"]
    assert path.read_text(encoding="utf-8") == "one\ntwo\n"


def test_failed_batch_leaves_file_untouched(settings, make_file):
    path = make_file("a.py", "one\ntwo\nthree\n")
    original = path.read_text(encoding="utf-8")
    digest = file_read("a.py", settings=settings)["hash"]

    with pytest.raises(EditMcpError) as excinfo:
        edit_apply(
            "a.py",
            specs(replace(1, 1, "OK"), replace(99, 99, "BAD")),
            base_hash=digest,
            settings=settings,
        )

    assert excinfo.value.code == ErrorCode.RANGE_OUT_OF_BOUNDS
    assert path.read_text(encoding="utf-8") == original


def test_expect_guard_uses_current_range_text(settings, make_file):
    path = make_file("a.py", "def fetch_user(self):\n    pass\n")
    digest = file_read("a.py", settings=settings)["hash"]

    with pytest.raises(EditMcpError) as excinfo:
        edit_apply(
            "a.py",
            specs(replace(1, 1, "x", expect="def other_function")),
            base_hash=digest,
            settings=settings,
        )

    assert excinfo.value.code == ErrorCode.EXPECTED_TEXT_MISMATCH
    assert "def fetch_user(self):" in excinfo.value.details["current_text"]
    assert path.read_text(encoding="utf-8") == "def fetch_user(self):\n    pass\n"


def test_noop_edit_reports_no_change(settings, make_file):
    path = make_file("a.py", "same\n")
    digest = file_read("a.py", settings=settings)["hash"]

    result = edit_apply(
        "a.py", specs(replace(1, 1, "same")), base_hash=digest, settings=settings
    )

    assert result["changed"] is False
    assert result["written"] is False
    assert result["diff"] == ""
    assert path.read_text(encoding="utf-8") == "same\n"


def test_edit_preserves_crlf_endings(settings, make_file):
    path = make_file("a.txt", "a\r\nb\r\nc\r\n")
    digest = file_read("a.txt", settings=settings)["hash"]

    edit_apply("a.txt", specs(replace(2, 2, "B")), base_hash=digest, settings=settings)

    assert path.read_bytes() == b"a\r\nB\r\nc\r\n"


def test_edit_preserves_missing_final_newline(settings, make_file):
    path = make_file("a.txt", "a\nb")
    digest = file_read("a.txt", settings=settings)["hash"]

    edit_apply("a.txt", specs(replace(1, 1, "A")), base_hash=digest, settings=settings)

    assert path.read_bytes() == b"A\nb"


def test_edit_preserves_bom(settings, make_file):
    path = settings.workspace_root / "bom.txt"
    path.write_bytes(b"\xef\xbb\xbfalpha\nbeta\n")
    digest = file_read("bom.txt", settings=settings)["hash"]

    edit_apply(
        "bom.txt", specs(replace(1, 1, "ALPHA")), base_hash=digest, settings=settings
    )

    assert path.read_bytes() == b"\xef\xbb\xbfALPHA\nbeta\n"


def test_batch_applies_in_snapshot_coordinates(settings, make_file):
    path = make_file("a.py", "l1\nl2\nl3\n")
    digest = file_read("a.py", settings=settings)["hash"]

    edit_apply(
        "a.py",
        specs(
            {"op": "insert", "after_line": 0, "new_text": "top"},
            replace(3, 3, "L3"),
        ),
        base_hash=digest,
        settings=settings,
    )

    assert path.read_text(encoding="utf-8") == "top\nl1\nl2\nL3\n"


def test_too_many_edits_rejected(settings, make_file, tmp_path):
    from dataclasses import replace as dataclass_replace

    make_file("a.py", "a\n")
    digest = file_read("a.py", settings=settings)["hash"]
    tight = dataclass_replace(settings, max_edits=2)

    with pytest.raises(EditMcpError) as excinfo:
        edit_apply(
            "a.py",
            specs(*[replace(1, 1, "x")] * 3),
            base_hash=digest,
            settings=tight,
        )

    assert excinfo.value.code == ErrorCode.TOO_MANY_EDITS
