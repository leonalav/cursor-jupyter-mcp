"""Validation pipeline behaviour, end to end through ``edit_apply``.

These run the real Ruff subprocess, because the things worth testing here are
the interactions with a real tool: baseline diffing across two real lint runs,
range containment against a real formatter, and the difference between a stage
that ran and a stage that was skipped.
"""

from __future__ import annotations

import pytest

from cursor_edit_mcp.errors import EditMcpError, ErrorCode
from cursor_edit_mcp.tools.edit_apply import EditSpec, edit_apply
from cursor_edit_mcp.tools.file_create import file_create
from cursor_edit_mcp.tools.file_read import file_read
from cursor_edit_mcp.tools.validate_file import validate_file
from cursor_edit_mcp.validate.pipeline import classify

#: ``EditSpec`` fields, kept apart from the options that belong to edit_apply.
EDIT_FIELDS = {"op", "range", "after_line", "new_text", "expect"}
OPTION_FIELDS = {
    "validate",
    "format",
    "autofix",
    "require_all_stages",
    "indent_mode",
    "dry_run",
}


def build(path, settings, **kw):
    """Call ``edit_apply`` with edit fields and options split apart."""
    spec = EditSpec.model_validate({k: v for k, v in kw.items() if k in EDIT_FIELDS})
    options = {k: v for k, v in kw.items() if k in OPTION_FIELDS}
    read = file_read(path, settings=settings)
    return edit_apply(path, [spec], base_hash=read["hash"], settings=settings, **options)


def attempt(path, settings, **kw):
    """Like ``build`` but returns the error instead of raising."""
    try:
        return build(path, settings, **kw)
    except EditMcpError as exc:
        return exc


# --- classification -------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.py", "python"),
        ("a.pyi", "python"),
        ("a.md", "markdown"),
        ("a.markdown", "markdown"),
        ("a.ts", "other"),
        ("a.txt", "other"),
    ],
)
def test_classify(name, expected):
    from pathlib import Path

    assert classify(Path(name)) == expected


# --- modes ----------------------------------------------------------------


def test_off_skips_checks_but_still_formats(settings, make_file):
    """`off` disables syntax and lint. It is not a master switch.

    Format and autofix are separate knobs: turning off the check that decides
    whether the edit may be written must not silently turn off the knob that
    decides what it looks like.
    """
    make_file("m.py", "def alpha():\n    y   =   2\n    return y\n")
    result = build(
        "m.py",
        settings,
        op="replace",
        range={"start_line": 2, "end_line": 2},
        new_text="    y   =   22",
        validate="off",
        format="range",
    )
    assert result["written"] is True
    statuses = {s["stage"]: s["status"] for s in result["stages"]}
    assert statuses["syntax"] == "skipped"
    assert statuses["lint"] == "skipped"
    assert statuses["format"] == "ok"
    assert result["formatted"] is True
    assert "    y = 22\n" in (settings.workspace_root / "m.py").read_text(
        encoding="utf-8"
    )


def test_off_still_autofixes(settings, make_file):
    make_file("m.py", "import sys\nimport os\n\n\ndef alpha():\n    return 1\n")
    build(
        "m.py",
        settings,
        op="replace",
        range={"start_line": 6, "end_line": 6},
        new_text="    return 2",
        validate="off",
        format="none",
        autofix="safe",
    )
    after = (settings.workspace_root / "m.py").read_text(encoding="utf-8")
    assert after.startswith("import os\nimport sys\n")


def test_autofix_keeps_lf_endings_on_an_lf_file(settings, make_file):
    """Ruff's auto line-ending resolves to CRLF on Windows; fix content must be
    re-ended to the file's own convention or it splices CRLF into an LF file."""
    make_file("m.py", "import sys\nimport os\n\n\ndef alpha():\n    return 1\n")
    build(
        "m.py",
        settings,
        op="replace",
        range={"start_line": 6, "end_line": 6},
        new_text="    return 2",
        validate="off",
        format="none",
        autofix="safe",
    )
    raw = (settings.workspace_root / "m.py").read_bytes()
    assert b"\r\n" not in raw


