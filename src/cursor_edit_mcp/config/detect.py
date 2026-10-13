"""``config_detect``: report the effective tooling for a file or workspace.

The validation pipeline already calls :func:`resolution.resolve_all` on every
edit. ``config_detect`` is the same call, exposed as a tool, so the agent can
ask "what tools govern this file?" before touching it, or "what tools are
configured for the workspace?" when it is choosing a tool to run.

**Read-only by design.** The doctor writes patches; detection does not. Every
report returns the resolution for each tool the editor cares about (Ruff,
markdownlint, Prettier, EditorConfig) plus a ``tooling_paths`` list of files
that govern the project, which the doctor will diff against each other.

**Two modes.** Pass a ``path`` to resolve for one file, or no path to scan
the whole workspace. The workspace scan walks the project root, finds the
config files that govern it, and returns a single per-tool summary. That
summary is what the doctor consumes; the doctor never has to walk the tree
itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..security.paths import resolve_path
from ..settings import Settings, get_settings
from .resolution import (
    MARKDOWNLINT_FILES,
    PRETTIER_FILES,
    RUFF_FILES,
    RUFF_PYPROJECT,
    Resolution,
    resolve_all,
    resolve_editorconfig,
    resolve_markdownlint,
    resolve_prettier,
    resolve_ruff,
)

_LOGGER = get_logger(__name__)

#: Tools this server knows about, in the order they appear in reports.
#: The order is intentional: the ones with the strongest defaults appear
#: first so a "no" answer for a later tool is visually quieter.
TOOLS: tuple[str, ...] = ("ruff", "markdownlint", "prettier", "editorconfig")


@dataclass(frozen=True)
class ToolingPath:
    """One config file the project actually has on disk.

    Distinct from a :class:`Resolution` because ``Resolution`` describes
    *which* file governs a target, while a ``ToolingPath`` lists every file
    the project has, including ones that have been overridden by a
    higher-precedence file. The doctor compares the two.
    """

    tool: str
    path: Path
    priority: int  # 0 = highest, 1 = next, ...
    is_active: bool  # True if this file actually governs something

    def as_dict(self) -> dict[str, object]:
        return {
            "tool": self.tool,
            "path": self.path.name,
            "priority": self.priority,
            "active": self.is_active,
        }


@dataclass
class WorkspaceConfigReport:
    """The whole-project summary the doctor consumes."""

    workspace_root: Path
    tools: dict[str, Resolution]
    tooling_paths: list[ToolingPath] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "workspace_root": self.workspace_root.name,
            "tools": {name: self.tools[name].as_dict() for name in TOOLS if name in self.tools},
            "tooling_paths": [p.as_dict() for p in self.tooling_paths],
        }


def _file_resolution(path: Path, root: Path) -> dict[str, Any]:
    """Per-file resolution report.

    Same shape as the ``tooling`` field already produced by ``validate_file``;
    the duplication is deliberate so the agent has a stable answer
    regardless of which tool it called.
    """
    config = resolve_all(path, root)
    return config.as_dict()


def detect_file(path: str, settings: Settings | None = None) -> dict[str, Any]:
    """Resolve the effective tooling for one file."""
    settings = settings or get_settings()
    resolved = resolve_path(path, settings)
    report = _file_resolution(resolved, settings.workspace_root)
    _LOGGER.info(
        "config_detect.file",
        extra={"context": {"path": resolved.as_posix()}},
    )
    return {"scope": "file", "path": path, **report}


def _walk_workspace(root: Path) -> WorkspaceConfigReport:
    """Find every tooling file the project has, then resolve each tool.

    The "active" flag on a tooling path is decided by the per-tool
    resolver: Ruff says which of two config files wins; the resolver for
    Prettier only knows about a single path per project, so the
    pre-existing one is always active.
    """
    tooling: list[ToolingPath] = []
    active: dict[str, Path] = {}

    # Ruff: pyproject with [tool.ruff] is priority 0, an explicit
    # ruff.toml is priority 1, the .ruff.toml variant priority 2. We
    # inspect the file content for pyproject so an empty pyproject
    # does not count.
    pyproject = root / RUFF_PYPROJECT
    pyproject_ruff = pyproject.is_file() and _has_table(pyproject, "[tool.ruff")
    seen_ruff: set[Path] = set()
    if pyproject_ruff:
        tooling.append(ToolingPath("ruff", pyproject, 0, is_active=True))
        active["ruff"] = pyproject
        seen_ruff.add(pyproject)
    for priority, name in enumerate(RUFF_FILES, start=1 if pyproject_ruff else 0):
        candidate = root / name
        if candidate.is_file() and candidate not in seen_ruff:
            is_active = "ruff" not in active
            tooling.append(ToolingPath("ruff", candidate, priority, is_active=is_active))
            seen_ruff.add(candidate)
            active.setdefault("ruff", candidate)

    # markdownlint, Prettier, EditorConfig: a single name list each.
    # All candidates are recorded; the first found is the active one.
    for tool, names in (
        ("markdownlint", MARKDOWNLINT_FILES),
        ("prettier", PRETTIER_FILES),
        ("editorconfig", (".editorconfig",)),
    ):
        seen: set[Path] = set()
        for priority, name in enumerate(names):
            candidate = root / name
            if candidate.is_file() and candidate not in seen:
                is_active = tool not in active
                tooling.append(ToolingPath(tool, candidate, priority, is_active=is_active))
                seen.add(candidate)
                active.setdefault(tool, candidate)

    # Per-tool resolutions, rooted at the workspace. These are the
    # canonical answers the validation pipeline will use.
    any_path = root / "README.md"  # resolution needs *some* path to walk from
    if not any_path.is_file():
        any_path = root  # fall back to the root; resolution walks the root
    tools = {
        "ruff": resolve_ruff(any_path, root),
        "markdownlint": resolve_markdownlint(any_path, root),
        "prettier": resolve_prettier(any_path, root),
        "editorconfig": resolve_editorconfig(any_path, root),
    }
    return WorkspaceConfigReport(
        workspace_root=root, tools=tools, tooling_paths=tooling
    )


def _has_table(path: Path, marker: str) -> bool:
    """Cheap check: does the file contain the marker line for a tool table?

    The check is byte-level, not a real TOML parse. That is the right
    trade-off for a doctor pass: a missing table is the only signal that
    matters, and a real parse would pull in a TOML dependency that
    nothing else needs.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return marker in text


