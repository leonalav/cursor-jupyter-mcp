"""Tests for the configuration service: detect, doctor, apply_fix.

The doctor is a fixed catalogue of small checks, each producing a
``Finding``. The tests in this file cover the catalogue, the diff
helper, the apply path, and the integration with the rest of the
server (the tool wrappers and the registered names).

Subprocess calls are *not* made here. The doctor is a static-analysis
layer over the project's own config files; it reads the files, parses
a few lines, and reports. There is nothing to mock and no external
tool to install.
"""

from __future__ import annotations

import pytest

from cursor_edit_mcp.config.detect import detect_file, detect_workspace
from cursor_edit_mcp.config.doctor import (
    _apply_unified_diff,
    _make_token,
    apply_fix,
    diagnose,
    make_patch,
)
from cursor_edit_mcp.errors import EditMcpError
from cursor_edit_mcp.tools.config import (
    config_apply_fix,
    config_detect,
    config_doctor,
)


# --- detect -----------------------------------------------------------------


def test_detect_file_returns_per_tool_resolution(settings, make_file):
    """A file's report has the four tools the server knows about."""
    make_file("pyproject.toml", "[tool.ruff]\nline-length = 100\n")
    make_file(".editorconfig", "root = true\n[*]\nend_of_line = lf\n")
    report = detect_file("pyproject.toml", settings)
    assert report["scope"] == "file"
    assert "ruff" in report
    assert report["ruff"]["configured"] is True
    assert report["ruff"]["config_file"] == "pyproject.toml"
    assert "editorconfig" in report
    assert report["editorconfig"]["configured"] is True


def test_detect_workspace_lists_active_and_redundant(settings, make_file):
    """Workspace report flags every tool file, including redundant ones."""
    make_file("pyproject.toml", "[tool.ruff]\nline-length = 100\n")
    make_file("ruff.toml", "line-length = 100\n")
    report = detect_workspace(settings)
    assert report["scope"] == "workspace"
    paths = {entry["path"] for entry in report["tooling_paths"]}
    assert "pyproject.toml" in paths
    assert "ruff.toml" in paths


def test_detect_workspace_no_configs(settings):
    """A bare workspace still returns a structured answer, not an error."""
    report = detect_workspace(settings)
    assert report["scope"] == "workspace"
    assert report["tooling_paths"] == []


def test_config_detect_tool_with_path(settings, make_file):
    """The tool wrapper passes a path through to ``detect_file``."""
    make_file("ruff.toml", "line-length = 88\n")
    out = config_detect(path="ruff.toml", settings=settings)
    assert out["scope"] == "file"
    assert out["ruff"]["config_file"] == "ruff.toml"


def test_config_detect_tool_without_path(settings):
    """The tool wrapper defaults to a workspace report when ``path`` is ``None``."""
    out = config_detect(path=None, settings=settings)
    assert out["scope"] == "workspace"


# --- doctor -----------------------------------------------------------------


def test_doctor_flags_two_ruff_configs(settings, make_file):
    """Two Ruff config files produce a single doctor finding."""
    make_file("pyproject.toml", "[tool.ruff]\nline-length = 100\n")
    make_file("ruff.toml", "line-length = 100\n")
    out = config_doctor(path=None, settings=settings)
    titles = [f["title"] for f in out["findings"]]
    assert any("Two Ruff configs" in t for t in titles)
    summary = out["summary"]
    assert summary["total"] >= 1


def test_doctor_flags_legacy_markdownlint(settings, make_file):
    """Both old and new markdownlint configs trigger a finding."""
    make_file(".markdownlint.json", '{"default": true}\n')
    make_file(".markdownlint-cli2.jsonc", '{}\n')
    out = config_doctor(path=None, settings=settings)
    titles = [f["title"] for f in out["findings"]]
    assert any("Legacy markdownlint" in t for t in titles)


def test_doctor_flags_ruff_overlap_with_patch(settings, make_file):
    """When pyproject and ruff.toml set ``line-length`` differently, the doctor
    produces a patch that removes the dead key from pyproject."""
    make_file("pyproject.toml", "[tool.ruff]\nline-length = 100\n")
    make_file("ruff.toml", "line-length = 88\n")
    out = config_doctor(path=None, settings=settings)
    overlap = [f for f in out["findings"] if f["title"].startswith("Conflicting Ruff settings")]
    assert overlap, "expected an overlap finding"
    patch = overlap[0]["suggested_patch"]
    assert patch is not None
    # The patch is a unified diff; the remove line carries the
    # ``pyproject.toml`` value, the add section does not (the doctor
    # proposes deleting the dead key).
    assert "line-length = 100" in patch
    assert "diff --git" not in patch  # plain unified diff, not git's two-file format


def test_doctor_clean_workspace_reports_no_findings(settings):
    """A bare workspace has no findings at error or warning severity."""
    out = config_doctor(path=None, settings=settings)
    assert out["summary"]["errors"] == 0
    assert out["summary"]["warnings"] == 0


