"""Baseline diffing and fingerprinting.

The contract these tests protect: an edit is reported only for the problems it
actually introduced. A file that already has lint debt must stay editable, and a
problem that merely moved because of a line shift must not be re-reported.
"""

from __future__ import annotations

from cursor_edit_mcp.validate.diagnostics import Diagnostic, diff


def make(code="F401", message="unused import", line=1, source="import os", **kw):
    return Diagnostic(
        tool=kw.pop("tool", "ruff"),
        code=code,
        message=message,
        severity=kw.pop("severity", "error"),
        line=line,
        column=1,
        source_line=source,
        **kw,
    )


def test_no_diagnostics_diff_is_empty():
    result = diff((), ())
    assert result.new == ()
    assert result.preexisting_count == 0
    assert result.resolved_count == 0


def test_new_diagnostic_is_reported():
    result = diff((), (make(),))
    assert len(result.new) == 1
    assert result.has_new_errors is True


def test_unchanged_diagnostic_is_preexisting():
    """The core promise: an untouched problem is not the edit's fault."""
    result = diff((make(),), (make(),))
    assert result.new == ()
    assert result.preexisting_count == 1
    assert result.has_new_errors is False


def test_diagnostic_that_only_moved_lines_is_preexisting():
    """An edit above a problem shifts it; that is not a new problem."""
    result = diff((make(line=10),), (make(line=25),))
    assert result.new == ()
    assert result.preexisting_count == 1


def test_whitespace_only_change_is_the_same_finding():
    result = diff(
        (make(source="import  os"),),
        (make(source="import os"),),
    )
    assert result.new == ()


def test_different_rule_on_same_line_is_new():
    result = diff((make(code="F401"),), (make(code="F811"),))
    assert len(result.new) == 1


def test_resolved_count():
    result = diff((make(),), ())
    assert result.resolved_count == 1
    assert result.new == ()


def test_multiplicity_counts_duplicates_separately():
    """Two identical findings before, one after, is a resolution, not a new one."""
    result = diff((make(), make()), (make(),))
    assert result.new == ()
    assert result.resolved_count == 1


def test_extra_duplicate_counts_as_new():
    result = diff((make(),), (make(), make()))
    assert len(result.new) == 1


def test_warning_does_not_block_strict():
    result = diff((), (make(severity="warning"),))
    assert result.has_new_errors is False
    assert len(result.new) == 1


def test_report_shape_is_compact():
    body = diff((), (make(),)).as_dict()
    assert body["new_count"] == 1
    assert body["new_error_count"] == 1
    assert body["preexisting_count"] == 0
    assert body["truncated"] is False
    assert body["new"][0]["code"] == "F401"


def test_report_truncates_but_keeps_the_truthful_count():
    many = tuple(make(line=n, source=f"import x{n}") for n in range(1, 31))
    body = diff((), many).as_dict(cap=5)
    assert body["new_count"] == 30
    assert len(body["new"]) == 5
    assert body["truncated"] is True


def test_preexisting_count_reflects_survivors_not_total():
    result = diff((make(code="A"), make(code="B")), (make(code="A"),))
    assert result.preexisting_count == 1
    assert result.resolved_count == 1


def test_diagnostic_dict_omits_blank_source_line():
    assert "source_line" not in make(source="   ").as_dict()
    assert "source_line" in make(source="x = 1").as_dict()


def test_fingerprint_is_stable_and_line_independent():
    assert make(line=3).fingerprint == make(line=99).fingerprint
    assert make(line=3).fingerprint != make(code="F811").fingerprint