def test_autofix_does_not_corrupt_overlapping_fixes(settings, make_file):
    """Ruff offers an I001 sort and an F401 delete for the same import block.

    They are alternatives covering the same characters; applying both would
    splice the file.
    """
    make_file("m.py", "import sys\nimport os\n\n\ndef alpha():\n    return 1\n")
    build(
        "m.py",
        settings,
        op="replace",
        range={"start_line": 6, "end_line": 6},
        new_text="    return 2",
        validate="off",
        format="none",
        autofix="safe",
    )
    after = (settings.workspace_root / "m.py").read_text(encoding="utf-8")
    assert after.count("def alpha():") == 1
    assert after.count("return 2") == 1
    compile(after, "m.py", "exec")


def test_off_reports_no_diagnostics(settings, make_file):
    make_file("m.py", "def alpha():\n    return 1\n")
    result = build(
        "m.py",
        settings,
        op="replace",
        range={"start_line": 1, "end_line": 1},
        new_text="def alpha(:",
        validate="off",
    )
    assert result["written"] is True
    assert result["diagnostics"]["new_count"] == 0


def test_warn_applies_and_reports(settings, make_file):
    make_file("m.py", "def alpha():\n    return 1\n")
    result = build(
        "m.py",
        settings,
        op="replace",
        range={"start_line": 1, "end_line": 1},
        new_text="def alpha(:",
        validate="warn",
    )
    assert result["written"] is True
    assert result["diagnostics"]["new_count"] > 0


def test_strict_rejects_a_syntax_error(settings, make_file):
    make_file("m.py", "def alpha():\n    return 1\n")
    error = attempt(
        "m.py",
        settings,
        op="replace",
        range={"start_line": 1, "end_line": 1},
        new_text="def alpha(:",
        validate="strict",
    )
    assert isinstance(error, EditMcpError)
    assert error.code == ErrorCode.SYNTAX_ERROR_INTRODUCED


def test_strict_rejection_leaves_the_file_untouched(settings, make_file):
    before = "def alpha():\n    return 1\n"
    make_file("m.py", before)
    attempt(
        "m.py",
        settings,
        op="replace",
        range={"start_line": 1, "end_line": 1},
        new_text="def alpha(:",
        validate="strict",
    )
    assert (settings.workspace_root / "m.py").read_text(encoding="utf-8") == before


def test_rejection_includes_the_diff(settings, make_file):
    make_file("m.py", "def alpha():\n    return 1\n")
    error = attempt(
        "m.py",
        settings,
        op="replace",
        range={"start_line": 1, "end_line": 1},
        new_text="def alpha(:",
        validate="strict",
    )
    assert error.details["rejected_diff"]
    assert "def alpha" in error.details["rejected_diff"]


def test_unknown_validate_mode_rejected(settings, make_file):
    make_file("m.py", "x = 1\n")
    with pytest.raises(EditMcpError):
        build(
            "m.py",
            settings,
            op="delete",
            range={"start_line": 1, "end_line": 1},
            validate="magic",
        )


# --- baseline diffing -----------------------------------------------------


def test_preexisting_lint_debt_does_not_block(settings, make_file):
    """The promise that makes this usable on a real codebase."""
    make_file("m.py", "import sys\n\n\ndef alpha():\n    return 1\n")
    result = build(
        "m.py",
        settings,
        op="replace",
        range={"start_line": 5, "end_line": 5},
        new_text="    return 2",
        validate="strict",
    )
    assert result["written"] is True
    assert result["diagnostics"]["new_count"] == 0


def test_new_lint_error_is_reported_in_warn(settings, make_file):
    make_file("m.py", "def alpha():\n    return 1\n")
    result = build(
        "m.py",
        settings,
        op="replace",
        range={"start_line": 2, "end_line": 2},
        new_text="    unused = 1\n    return 1",
        validate="warn",
        format="none",
    )
    codes = [d["code"] for d in result["diagnostics"]["new"]]
    assert "F841" in codes


