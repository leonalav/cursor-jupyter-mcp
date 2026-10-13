"""Cell execution: ``execute_cell``, ``execute_all_cells``,
``execute_cells_in_range``, and ``kernel_control``.

Execution runs the cell on a real Python kernel managed by ``jupyter_client``;
the cell's outputs and ``execution_count`` are written back to the notebook
when ``persist`` is true, the same way Jupyter does. Markdown and raw cells in
a range are reported as ``skipped``; requesting one on its own returns
``CELL_NOT_CODE``.

Two extensions over the per-notebook model:

**Percent-format scripts.** The execute tools accept a ``.py`` file with
``# %%`` cell markers. Cells are run in the same way as a notebook's code
cells; outputs are *not* persisted to the file (``persist: false`` always)
because percent scripts have no on-disk location for outputs and writing
them would invent state the file never had.

**Remote kernels.** ``connection_file`` lets a notebook (or a percent
script) run on a kernel that was started elsewhere — a JupyterHub spawner,
a long-running container, or any other Jupyter-compatible process. The
file is a connection document, the JSON Jupyter writes when it starts a
kernel, with ``ip``, ``shell_port``, ``iopub_port``, ``stdin_port``,
``hb_port``, ``key``, and ``signature_scheme``. The registry connects
without owning the kernel: ``kernel_control`` with ``restart`` or
``shutdown`` on a remote session only closes the local channels.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..edit.atomic import atomic_write
from ..edit.hashing import hash_bytes
from ..errors import EditMcpError, ErrorCode
from ..logging_setup import get_logger
from ..notebook import cells as cell_ops
from ..notebook import engine, kernels
from ..notebook import execute as execution
from ..notebook.dispatch import is_percent_script, load_session
from ..notebook.session import NotebookSession
from ..settings import Settings, get_settings

_LOGGER = get_logger(__name__)


EXECUTE_CELL_DESCRIPTION = """\
Run one code cell on the notebook's kernel and return its outputs.

Works on `.ipynb` files and percent-format `.py` scripts. Identify the cell
with `cell_id` (preferred, stable) or `cell_number`. A markdown or raw cell
returns `CELL_NOT_CODE`.

The kernel is started lazily on the first call and kept alive across calls;
state persists from one cell to the next, the same as in Jupyter. The
notebook's `kernelspec` chooses the kernel; the first executable on `PATH` is
the fallback.

`persist: true` (default) writes the outputs and `execution_count` back to
the cell on `.ipynb` files. Percent-format scripts never persist outputs.

Pass `connection_file` to run on a kernel that was started elsewhere
(JupyterHub, a long-running container). The remote kernel is not owned by
this server; `restart` and `shutdown` on a remote session only close the
local channels. Image mime types are dropped by default to keep responses
small; pass `images: "base64"` to include them."""


EXECUTE_ALL_DESCRIPTION = """\
Run every code cell in the notebook, in order, on the notebook's kernel.

Works on `.ipynb` and percent-format `.py` files. Markdown and raw cells are
reported as `skipped` and otherwise untouched. `stop_on_error: true`
(default) halts at the first error; pass `false` to continue and report each
cell's status independently. Restart the kernel before running by passing
`restart_kernel: true` (default false) — this wipes the interpreter's state
and starts fresh.

For percent-format scripts the run is always non-persistent: the file is
not written back, even when `persist: true` was requested.

Each entry in the result carries `cell_number`, `cell_id`, `status`
(`ok` / `error` / `timeout` / `skipped`), the `execution_count` reported by
the kernel, `duration_ms`, and the `output` list."""


EXECUTE_RANGE_DESCRIPTION = """\
Run the code cells in a 1-based inclusive range. `cell_start` must be less
than or equal to `cell_end`, and both must be inside the notebook. Out-of-
range or reversed ranges return `CELL_RANGE_INVALID`.

Works on `.ipynb` and percent-format `.py` files. Markdown and raw cells in
the range are reported as `skipped` rather than run. Other than that, the
per-cell behaviour matches `execute_cell`. Percent-format scripts never
persist outputs."""


KERNEL_CONTROL_DESCRIPTION = """\
Manage a notebook's or percent script's kernel.

- `status` (default): returns the kernel's name, whether it is alive and
  ready, restart count, and time of last activity. No kernel is running yet
  for an untouched file, and that is reported.
- `restart`: shut the kernel down (if running) and start a fresh one. The
  next execution has a clean interpreter. Refused on remote sessions.
- `interrupt`: send SIGINT-equivalent. Use to break out of a long-running
  cell. The next execution proceeds.
- `shutdown`: stop the kernel and remove it from the registry. The next
  execution starts a new one. Refused on remote sessions; only the local
  channels are closed.

