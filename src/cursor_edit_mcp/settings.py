"""Runtime settings.

Settings come from the environment so the server can be wired entirely through
``mcp.json``, with no config file to keep in sync. The file is named
``settings.py`` rather than ``config.py`` to leave the planned configuration
service package (M5) free to own that name.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_EDITS = 100
DEFAULT_MAX_CONTENT_CHARS = 200_000
#: Hard timeout per validation subprocess. The PRD sets 10 s.
DEFAULT_TOOL_TIMEOUT_S = 10.0


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class Settings:
    """Resolved server settings."""

    workspace_root: Path
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_edits: int = DEFAULT_MAX_EDITS
    max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS
    #: Hard timeout per validation subprocess (Ruff, markdownlint, Prettier).
    tool_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S
    #: Extra names denied for mutation, in addition to the built-in list.
    extra_protected: tuple[str, ...] = ()


def _workspace_root() -> Path:
    """Resolve the workspace root.

    ``WORKSPACE_ROOT`` is what ``mcp.json`` sets from ``${workspaceFolder}``.
    Cursor's documented config interpolation resolves that variable in ``env``
    fields. Should a host leave it uninterpolated, the literal placeholder would
    otherwise resolve to a nonsense path and jail every real path out, so it is
    detected here and treated the same as an unset variable.
    """
    raw = os.environ.get("WORKSPACE_ROOT", "").strip()
    if raw.startswith("${") and raw.endswith("}"):
        raw = ""
    root = Path(raw).expanduser() if raw else Path.cwd()
    try:
        return root.resolve()
    except OSError:  # pragma: no cover - unresolvable path, e.g. removed drive
        return root.absolute()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    extra = tuple(
        part.strip()
        for part in os.environ.get("PROTECTED_PATHS", "").split(os.pathsep)
        if part.strip()
    )
    return Settings(
        workspace_root=_workspace_root(),
        max_file_bytes=_env_int("MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES),
        max_edits=_env_int("MAX_EDITS", DEFAULT_MAX_EDITS),
        max_content_chars=_env_int("MAX_CONTENT_CHARS", DEFAULT_MAX_CONTENT_CHARS),
        tool_timeout_s=_env_float("TOOL_TIMEOUT_S", DEFAULT_TOOL_TIMEOUT_S),
        extra_protected=extra,
    )