def test_new_lint_error_blocks_in_strict(settings, make_file):
    make_file("m.py", "def alpha():\n    return 1\n")
    error = attempt(
        "m.py",
        settings,
        op="replace",
        range={"start_line": 2, "end_line": 2},
        new_text="    unused = 1\n    return 1",
        validate="strict",
        format="none",
    )
    assert isinstance(error, EditMcpError)
    assert error.code == ErrorCode.LINT_REGRESSION


def test_diagnostics_survive_a_line_shift(settings, make_file):
    """Inserting above an existing problem must not re-report it.

    The unused-import warning moves down a line; it is still the same warning,
    so it must not appear in the "new" set.
    """
    make_file("m.py", "import sys\n\n\ndef alpha():\n    return 1\n")
    result = build(
        "m.py",
        settings,
        op="insert",
        after_line=0,
        new_text="# a comment\n",
        validate="warn",
        format="none",
    )
    assert result["diagnostics"]["new_count"] == 0
    assert result["diagnostics"]["preexisting_count"] >= 1


# --- range-contained format ----------------------------------------------


def test_format_does_not_touch_untouched_code(settings, make_file):
    source = (
        "import sys\n"
        "import os\n"
        "\n"
        "\n"
        "def alpha():\n"
        "    y   =   2\n"
        "    return y\n"
        "\n"
        "\n"
        "def beta():\n"
        "    z   =   3\n"
        "    return z\n"
    )
    make_file("m.py", source)
    build(
        "m.py",
        settings,
        op="replace",
        range={"start_line": 6, "end_line": 6},
        new_text="    y   =   22",
        validate="warn",
        format="range",
    )
    after = (settings.workspace_root / "m.py").read_text(encoding="utf-8")

    # The edited line was formatted...
    assert "    y = 22\n" in after
    # ...and nothing else was.
    assert "    z   =   3\n    return z\n" in after
    assert after.startswith("import sys\nimport os\n")


def test_format_none_leaves_spacing_alone(settings, make_file):
    make_file("m.py", "x   =   1\n")
    build(
        "m.py",
        settings,
        op="replace",
        range={"start_line": 1, "end_line": 1},
        new_text="y   =   2",
        validate="off",
        format="none",
    )
    assert (settings.workspace_root / "m.py").read_text(encoding="utf-8") == "y   =   2\n"


# --- CRLF round trip ------------------------------------------------------


def test_crlf_file_survives_validation(settings, make_file):
    make_file("m.py", "def alpha():\r\n    return 1\r\n")
    build(
        "m.py",
        settings,
        op="replace",
        range={"start_line": 2, "end_line": 2},
        new_text="    return 2",
        validate="off",
    )
    raw = (settings.workspace_root / "m.py").read_bytes()
    assert raw == b"def alpha():\r\n    return 2\r\n"


def test_crlf_format_does_not_inject_blank_lines(settings, make_file):
    """Regression: CRLF fed to Ruff makes it emit a blank line per source line."""
    make_file("m.py", "def alpha():\r\n    y   =   2\r\n    return y\r\n")
    build(
        "m.py",
        settings,
        op="replace",
        range={"start_line": 2, "end_line": 2},
        new_text="    y   =   22",
        validate="warn",
        format="range",
    )
    body = (settings.workspace_root / "m.py").read_text(encoding="utf-8")
    assert body.count("\n\n\n") == 0
    assert body.replace("\r\n", "\n") == "def alpha():\n    y = 22\n    return y\n"


# --- validate_file --------------------------------------------------------


def test_validate_file_reports_a_clean_file(settings, make_file):
    make_file("m.py", "def alpha():\n    return 1\n")
    result = validate_file("m.py", settings=settings)
    assert result["ok"] is True
    assert result["validated"] == "file"


def test_validate_file_reports_problems(settings, make_file):
    make_file("m.py", "def alpha(:\n")
    result = validate_file("m.py", settings=settings)
    assert result["ok"] is False
    assert result["diagnostics"]["new_count"] > 0


def test_validate_file_accepts_proposed_content(settings, make_file):
    make_file("m.py", "x = 1\n")
    result = validate_file("m.py", content="def ok():\n    return 1\n", settings=settings)
    assert result["validated"] == "proposed"
    assert result["ok"] is True