Pass `connection_file` to attach a remote kernel before any of the above
actions. The path is registered as a session key, and the file is the
kernel's connection document."""


def _resolve_execution_session(
    path: str,
    settings: Settings,
    *,
    connection_file: str | None,
) -> tuple[Any, bool, Path]:
    """Return ``(notebook, persist, resolved_path)``.

    ``notebook`` is a ``Notebook`` for ``.ipynb`` files and a ``Script`` for
    ``.py`` percent files. The cell tools' dispatch is reused so a ``.py``
    file with no ``# %%`` markers is rejected with ``SCRIPT_INVALID`` at the
    same point the cell tools would reject it.

    ``persist`` is ``False`` for percent scripts regardless of the caller's
    flag, because there is no on-disk location for outputs. It is left to
    the caller to enforce that; this function returns the truth.
    """
    dispatched = load_session(path, settings)
    if connection_file is not None:
        kernels.get_registry().connect_remote(dispatched.session.resolved, connection_file)
    if dispatched.kind == "ipynb":
        return (
            dispatched.session.notebook,
            True,
            dispatched.session.resolved,
            dispatched.session.current_hash,
            dispatched.kind,
        )
    return (
        dispatched.session.script,
        False,
        dispatched.session.resolved,
        dispatched.session.current_hash,
        dispatched.kind,
    )


