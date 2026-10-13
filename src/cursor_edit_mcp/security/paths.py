"""Path jail and protected paths.

Two independent gates guard every path. The jail keeps all access inside the
workspace root; the protected list denies mutation of directories and files
whose contents are not ours to rewrite (``.git``, virtualenvs, secrets).

Reads go through :func:`resolve_path`; writes additionally go through
:func:`assert_mutable`.
"""

from __future__ import annotations

from pathlib import Path, PurePath

from ..errors import EditMcpError, ErrorCode, file_not_found, path_forbidden
from ..settings import Settings

#: Directory names denied for mutation wherever they appear in the tree.
PROTECTED_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__"})

#: Filename globs denied for mutation.
PROTECTED_FILE_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "id_rsa", "id_ed25519")


def _is_protected(relative: PurePath, settings: Settings) -> str | None:
    """Return the rule name blocking mutation of ``relative``, or ``None``."""
    for part in relative.parts:
        if part in PROTECTED_DIRS:
            return f"protected directory '{part}'"
        if any(part == p for p in settings.extra_protected):
            return f"protected path '{part}' (PROTECTED_PATHS)"
    name = relative.name
    if name in settings.extra_protected:
        return f"protected path '{name}' (PROTECTED_PATHS)"
    for pattern in PROTECTED_FILE_PATTERNS:
        if Path(name).match(pattern):
            return f"protected file pattern '{pattern}'"
    return None


def resolve_path(raw: str, settings: Settings) -> Path:
    """Resolve ``raw`` to an absolute real path inside the workspace root.

    Symlinks are followed before the containment check, so a link inside the
    workspace cannot be used to reach a target outside it. Windows paths that
    arrive with forward slashes, or with a drive letter of a different case,
    resolve normally because :class:`Path` normalizes both.
    """
    if not raw or not raw.strip():
        raise EditMcpError(
            ErrorCode.INVALID_EDIT,
            "path must be a non-empty string.",
            hint="Pass a path relative to the workspace root, e.g. 'src/app.py'.",
        )

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = settings.workspace_root / candidate

    try:
        real = candidate.resolve(strict=False)
    except OSError as exc:  # pragma: no cover - platform-specific resolution failure
        raise EditMcpError(
            ErrorCode.PATH_FORBIDDEN,
            f"Could not resolve {raw}: {exc}",
            hint="Check the path for invalid characters or a missing network drive.",
            details={"path": raw},
        ) from exc

    root = settings.workspace_root
    if real != root and root not in real.parents:
        raise path_forbidden(real.as_posix(), root.as_posix(), "outside workspace root")
    return real


def assert_exists(path: Path, raw: str) -> None:
    if not path.exists():
        raise file_not_found(raw)
    if not path.is_file():
        raise EditMcpError(
            ErrorCode.NOT_A_FILE,
            f"{raw} is a directory, not a file.",
            hint="Pass a file path.",
            details={"path": raw},
        )


def assert_mutable(path: Path, raw: str, settings: Settings) -> None:
    """Reject writes to protected paths. Call after the path is resolved."""
    rule = _is_protected(path.relative_to(settings.workspace_root), settings)
    if rule is not None:
        raise path_forbidden(path.as_posix(), settings.workspace_root.as_posix(), rule)


def assert_parent_exists(path: Path, raw: str) -> None:
    """Ensure the directory that will hold a newly created file exists."""
    if not path.parent.is_dir():
        raise EditMcpError(
            ErrorCode.PATH_FORBIDDEN,
            f"Directory for {raw} does not exist.",
            hint="Create the directory first, then retry.",
            details={"path": raw, "parent": path.parent.as_posix()},
        )
