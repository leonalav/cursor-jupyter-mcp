"""Type-check tool tests.

These tests run ``pyright`` and ``mypy`` subprocesses against a temp file
the tool writes. The actual tool binaries are required: they are skipped
when neither is on ``PATH`` rather than mocked, because the value of the
test is the JSON shape, the path remap, and the line/column coordinates.
Mocking the runner would test the mock, not the real behaviour.

Tests that exercise the ``content`` path also write a real file; the
``_typecheck_dir`` fixture makes the file non-existent so the ``read``
path is forced to error if it is taken instead of the ``content`` path.
"""

from __future__ import annotations

import shutil

import pytest

from cursor_edit_mcp.runners.base import Resolver


def _have_any_typechecker() -> bool:
    """True if at least one of the supported type checkers is on PATH."""
    return bool(shutil.which("pyright") or shutil.which("mypy"))


pytestmark = pytest.mark.skipif(
    not _have_any_typechecker(),
    reason="pyright and mypy are both unavailable; the type_check tool is a no-op",
)


def _have(name: str) -> bool:
    return bool(shutil.which(name))


# --- runner ---------------------------------------------------------------


def test_pyright_runner_finds_type_error(settings, make_file):
    """A clear type mismatch surfaces as a ``pyright`` diagnostic.

    The runner writes a sibling temp file so the user's config resolves
    the same way, and the diagnostic's line/column match the file
    contents, not the temp file's randomized suffix.
    """
    if not _have("pyright"):
        pytest.skip("pyright is not installed")
    make_file(
        "t.py",
        "def add(a: int, b: int) -> int:\n    return a + b\n\n"
        "result: str = add(1, 2)\nprint(result)\n",
    )
    from cursor_edit_mcp.runners import pyright

    resolver = Resolver(settings.workspace_root)
    result = pyright.check(
        (settings.workspace_root / "t.py").read_text(encoding="utf-8"),
        path=settings.workspace_root / "t.py",
        resolver=resolver,
        timeout=15.0,
    )
    assert result.outcome.ran
    assert any(d.code == "reportAssignmentType" for d in result.diagnostics)
    # The diagnostic carries the line number of the offending assignment.
    assert any(d.line == 4 for d in result.diagnostics)


def test_mypy_runner_finds_type_error(settings, make_file):
    if not _have("mypy"):
        pytest.skip("mypy is not installed")
    make_file(
        "t.py",
        "def add(a: int, b: int) -> int:\n    return a + b\n\n"
        "result: str = add(1, 2)\nprint(result)\n",
    )
    from cursor_edit_mcp.runners import mypy

    resolver = Resolver(settings.workspace_root)
    result = mypy.check(
        (settings.workspace_root / "t.py").read_text(encoding="utf-8"),
        path=settings.workspace_root / "t.py",
        resolver=resolver,
        timeout=15.0,
    )
    assert result.outcome.ran
    assert any(d.code == "assignment" for d in result.diagnostics)


def test_pyright_runner_skips_when_missing(settings, make_file, monkeypatch):
    """A missing Pyright is reported as ``skipped``, not as zero diagnostics.

    Zero findings from a tool that was never asked to run is the worst
    possible output: the user would believe the file is clean. The
    skipped result is what makes the difference visible.
    """
    from cursor_edit_mcp.runners import pyright

    monkeypatch.setattr(pyright, "PYRIGHT", "definitely-not-installed-xyz")
    make_file("t.py", "x = 1\n")
    resolver = Resolver(settings.workspace_root)
    result = pyright.check(
        (settings.workspace_root / "t.py").read_text(encoding="utf-8"),
        path=settings.workspace_root / "t.py",
        resolver=resolver,
        timeout=5.0,
    )
    assert not result.outcome.ran
    assert result.outcome.reason is not None
    assert result.diagnostics == ()


def test_mypy_runner_skips_when_missing(settings, make_file, monkeypatch):
    from cursor_edit_mcp.runners import mypy

    monkeypatch.setattr(mypy, "MYPY", "definitely-not-installed-xyz")
    make_file("t.py", "x = 1\n")
    resolver = Resolver(settings.workspace_root)
    result = mypy.check(
        (settings.workspace_root / "t.py").read_text(encoding="utf-8"),
        path=settings.workspace_root / "t.py",
        resolver=resolver,
        timeout=5.0,
    )
    assert not result.outcome.ran
    assert result.outcome.reason is not None


