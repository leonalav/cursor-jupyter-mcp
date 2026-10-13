"""Workspace root resolution.

The path jail is the server's main security boundary, so the rules that decide
what "inside the workspace" means are tested directly, including the case where
a host leaves ``${workspaceFolder}`` uninterpolated.
"""

from __future__ import annotations

import pytest

from cursor_edit_mcp import settings as settings_module
from cursor_edit_mcp.settings import Settings


@pytest.fixture(autouse=True)
def clear_settings_cache():
    settings_module.get_settings.cache_clear()
    yield
    settings_module.get_settings.cache_clear()


def resolve_with(monkeypatch, tmp_path, raw: str | None) -> Settings:
    monkeypatch.chdir(tmp_path)
    if raw is None:
        monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    else:
        monkeypatch.setenv("WORKSPACE_ROOT", raw)
    return settings_module.get_settings()


def test_workspace_root_from_env(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    assert (
        resolve_with(monkeypatch, project, str(project)).workspace_root
        == project.resolve()
    )


def test_workspace_root_defaults_to_cwd(tmp_path, monkeypatch):
    assert resolve_with(monkeypatch, tmp_path, None).workspace_root == tmp_path.resolve()


def test_uninterpolated_placeholder_falls_back_to_cwd(tmp_path, monkeypatch):
    """A literal ``${workspaceFolder}`` must not become a nonsense root.

    Resolving it as a path would jail every real path out of the workspace and
    make every tool call fail with PATH_FORBIDDEN.
    """
    settings = resolve_with(monkeypatch, tmp_path, "${workspaceFolder}")
    assert settings.workspace_root == tmp_path.resolve()


def test_blank_env_falls_back_to_cwd(tmp_path, monkeypatch):
    assert resolve_with(monkeypatch, tmp_path, "   ").workspace_root == tmp_path.resolve()


def test_env_limits_are_applied(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(project))
    monkeypatch.setenv("MAX_EDITS", "7")
    monkeypatch.setenv("MAX_FILE_BYTES", "not-a-number")

    settings = settings_module.get_settings()

    assert settings.max_edits == 7
    # An unparseable value falls back to the default rather than crashing startup.
    assert settings.max_file_bytes == settings_module.DEFAULT_MAX_FILE_BYTES


def test_extra_protected_paths_are_parsed(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("PROTECTED_PATHS", "generated.json;;vendor")

    settings = settings_module.get_settings()

    assert settings.extra_protected == ("generated.json", "vendor")
