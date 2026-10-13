"""Atomic file writes.

A write goes to a temporary file in the same directory, is flushed to disk, and
is then renamed over the target. Rename is atomic within a filesystem, so a
reader sees either the old file or the new one, never a partial write.

File mode is copied from the previous version so an edit never silently
widens permissions on an executable script.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from ..errors import EditMcpError, ErrorCode


def _replace(tmp_path: Path, path: Path) -> None:
    """Rename over the target, clearing a Windows read-only flag if needed.

    Windows refuses to replace a file carrying the read-only attribute, even
    when the directory itself is writable. That attribute is a mode bit we
    intend to preserve rather than a policy, so it is cleared for the rename
    and reapplied afterwards.
    """
    try:
        os.replace(tmp_path, path)
        return
    except PermissionError:
        if os.name != "nt" or not path.exists():
            raise

    current = path.stat().st_mode
    if not current & stat.S_IWRITE:
        os.chmod(path, current | stat.S_IWRITE)
        try:
            os.replace(tmp_path, path)
        finally:
            os.chmod(path, current)


def atomic_write(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """Replace ``path`` with ``data``, atomically.

    ``mode`` is applied before the rename so the file never exists with the
    wrong permissions, not even momentarily.
    """
    directory = path.parent
    handle = None
    tmp_path: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=directory, prefix=f".{path.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        handle = os.fdopen(fd, "wb")
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None

        if mode is not None:
            os.chmod(tmp_path, mode)
        _replace(tmp_path, path)
        tmp_path = None
    except OSError as exc:
        raise EditMcpError(
            ErrorCode.WRITE_FAILED,
            f"Could not write {path.name}: {exc.strerror or exc}.",
            hint=(
                "Check file permissions and that the file is not locked "
                "by another program."
            ),
            details={"path": path.as_posix(), "reason": str(exc)},
        ) from exc
    finally:
        if handle is not None:  # pragma: no cover - only on a mid-write failure
            handle.close()
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def current_mode(path: Path) -> int | None:
    """Return the existing file mode, or ``None`` for a new file."""
    try:
        return path.stat().st_mode & 0o777
    except OSError:  # pragma: no cover - stat on a path we just created
        return None