# --- tool ----------------------------------------------------------------


def test_type_check_picks_pyright_by_default(settings, make_file):
    """``tool: "auto"`` runs Pyright when both are installed."""
    from cursor_edit_mcp.tools.type_check import type_check

    make_file(
        "t.py",
        "def add(a: int, b: int) -> int:\n    return a + b\n\n"
        "result: str = add(1, 2)\nprint(result)\n",
    )
    result = type_check("t.py", settings=settings)
    assert result["tool"] == "pyright"


def test_type_check_with_explicit_pyright(settings, make_file):
    from cursor_edit_mcp.tools.type_check import type_check

    if not _have("pyright"):
        pytest.skip("pyright is not installed")
    make_file(
        "t.py",
        "def add(a: int, b: int) -> int:\n    return a + b\n\n"
        "result: str = add(1, 2)\nprint(result)\n",
    )
    result = type_check("t.py", tool="pyright", settings=settings)
    assert result["tool"] == "pyright"
    assert result["tool_version"]
    assert result["diagnostics"]["new_count"] >= 1
    assert result["ok"] is False


def test_type_check_with_explicit_mypy(settings, make_file):
    from cursor_edit_mcp.tools.type_check import type_check

    if not _have("mypy"):
        pytest.skip("mypy is not installed")
    make_file(
        "t.py",
        "def add(a: int, b: int) -> int:\n    return a + b\n\n"
        "result: str = add(1, 2)\nprint(result)\n",
    )
    result = type_check("t.py", tool="mypy", settings=settings)
    assert result["tool"] == "mypy"
    assert result["diagnostics"]["new_count"] >= 1


def test_type_check_clean_file_reports_zero(settings, make_file):
    """A file with no type errors returns ``ok: true`` and zero findings."""
    from cursor_edit_mcp.tools.type_check import type_check

    make_file("clean.py", "x: int = 1\ny: int = x + 1\nprint(y)\n")
    result = type_check("clean.py", settings=settings)
    assert result["ok"] is True
    assert result["diagnostics"]["new_count"] == 0


def test_type_check_with_proposed_content(settings, make_file):
    """``content`` validates the proposed text without writing.

    The temp file the runner creates is deleted by the time the call
    returns, so the workspace is left clean.
    """
    from cursor_edit_mcp.tools.type_check import type_check

    # The file does not need to exist; ``content`` is the source of truth.
    result = type_check(
        "nope.py",
        tool="pyright",
        content="def add(a: int, b: int) -> int:\n    return a + b\n\n"
        "result: str = add(1, 2)\nprint(result)\n",
        settings=settings,
    )
    assert result["validated"] == "proposed"
    if not _have("pyright"):
        pytest.skip("pyright is not installed")
    assert result["diagnostics"]["new_count"] >= 1

    # The temp file should not have leaked.
    leftovers = list(settings.workspace_root.glob("_mcp_pyright_*"))
    assert leftovers == []


def test_type_check_rejects_unknown_tool(settings, make_file):
    from cursor_edit_mcp.errors import EditMcpError, ErrorCode
    from cursor_edit_mcp.tools.type_check import type_check

    make_file("t.py", "x = 1\n")
    with pytest.raises(EditMcpError) as excinfo:
        type_check("t.py", tool="unknown", settings=settings)
    assert excinfo.value.code == ErrorCode.INVALID_EDIT


def test_type_check_reports_skipped_when_no_tool(settings, make_file, monkeypatch):
    """A missing Pyright falls back to mypy, then to ``skipped``.

    ``auto`` tries both. With both stubbed, the result is ``skipped``
    rather than a clean result: a clean result from a tool that was
    never asked is worse than a refused run.
    """
    from cursor_edit_mcp.runners import mypy, pyright
    from cursor_edit_mcp.tools.type_check import type_check

    monkeypatch.setattr(pyright, "PYRIGHT", "definitely-not-installed-xyz")
    monkeypatch.setattr(mypy, "MYPY", "definitely-not-installed-xyz")
    make_file("t.py", "x = 1\n")
    result = type_check("t.py", settings=settings)
    assert result.get("skipped") is True
    assert "reason" in result