def execute_cell(
    path: str,
    cell_id: str | None = None,
    cell_number: int | None = None,
    timeout_s: float = execution.DEFAULT_TIMEOUT_S,
    persist: bool = True,
    images: str = "discard",
    connection_file: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Run one code cell on the notebook's kernel."""
    settings = settings or get_settings()
    notebook, persist_ok, resolved, _current_hash, kind = _resolve_execution_session(
        path, settings, connection_file=connection_file
    )
    run = execution.run(
        notebook,
        cell_id=cell_id,
        cell_number=cell_number,
        timeout_s=timeout_s,
        persist=persist and persist_ok,
        images=images,
    )
    return _finish_cell(
        path,
        run,
        persist=persist,
        notebook=notebook,
        persist_ok=persist_ok,
        resolved=resolved,
        kind=kind,
    )


def execute_all_cells(
    path: str,
    stop_on_error: bool = True,
    timeout_s: float = execution.DEFAULT_TIMEOUT_S,
    persist: bool = True,
    restart_kernel: bool = False,
    images: str = "discard",
    connection_file: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Run every code cell in order."""
    settings = settings or get_settings()
    notebook, persist_ok, resolved, _current_hash, _kind = _resolve_execution_session(
        path, settings, connection_file=connection_file
    )
    runs = execution.run_many(
        notebook,
        stop_on_error=stop_on_error,
        timeout_s=timeout_s,
        persist=persist and persist_ok,
        restart_kernel=restart_kernel,
        images=images,
    )
    return _finish_many(
        path,
        runs,
        persist=persist,
        notebook=notebook,
        persist_ok=persist_ok,
        resolved=resolved,
    )


def execute_cells_in_range(
    path: str,
    cell_start: int,
    cell_end: int,
    stop_on_error: bool = True,
    timeout_s: float = execution.DEFAULT_TIMEOUT_S,
    persist: bool = True,
    restart_kernel: bool = False,
    images: str = "discard",
    connection_file: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Run a contiguous 1-based range of cells."""
    settings = settings or get_settings()
    notebook, persist_ok, resolved, _current_hash, _kind = _resolve_execution_session(
        path, settings, connection_file=connection_file
    )
    runs = execution.run_many(
        notebook,
        cell_start=cell_start,
        cell_end=cell_end,
        stop_on_error=stop_on_error,
        timeout_s=timeout_s,
        persist=persist and persist_ok,
        restart_kernel=restart_kernel,
        images=images,
    )
    return _finish_many(
        path,
        runs,
        persist=persist,
        notebook=notebook,
        persist_ok=persist_ok,
        resolved=resolved,
    )


def kernel_control(
    path: str,
    action: str = "status",
    connection_file: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Manage a notebook's kernel."""
    settings = settings or get_settings()
    # Resolve the path through the same jail the cell tools use, so a
    # relative or symlinked path lands on the same key the execute tools
    # used. The execute tools register the session under the resolved path;
    # status lookup by an unresolved path would miss it.
    from ..security.paths import resolve_path

    resolved = resolve_path(path, settings)
    if resolved.suffix.lower() not in (".ipynb", ".py"):
        raise EditMcpError(
            ErrorCode.NOTEBOOK_INVALID,
            f"{path} is not an .ipynb or .py file.",
            hint="kernel_control operates on .ipynb files and percent-format "
            ".py scripts.",
            details={"path": path, "suffix": resolved.suffix},
        )
    if connection_file is not None:
        # Connecting is idempotent: a second call with the same file just
        # replaces the existing session. The user can use status to check
        # the connection landed.
        kernels.get_registry().connect_remote(resolved, connection_file)

    registry = kernels.get_registry()

    if action == "status":
        return {
            "ok": True,
            "path": path,
            "action": "status",
            "kernel": registry.status(resolved).as_dict(),
        }

    if action == "restart":
        session = registry.status(resolved)
        if session.remote:
            return {
                "ok": False,
                "path": path,
                "action": "restart",
                "kernel": session.as_dict(),
                "note": "Remote kernels are not restarted by this server. "
                "Restart the kernel on the host that owns it.",
            }
        registry.restart(resolved)
        return {
            "ok": True,
            "path": path,
            "action": "restart",
            "kernel": registry.status(resolved).as_dict(),
        }

    if action == "interrupt":
        ok = registry.interrupt(resolved)
        return {
            "ok": ok,
            "path": path,
            "action": "interrupt",
            "kernel": registry.status(resolved).as_dict(),
            "note": None
            if ok
            else "No running kernel to interrupt; start one with execute_cell.",
        }

    if action == "shutdown":
        session = registry.status(resolved)
        if session.remote:
            # Close local channels only; the kernel itself is the user's
            # responsibility.
            registry.shutdown(resolved)
            return {
                "ok": True,
                "path": path,
                "action": "shutdown",
                "kernel": registry.status(resolved).as_dict(),
                "note": "Remote session detached. The kernel itself was "
                "left running on its host.",
            }
        ok = registry.shutdown(resolved)
        return {
            "ok": ok,
            "path": path,
            "action": "shutdown",
            "kernel": registry.status(resolved).as_dict(),
        }

    raise EditMcpError(
        ErrorCode.INVALID_EDIT,
        f"Unknown kernel action {action!r}.",
        hint="Use one of 'status', 'restart', 'interrupt', 'shutdown'.",
        details={"action": action},
    )


# --- helpers --------------------------------------------------------------


def _finish_cell(
    path: str,
    run: execution.CellRun,
    *,
    persist: bool,
    notebook: Any,
    persist_ok: bool,
    resolved: Path,
    kind: str,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "ok": run.status != "error",
        "path": path,
        "format": kind,
        "kernel": kernels.get_registry().status(resolved).as_dict(),
        "result": run.as_dict(),
    }

    if persist and persist_ok:
        body["persisted"] = _write_notebook(resolved, notebook)
    elif persist and not persist_ok:
        body["persisted"] = False
        body["persist_note"] = (
            "Percent-format scripts do not persist outputs; the run was "
            "agent-only."
        )
    return body


def _finish_many(
    path: str,
    runs: list[execution.CellRun],
    *,
    persist: bool,
    notebook: Any,
    persist_ok: bool,
    resolved: Path,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "ok": all(r.status != "error" for r in runs),
        "path": path,
        "kernel": kernels.get_registry().status(resolved).as_dict(),
        "results": [r.as_dict() for r in runs],
    }

    if persist and persist_ok:
        body["persisted"] = _write_notebook(resolved, notebook)
    elif persist and not persist_ok:
        body["persisted"] = False
        body["persist_note"] = (
            "Percent-format scripts do not persist outputs; the run was "
            "agent-only."
        )
    return body


def _write_notebook(path: Path, notebook: Any) -> dict[str, Any]:
    """Write an ``.ipynb`` after a non-persistent run is requested to persist.

    The kernel already ran, so the cell's outputs are in memory. Writing
    reflects them on disk. Schema failure here is a real bug: we control
    the shape of what we put in, and ``engine.validate_schema`` catches
    drift early.
    """
    if not hasattr(notebook, "data"):
        return {"written": False, "reason": "percent-format script"}
    cell_ops.finalize(notebook)
    engine.validate_schema(notebook.data, path)
    payload = engine.to_bytes(notebook)
    new_hash = hash_bytes(payload)
    atomic_write(path, payload, mode=None)
    return {
        "written": True,
        "new_hash": new_hash,
        "cell_count": len(notebook.cells),
    }


__all__ = [
    "EXECUTE_ALL_DESCRIPTION",
    "EXECUTE_CELL_DESCRIPTION",
    "EXECUTE_RANGE_DESCRIPTION",
    "KERNEL_CONTROL_DESCRIPTION",
    "execute_all_cells",
    "execute_cell",
    "execute_cells_in_range",
    "kernel_control",
]