def detect_workspace(settings: Settings | None = None) -> dict[str, Any]:
    """Resolve the effective tooling for the whole workspace."""
    settings = settings or get_settings()
    report = _walk_workspace(settings.workspace_root)
    _LOGGER.info(
        "config_detect.workspace",
        extra={"context": {"workspace_root": settings.workspace_root.as_posix()}},
    )
    return {"scope": "workspace", **report.as_dict()}


#: Tool description string surfaced in the MCP tool list.
DETECT_DESCRIPTION = """\
Report the effective tooling for a file, or for the whole workspace.

Use this when the question is "what governs this file?" — for example,
before running the validation pipeline, before adding a lint rule, or
when a tool reports an unexpected setting. The validation pipeline
already reports the same answer alongside its diagnostics, but reading
this first lets you plan a change before you commit to it.

`path` resolves for a single file. Calling the tool with no `path`
returns a workspace summary: which tools are configured, which config
file is the active one, and which other files exist for the same tool
(some of which the doctor will flag as redundant or conflicting).

The report is read-only. To find conflicts and propose fixes, use
`config_doctor`."""


__all__ = [
    "DETECT_DESCRIPTION",
    "TOOLS",
    "ToolingPath",
    "WorkspaceConfigReport",
    "detect_file",
    "detect_workspace",
]