def test_validate_file_flags_bad_proposed_content(settings, make_file):
    make_file("m.py", "x = 1\n")
    result = validate_file("m.py", content="def broken(:\n", settings=settings)
    assert result["ok"] is False


def test_validate_file_does_not_modify(settings, make_file):
    before = "def alpha():\n    return 1\n"
    make_file("m.py", before)
    validate_file("m.py", settings=settings)
    assert (settings.workspace_root / "m.py").read_text(encoding="utf-8") == before


def test_validate_file_reports_tooling(settings, make_file):
    make_file("m.py", "x = 1\n")
    result = validate_file("m.py", settings=settings)
    assert "ruff" in result["tooling"]
    assert "markdownlint" in result["tooling"]


# --- file_create ----------------------------------------------------------


def test_file_create_writes_a_file(settings):
    result = file_create("new.py", "def alpha():\n    return 1\n", settings=settings)
    assert result["created"] is True
    assert (settings.workspace_root / "new.py").exists()


def test_file_create_refuses_to_overwrite(settings, make_file):
    make_file("m.py", "x = 1\n")
    with pytest.raises(EditMcpError) as excinfo:
        file_create("m.py", "y = 2\n", settings=settings)
    assert excinfo.value.code == ErrorCode.FILE_EXISTS
    assert (settings.workspace_root / "m.py").read_text(encoding="utf-8") == "x = 1\n"


def test_file_create_overwrites_when_asked(settings, make_file):
    make_file("m.py", "x = 1\n")
    file_create("m.py", "y = 2\n", overwrite=True, settings=settings)
    assert (settings.workspace_root / "m.py").read_text(encoding="utf-8") == "y = 2\n"


def test_file_create_rejects_invalid_python_by_default(settings):
    with pytest.raises(EditMcpError) as excinfo:
        file_create("bad.py", "def broken(:\n", settings=settings)
    assert excinfo.value.code == ErrorCode.SYNTAX_ERROR_INTRODUCED
    assert not (settings.workspace_root / "bad.py").exists()


def test_file_create_can_be_told_to_warn(settings):
    result = file_create("bad.py", "def broken(:\n", validate="warn", settings=settings)
    assert result["created"] is True
    assert result["diagnostics"]["new_count"] > 0


def test_file_create_adds_a_final_newline(settings):
    file_create("n.py", "x = 1", settings=settings)
    assert (settings.workspace_root / "n.py").read_text(encoding="utf-8") == "x = 1\n"


def test_file_create_respects_the_path_jail(settings):
    with pytest.raises(EditMcpError) as excinfo:
        file_create("../escape.py", "x = 1\n", settings=settings)
    assert excinfo.value.code == ErrorCode.PATH_FORBIDDEN


# --- markdown -------------------------------------------------------------


def test_markdown_structural_error_blocks_strict(settings, make_file):
    """The original is valid; the edit is what opens the unclosed fence."""
    make_file("R.md", "# T\n\nsome text\n")
    error = attempt(
        "R.md",
        settings,
        op="replace",
        range={"start_line": 1, "end_line": 1},
        new_text="# T\n\n```python\nx = 1",
        validate="strict",
        format="none",
    )
    assert isinstance(error, EditMcpError)
    assert error.code == ErrorCode.SYNTAX_ERROR_INTRODUCED


def test_clean_markdown_edit_succeeds(settings, make_file):
    make_file("R.md", "# Title\n\nSome text.\n")
    result = build(
        "R.md",
        settings,
        op="replace",
        range={"start_line": 1, "end_line": 1},
        new_text="# New Title",
        validate="strict",
        format="none",
    )
    assert result["written"] is True
    assert result["diagnostics"]["new_count"] == 0


def test_other_file_types_skip_validation_stages(settings, make_file):
    make_file("notes.txt", "hello\n")
    result = build(
        "notes.txt",
        settings,
        op="replace",
        range={"start_line": 1, "end_line": 1},
        new_text="goodbye",
        validate="strict",
    )
    statuses = {s["stage"]: s["status"] for s in result["stages"]}
    assert statuses["syntax"] == "skipped"
    assert statuses["lint"] == "skipped"
    assert result["written"] is True