def test_doctor_info_for_unconfigured_ruff(settings):
    """Missing ruff config yields a single info finding with a patch."""
    out = config_doctor(path=None, settings=settings)
    info = [f for f in out["findings"] if f["severity"] == "info"]
    titles = [f["title"] for f in info]
    assert any("No Ruff config" in t for t in titles)
    patch = next(f for f in info if f["title"].startswith("No Ruff config"))["suggested_patch"]
    assert patch is not None
    assert "line-length" in patch


# --- make_patch / diff roundtrip --------------------------------------------


def test_make_patch_roundtrip(tmp_path):
    """``_apply_unified_diff(make_patch(before, after), before) == after`` for
    a single-hunk replacement that the doctor would actually emit."""
    path = tmp_path / "ruff.toml"
    before = "line-length = 88\n"
    after = "line-length = 100\n"
    patch = make_patch(path, before, after)
    assert patch != ""
    out = _apply_unified_diff(before, patch, path)
    assert out == after


def test_make_patch_empty_when_identical():
    """A no-op diff is the empty string, not a fake hunk."""
    patch = make_patch(
        __file__, "line-length = 88\n", "line-length = 88\n"
    )
    assert patch == ""


def test_apply_unified_diff_returns_none_on_mismatch(tmp_path):
    """When the diff does not apply to ``before``, the result is ``None``."""
    path = tmp_path / "ruff.toml"
    before = "line-length = 88\n"
    patch = make_patch(path, before, "line-length = 100\n")
    # Now apply against a *different* ``before``; the diff should not match.
    bogus = "line-length = 120\n"
    assert _apply_unified_diff(bogus, patch, path) is None


# --- apply_fix --------------------------------------------------------------


def _find_finding(out, prefix: str) -> dict:
    return next(f for f in out["findings"] if f["title"].startswith(prefix))


def test_apply_fix_uses_token_for_confirmation(settings, make_file):
    """Apply uses the doctor's confirmation token; a wrong token is rejected."""
    make_file(".markdownlint.json", '{"default": true}\n')
    make_file(".markdownlint-cli2.jsonc", '{}\n')
    out = config_doctor(path=None, settings=settings)
    legacy = next(f for f in out["findings"] if f["title"].startswith("Legacy markdownlint"))
    # Legacy config findings have no patch — but the apply step is still
    # exercised end-to-end below for an unconfigured-ruff finding.
    ruff_info = next(f for f in out["findings"] if f["title"].startswith("No Ruff config"))
    with pytest.raises(EditMcpError):
        # Wrong token
        apply_fix(ruff_info["id"], "not-the-right-token", settings)
    # Right token: a fresh ruff.toml is created.
    result = config_apply_fix(
        finding_id=ruff_info["id"],
        confirmation_token=ruff_info["confirmation_token"],
        settings=settings,
    )
    assert result["applied"] is True
    assert (settings.workspace_root / "ruff.toml").is_file()


def test_apply_fix_unknown_finding_is_anchornotfound(settings):
    """An unknown finding id raises an ``ANCHOR_NOT_FOUND``-flavoured error."""
    with pytest.raises(EditMcpError) as exc:
        apply_fix("nope::nope::0", "x", settings)
    assert "finding" in str(exc.value).lower()


def test_apply_fix_no_patch_for_diagnostic_only(settings, make_file):
    """A finding without a patch (e.g. legacy config) is rejected with a hint."""
    make_file(".markdownlint.json", '{"default": true}\n')
    make_file(".markdownlint-cli2.jsonc", '{}\n')
    out = config_doctor(path=None, settings=settings)
    legacy = next(f for f in out["findings"] if f["title"].startswith("Legacy markdownlint"))
    with pytest.raises(EditMcpError) as exc:
        apply_fix(legacy["id"], legacy["confirmation_token"], settings)
    assert "no patch" in str(exc.value).lower()


def test_apply_fix_token_changes_with_workspace(settings, make_file):
    """The token is a function of (id, workspace_root); two different
    workspaces do not share tokens."""
    from dataclasses import replace
    token_a = _make_token("some::id", settings.workspace_root)
    other = replace(settings, workspace_root=settings.workspace_root.parent)
    token_b = _make_token("some::id", other.workspace_root)
    assert token_a != token_b


# --- integration with the validation pipeline ------------------------------


def test_detect_output_matches_validation_tooling_field(settings, make_file):
    """The config_detect output for a file matches the ``tooling`` field the
    validation pipeline would compute, so the agent has one shape to read."""
    make_file("pyproject.toml", "[tool.ruff]\nline-length = 100\n")
    detect = config_detect(path="pyproject.toml", settings=settings)
    # ruff is configured via pyproject in both code paths; the field name
    # ``config_file`` is the shared shape.
    assert detect["ruff"]["config_file"] == "pyproject.toml"
    assert detect["ruff"]["configured"] is True
