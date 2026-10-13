"""Path jail, protected paths, and atomic writes."""

from __future__ import annotations

import os
import stat

import pytest

from cursor_edit_mcp.edit.atomic import atomic_write
from cursor_edit_mcp.errors import EditMcpError, ErrorCode
from cursor_edit_mcp.security.paths import assert_mutable, resolve_path
from cursor_edit_mcp.tools.file_read import file_read

# --- path jail ------------------------------------------------------------


def test_relative_path_resolves_inside_workspace(settings, make_file):
    make_file("src/app.py", "x\n")
    assert (
        resolve_path("src/app.py", settings) == settings.workspace_root / "src" / "app.py"
    )


def test_absolute_path_inside_workspace_allowed(settings, make_file):
    target = make_file("app.py", "x\n")
    assert resolve_path(str(target), settings) == target


def test_path_outside_workspace_rejected(settings):
    with pytest.raises(EditMcpError) as excinfo:
        resolve_path("../../../etc/passwd", settings)
    assert excinfo.value.code == ErrorCode.PATH_FORBIDDEN


def test_absolute_path_outside_workspace_rejected(settings):
    with pytest.raises(EditMcpError) as excinfo:
        resolve_path("/etc/passwd", settings)
    assert excinfo.value.code == ErrorCode.PATH_FORBIDDEN


def test_symlink_escaping_workspace_rejected(settings, tmp_path):
    """A link inside the workspace must not reach a target outside it."""
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("sensitive", encoding="utf-8")
    link = settings.workspace_root / "link.txt"
    try:
        link.symlink_to(secret)
    except OSError as exc:  # pragma: no cover - needs Developer Mode on Windows
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(EditMcpError) as excinfo:
        resolve_path("link.txt", settings)
    assert excinfo.value.code == ErrorCode.PATH_FORBIDDEN


def test_symlinked_directory_escaping_workspace_rejected(settings, tmp_path):
    secret_dir = tmp_path.parent / "outside_dir"
    secret_dir.mkdir(exist_ok=True)
    (secret_dir / "leak.txt").write_text("sensitive", encoding="utf-8")

    link_dir = settings.workspace_root / "escape"
    try:
        link_dir.symlink_to(secret_dir, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - needs Developer Mode on Windows
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(EditMcpError) as excinfo:
        resolve_path("escape/leak.txt", settings)
    assert excinfo.value.code == ErrorCode.PATH_FORBIDDEN


def test_empty_path_rejected(settings):
    with pytest.raises(EditMcpError) as excinfo:
        resolve_path("", settings)
    assert excinfo.value.code == ErrorCode.INVALID_EDIT


# --- protected paths ------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        ".git/config",
        "node_modules/pkg/index.js",
        ".venv/lib/thing.py",
        "venv/lib/thing.py",
        "src/__pycache__/mod.pyc",
    ],
)
def test_protected_directories_rejected(settings, name):
    target = settings.workspace_root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")
    with pytest.raises(EditMcpError) as excinfo:
        assert_mutable(target, name, settings)
    assert excinfo.value.code == ErrorCode.PATH_FORBIDDEN


@pytest.mark.parametrize("name", [".env", ".env.local", "key.pem", "id_rsa"])
def test_protected_file_patterns_rejected(settings, name):
    target = settings.workspace_root / name
    target.write_text("x", encoding="utf-8")
    with pytest.raises(EditMcpError) as excinfo:
        assert_mutable(target, name, settings)
    assert excinfo.value.code == ErrorCode.PATH_FORBIDDEN


def test_protected_path_is_not_blocked_for_reads(settings, make_file):
    make_file(".env", "SECRET=1\n")
    assert file_read(".env", settings=settings)["content"] == "SECRET=1\n"


def test_extra_protected_names_from_settings(settings):
    from dataclasses import replace as dataclass_replace

    target = settings.workspace_root / "generated.json"
    target.write_text("{}", encoding="utf-8")
    tightened = dataclass_replace(settings, extra_protected=("generated.json",))

    with pytest.raises(EditMcpError) as excinfo:
        assert_mutable(target, "generated.json", tightened)
    assert excinfo.value.code == ErrorCode.PATH_FORBIDDEN


# --- atomic writes --------------------------------------------------------


def test_atomic_write_replaces_content(settings):
    target = settings.workspace_root / "f.txt"
    target.write_text("old", encoding="utf-8")
    atomic_write(target, b"new")
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_leaves_no_temp_files(settings):
    target = settings.workspace_root / "f.txt"
    target.write_text("old", encoding="utf-8")
    atomic_write(target, b"new")
    assert [p.name for p in settings.workspace_root.iterdir()] == ["f.txt"]


def test_atomic_write_preserves_mode(settings):
    """File mode survives the write.

    The writer chmods the temp file before renaming, so the target never exists
    with the wrong permissions, not even momentarily.
    """
    target = settings.workspace_root / "script.sh"
    target.write_text("#!/bin/sh\n", encoding="utf-8")

    if os.name == "nt":
        # Windows exposes only the read-only flag through the mode bits, and
        # making the target read-only would block its own replacement.
        atomic_write(target, b"#!/bin/sh\necho hi\n", mode=stat.S_IWRITE)
        assert target.stat().st_mode & stat.S_IWRITE
    else:
        os.chmod(target, 0o755)
        atomic_write(target, b"#!/bin/sh\necho hi\n", mode=0o755)
        assert target.stat().st_mode & 0o777 == 0o755
        assert target.read_bytes() == b"#!/bin/sh\necho hi\n"


def test_atomic_write_replaces_a_read_only_file(settings):
    """The rename must succeed even when the target cannot be written in place."""
    target = settings.workspace_root / "locked.txt"
    target.write_text("old\n", encoding="utf-8")

    if os.name == "nt":
        os.chmod(target, stat.S_IREAD)
        try:
            atomic_write(target, b"new\n", mode=stat.S_IWRITE)
            assert target.read_bytes() == b"new\n"
        finally:
            os.chmod(target, stat.S_IWRITE)
    else:
        os.chmod(target, 0o444)
        atomic_write(target, b"new\n", mode=0o644)
        assert target.read_bytes() == b"new\n"
