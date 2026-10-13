"""Shared test fixtures.

Two kinds live here.

**Workspace fixtures.** ``settings`` and ``make_file`` give every test a fresh
workspace, isolated from other tests, with a way to put files in it.

**Kernel isolation.** ``_isolate_kernels`` is autouse and closes every running
kernel before and after each test. A kernel is a real Python subprocess:
leaving one running between tests bleeds state from one test into the next.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from cursor_edit_mcp.notebook.kernels import get_registry
from cursor_edit_mcp.settings import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """A Settings instance with a fresh per-test workspace.

    Each test gets its own directory; nothing leaks between tests, and a test
    can write anywhere it wants inside ``workspace_root`` without affecting
    other tests.
    """
    return Settings(workspace_root=tmp_path)


@pytest.fixture
def make_file(settings: Settings) -> Callable[..., Path]:
    """Write a file into the test workspace.

    Returns the ``pathlib.Path`` to the written file. By default the parent
    directory is created if it does not exist; pass ``mkdirs=False`` to opt
    out. Encoding is always UTF-8.
    """

    def _write(
        name: str,
        content: str = "",
        *,
        mkdirs: bool = True,
        encoding: str = "utf-8",
    ) -> Path:
        path = settings.workspace_root / name
        if mkdirs:
            path.parent.mkdir(parents=True, exist_ok=True)
        # Write raw bytes. ``Path.write_text`` opens in text mode and, on
        # Windows, converts every ``\n`` to ``\r\n``. That mangles test
        # fixtures whose content already contains literal CRLF: a passed
        # ``\r\n`` becomes ``\r\r\n`` on disk, and the test asserts on the
        # original bytes.
        path.write_bytes(content.encode(encoding))
        return path

    return _write


@pytest.fixture(autouse=True)
def _isolate_kernels():
    """Close every running kernel before and after each test.

    A kernel is a real Python subprocess. Leaving one running between tests
    bleeds state: a ``shared = 99`` defined in test A is still in the kernel
    when test B starts, which makes ``test_kernel_status_before_any_execution``
    see a live kernel it never asked for.
    """
    registry = get_registry()
    registry.close_all()
    yield
    registry.close_all()
