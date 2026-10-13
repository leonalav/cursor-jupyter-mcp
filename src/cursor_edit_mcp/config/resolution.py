"""Per-file configuration resolution (FR-9).

The rule this module enforces: **the server never guesses a project's config.**
Ruff, markdownlint, and Prettier each have their own, well-tested resolution
algorithm, and reimplementing it would eventually disagree with the real tool
in ways that are very hard to debug.

So this module only *finds* the files, to answer "is this tool configured for
this file?", and lets the tool itself do the interpreting. That question decides
whether the lint and format stages run at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

#: Ruff reads these, in this precedence order.
RUFF_FILES = ("ruff.toml", ".ruff.toml")

#: ruff.toml may carry a ``[tool.ruff]`` section when it is a pyproject.toml.
RUFF_PYPROJECT = "pyproject.toml"

#: Prettier resolution, simplified to the filenames that matter here.
PRETTIER_FILES = (
    ".prettierrc",
    ".prettierrc.json",
    ".prettierrc.yaml",
    ".prettierrc.yml",
    ".prettierrc.toml",
    ".prettierrc.js",
    ".prettierrc.cjs",
    "prettier.config.js",
    "prettier.config.cjs",
    "prettier.config.mjs",
    ".prettierrc.toml",
)

MARKDOWNLINT_FILES = (
    ".markdownlint.json",
    ".markdownlint.yaml",
    ".markdownlint.yml",
    ".markdownlintrc",
    ".markdownlint-cli2.jsonc",
    ".markdownlint-cli2.json",
    ".markdownlint-cli2.yaml",
    ".markdownlint-cli2.yml",
    ".markdownlint-cli2c",
)

EDITORCONFIG = ".editorconfig"


@dataclass(frozen=True)
class Resolution:
    """Which config file governs a target, if any."""

    tool: str
    configured: bool
    config_path: Path | None = None
    searched_up_to: Path | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "tool": self.tool,
            "configured": self.configured,
            "config_file": self.config_path.name if self.config_path else None,
            "reason": self.reason,
        }


@dataclass
class FileConfig:
    """Everything resolved for one file."""

    path: Path
    ruff: Resolution
    markdownlint: Resolution
    prettier: Resolution
    editorconfig: Resolution = field(
        default_factory=lambda: Resolution("editorconfig", False)
    )

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path.name,
            "ruff": self.ruff.as_dict(),
            "markdownlint": self.markdownlint.as_dict(),
            "prettier": self.prettier.as_dict(),
            "editorconfig": self.editorconfig.as_dict(),
        }


def _search_up(start: Path, names: tuple[str, ...], stop: Path | None) -> Path | None:
    """Walk from the file's directory up to the workspace root.

    Bounded at the workspace root on purpose: honouring a config from above the
    user's project would let an unrelated parent directory decide how their code
    is formatted.
    """
    current = start if start.is_dir() else start.parent
    root = (stop or current).resolve()

    while True:
        for name in names:
            candidate = current / name
            if candidate.is_file():
                return candidate
        if current == root or current.parent == current:
            return None
        try:
            current = current.parent
        except OSError:  # pragma: no cover - unreadable parent
            return None
        if root not in (current, *current.parents):
            # Walked above the workspace without finding anything.
            return None


def resolve_ruff(path: Path, root: Path) -> Resolution:
    """Find the config Ruff itself would use for this file."""
    pyproject = _search_up(path, (RUFF_PYPROJECT,), root)
    if pyproject is not None and _pyproject_has_ruff(pyproject):
        return Resolution(
            "ruff",
            True,
            pyproject,
            root,
            "Ruff is configured in pyproject.toml.",
        )

    explicit = _search_up(path, RUFF_FILES, root)
    if explicit is not None:
        return Resolution(
            "ruff", True, explicit, root, f"Ruff is configured in {explicit.name}."
        )

    # Ruff also works with no config at all, using its defaults. That is enough
    # for syntax checking, which is why this still counts as configured.
    return Resolution(
        "ruff",
        True,
        None,
        root,
        "No Ruff config found; using Ruff's defaults.",
    )


def _pyproject_has_ruff(path: Path) -> bool:
    """True if a pyproject.toml actually carries a ``[tool.ruff]`` table.

    Checking by content rather than mere existence matters: almost every Python
    project has a pyproject.toml, and treating all of them as Ruff-configured
    would mean we claim a configuration exists when there is none.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "[tool.ruff" in text


def resolve_markdownlint(path: Path, root: Path) -> Resolution:
    """markdownlint only runs when the project configures it.

    Without a config, markdownlint would apply its full default rule set to a
    document that never asked for it, producing noise the agent cannot act on.
    """
    found = _search_up(path, MARKDOWNLINT_FILES, root)
    if found is None:
        return Resolution(
            "markdownlint",
            False,
            None,
            root,
            "No .markdownlint config; skipping markdownlint.",
        )
    return Resolution("markdownlint", True, found, root, f"Configured by {found.name}.")


def resolve_prettier(path: Path, root: Path) -> Resolution:
    found = _search_up(path, PRETTIER_FILES, root)
    if found is None:
        return Resolution(
            "prettier", False, None, root, "No Prettier config; skipping Prettier."
        )
    return Resolution("prettier", True, found, root, f"Configured by {found.name}.")


def resolve_editorconfig(path: Path, root: Path) -> Resolution:
    found = _search_up(path, (EDITORCONFIG,), root)
    if found is None:
        return Resolution("editorconfig", False, None, root, "No .editorconfig.")
    return Resolution("editorconfig", True, found, root, f"Configured by {found.name}.")


def resolve_all(path: Path, root: Path) -> FileConfig:
    """Resolve every tool's configuration for one file."""
    return FileConfig(
        path=path,
        ruff=resolve_ruff(path, root),
        markdownlint=resolve_markdownlint(path, root),
        prettier=resolve_prettier(path, root),
        editorconfig=resolve_editorconfig(path, root),
    )
