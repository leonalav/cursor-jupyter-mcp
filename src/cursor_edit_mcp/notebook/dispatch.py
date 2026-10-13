"""Dispatch helpers for the notebook tools.

A cell tool can target either a ``.ipynb`` file or a ``.py`` percent-format
script. The two paths share the same tool surface (``nb_outline``,
``nb_cell_read``, ``nb_cell_insert``, ...) but use different engines:

- ``.ipynb`` -> ``NotebookSession`` over ``notebook.engine`` and
  ``notebook.cells``.
- ``.py``   -> ``ScriptSession``  over ``notebook.script`` and
  ``notebook.script_ops``.

Both sessions expose the same minimal surface the cell tools need:
``outline``, ``resolve``, ``insert``, ``update``, ``delete``, ``move``,
``finish``. The two implementations live in their own modules; the
dispatch lives here so the tool code can stay linear.

The dispatch is also where ``.py`` files without ``# %%`` markers are
rejected: the cell tools are not for arbitrary Python files, and a clear
``SCRIPT_INVALID`` is what tells the user the wrong tool was picked.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..errors import EditMcpError, ErrorCode
from ..settings import Settings
from .script_ops import notebook_path_or_script
from .session import NotebookSession
from .script_session import ScriptSession


class _SessionLike(Protocol):
    """The shared surface the cell tools rely on."""

    resolved: Path
    current_hash: str

    def outline(self) -> list[Any]: ...
    def resolve(self, *, cell_id: str | None, cell_number: int | None) -> Any: ...
    def finish(self, *, extra: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]: ...
    def summary(self, **extra: Any) -> dict[str, Any]: ...


@dataclass
class _Dispatched:
    """What the dispatch returns: the session and a flag for the caller."""

    kind: str  # "ipynb" or "py"
    session: _SessionLike


def load_session(
    path: str,
    settings: Settings,
    *,
    base_hash: str | None = None,
) -> _Dispatched:
    """Open ``path`` as the right kind of session.

    The path resolution and the workspace jail are applied here, before the
    file extension is checked, so an out-of-workspace path is rejected the
    same way for either kind of file.
    """
    # ``notebook_path_or_script`` only inspects the suffix; the real
    # existence and permission checks happen in the session constructors.
    from ..security.paths import resolve_path

    resolved = resolve_path(path, settings)
    kind = notebook_path_or_script(resolved)

    if kind == "ipynb":
        return _Dispatched(
            kind="ipynb",
            session=NotebookSession(path, settings, base_hash=base_hash),
        )
    return _Dispatched(
        kind="py",
        session=ScriptSession(path, settings, base_hash=base_hash),
    )


def is_percent_script(path: str | Path) -> bool:
    """Quick check: is this a ``.py`` file the script engine would accept?

    Used by the execute tools to decide whether to load the file through
    the script engine (``persist: false`` always) or the notebook engine.
    A real existence check is not needed here; ``load`` will raise
    ``SCRIPT_INVALID`` if the file is a ``.py`` without markers.
    """
    return Path(path).suffix.lower() == ".py"
