"""Run code on a notebook's kernel and return structured results.

The execution model is the Jupyter one: send a ``execute_request`` over the
shell channel, then drain the iopub channel for ``stream``, ``execute_result``,
``display_data``, ``error``, and ``status`` messages until the kernel reports
idle. Each cell is run in isolation from the engine's point of view, but
state is shared with previous cells on the same kernel — that is the point of
running them in a kernel.

Timeouts are enforced by us, not the kernel: the kernel has no concept of
"give up after N seconds", so we keep draining messages and, if the deadline
expires before idle, ask the kernel to interrupt itself and report a timeout
result. The next execution then has a chance to proceed.

A markdown or raw cell inside a range is reported as ``skipped`` rather than
executed; an explicit request to run a non-code cell on its own returns
``CELL_NOT_CODE``.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from typing import Any

from ..errors import EditMcpError, ErrorCode
from . import cells as cell_ops
from . import engine
from .engine import source_text
from .kernels import KernelRegistry, get_registry, kernelspec_from_notebook


@dataclass(frozen=True)
class CellRun:
    cell_id: str
    cell_number: int
    status: str  # "ok" | "error" | "timeout" | "skipped"
    execution_count: int | None
    duration_ms: int
    output: list[dict[str, Any]]
    error: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "cell_number": self.cell_number,
            "cell_id": self.cell_id,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "output": self.output,
        }
        if self.execution_count is not None:
            body["execution_count"] = self.execution_count
        if self.error is not None:
            body["error"] = self.error
        return body


#: Per-cell timeout floor. The MCP tool takes its own; this is only the
#: default when the caller did not specify one.
DEFAULT_TIMEOUT_S = 30.0
#: How long to wait after an interrupt for the kernel to confirm idle.
INTERRUPT_SETTLE_S = 5.0
#: How long between polls of the iopub channel.
_POLL_S = 0.1


def _kernelspec_for(notebook) -> str | None:
    """Pick the right kernelspec for the file being executed.

    Notebooks carry their kernelspec in the JSON metadata, so a notebook
    whose ``kernelspec`` is ``ir`` runs on R. Percent-format scripts do
    not carry that metadata, and the default is whatever Jupyter falls
    back to (``python3``). The user can still pass a connection file
    through the execute tool to override.
    """
    data = getattr(notebook, "data", None)
    if data is None:
        return None
    return kernelspec_from_notebook(data)


def run(
    notebook: engine.Notebook,
    *,
    cell_id: str | None = None,
    cell_number: int | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    persist: bool = True,
    restart_kernel: bool = False,
    images: str = "discard",
    registry: KernelRegistry | None = None,
) -> CellRun:
    """Run one code cell on the notebook's kernel.

    Markdown and raw cells return ``CELL_NOT_CODE``. A bare execution count is
    reported only when the kernel confirms one.
    """
    if images not in ("discard", "base64"):
        raise EditMcpError(
            ErrorCode.INVALID_EDIT,
            f"images={images!r} is not supported.",
            hint="Use 'discard' (default) or 'base64'.",
            details={"images": images},
        )

    ref = cell_ops.resolve(notebook, cell_id=cell_id, cell_number=cell_number)
    cell = notebook.cells[ref.index]
    if cell.get("cell_type") != "code":
        raise EditMcpError(
            ErrorCode.CELL_NOT_CODE,
            f"Cell {ref.cell_number} is {cell.get('cell_type')!r}, not code.",
            hint="Use a code cell, or pick a different cell.",
            details={
                "path": notebook.path.name,
                "cell_number": ref.cell_number,
                "cell_type": cell.get("cell_type"),
            },
        )

    registry = registry or get_registry()
    if restart_kernel:
        registry.restart(notebook.path)
    session = registry.start(
        notebook.path,
        kernelspec=_kernelspec_for(notebook),
    )
    session.last_activity = time.time()

    code_text = source_text(cell.get("source"))
    with session.lock:
        run_started = time.time()
        result = _execute(
            session,
            code_text,
            timeout_s=timeout_s,
            images=images,
        )

    duration_ms = int((time.time() - run_started) * 1000)

    if persist:
        _persist(cell, result, duration_ms=duration_ms)

    return CellRun(
        cell_id=ref.cell_id,
        cell_number=ref.cell_number,
        status=result["status"],
        execution_count=result.get("execution_count"),
        duration_ms=duration_ms,
        output=result["output"],
        error=result.get("error"),
    )


def run_many(
    notebook: engine.Notebook,
    *,
    cell_numbers: list[int] | None = None,
    cell_start: int | None = None,
    cell_end: int | None = None,
    stop_on_error: bool = True,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    persist: bool = True,
    restart_kernel: bool = False,
    images: str = "discard",
    registry: KernelRegistry | None = None,
) -> list[CellRun]:
    """Run a sequence of code cells in order.

    ``cell_numbers`` is explicit (used by ``execute_all_cells``). A ``None``
    list is treated as "all code cells". ``cell_start``/``cell_end`` is
    inclusive and 1-based.
    """
    if cell_numbers is None and cell_start is not None and cell_end is not None:
        if cell_start > cell_end:
            raise EditMcpError(
                ErrorCode.CELL_RANGE_INVALID,
                f"cell_start ({cell_start}) must be <= cell_end ({cell_end}).",
                hint="Pass an inclusive range, or call execute_all_cells.",
                details={"cell_start": cell_start, "cell_end": cell_end},
            )
        if not 1 <= cell_start <= len(notebook.cells):
            raise EditMcpError(
                ErrorCode.CELL_RANGE_INVALID,
                f"cell_start {cell_start} is out of range.",
                details={"cell_count": len(notebook.cells), "cell_start": cell_start},
            )
        if not 1 <= cell_end <= len(notebook.cells):
            raise EditMcpError(
                ErrorCode.CELL_RANGE_INVALID,
                f"cell_end {cell_end} is out of range.",
                details={"cell_count": len(notebook.cells), "cell_end": cell_end},
            )
        cell_numbers = list(range(cell_start, cell_end + 1))
    elif cell_numbers is None:
        cell_numbers = list(range(1, len(notebook.cells) + 1))

    results: list[CellRun] = []
    for number in cell_numbers:
        ref = cell_ops.resolve(notebook, cell_number=number)
        cell = notebook.cells[ref.index]
        if cell.get("cell_type") != "code":
            results.append(
                CellRun(
                    cell_id=ref.cell_id,
                    cell_number=ref.cell_number,
                    status="skipped",
                    execution_count=None,
                    duration_ms=0,
                    output=[],
                    error={"reason": "not_code", "cell_type": cell.get("cell_type")},
                )
            )
            continue

        result = run(
            notebook,
            cell_number=number,
            timeout_s=timeout_s,
            persist=persist,
            restart_kernel=restart_kernel and not results,
            images=images,
            registry=registry,
        )
        results.append(result)

        if stop_on_error and result.status == "error":
            break

    return results


# --- internals ------------------------------------------------------------


def _execute(
    session: Any,
    code_text: str,
    *,
    timeout_s: float,
    images: str,
) -> dict[str, Any]:
    """Send one execution request and drain iopub until idle or timeout.

    The execution count lives on the shell ``execute_reply``, not on iopub, so
    we ask the shell channel for that reply in parallel with draining iopub for
    the user's outputs. Both end when the kernel reports idle.
    """
    client: Any = session.client
    parent = client.execute(code_text, store_history=True)

    captured: list[dict[str, Any]] = []
    exec_count: int | None = None
    error: dict[str, Any] | None = None
    deadline = time.time() + timeout_s
    interrupted = False

    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        try:
            msg = client.get_iopub_msg(timeout=min(remaining, 0.5))
        except Exception:
            if not client.is_alive():
                return {
                    "status": "error",
                    "output": captured,
                    "execution_count": exec_count,
                    "error": {
                        "reason": "kernel_died",
                        "ename": "KernelDied",
                        "evalue": "The kernel process exited before completing.",
                    },
                }
            continue

        if msg.get("parent_header", {}).get("msg_id") != parent:
            continue

        kind = msg["msg_type"]
        content = msg["content"]

        if kind == "execute_input":
            # Sent on iopub before the cell runs; the canonical count without
            # having to also drain the shell channel, which the kernel's
            # earlier kernel_info_reply can sit behind.
            if exec_count is None:
                exec_count = content.get("execution_count")
        elif kind == "stream":
            captured.append(
                {
                    "output_type": "stream",
                    "name": content.get("name", "stdout"),
                    "text": content.get("text", ""),
                }
            )
        elif kind == "execute_result":
            exec_count = content.get("execution_count")
            captured.append(
                _result_entry(
                    content.get("data") or {},
                    output_type="execute_result",
                    images=images,
                    metadata=content.get("metadata") or {},
                    execution_count=exec_count,
                )
            )
        elif kind == "display_data":
            captured.append(
                _result_entry(
                    content.get("data") or {},
                    output_type="display_data",
                    images=images,
                    metadata=content.get("metadata") or {},
                )
            )
        elif kind == "error":
            error = {
                "ename": content.get("ename", "Error"),
                "evalue": content.get("evalue", ""),
                "traceback": list(content.get("traceback") or []),
            }
        elif kind == "status" and content.get("execution_state") == "idle":
            break

    # The execute_reply carries the canonical execution_count for cells whose
    # only output was a stream, but the shell channel can hold an earlier
    # ``kernel_info_reply`` in front of it. Drain a couple of non-matching
    # shell messages first; matching ones are accepted.
    for _ in range(4):
        try:
            reply = client.get_shell_msg(timeout=0.2)
        except Exception:
            break
        if reply is None or reply.get("parent_header", {}).get("msg_id") != parent:
            continue
        if reply["msg_type"] == "execute_reply":
            content = reply["content"]
            if exec_count is None:
                exec_count = content.get("execution_count")
            if content.get("status") == "error" and error is None:
                payload = content.get("payload") or []
                error = {
                    "ename": "ExecutionError",
                    "evalue": " ".join(str(p.get("text") or "") for p in payload).strip(),
                    "traceback": [],
                }
        break

    if deadline - time.time() <= 0 and not error:
        if not interrupted:
            with contextlib.suppress(Exception):
                session.manager.interrupt_kernel()
            interrupted = True
        # Drain for a few seconds in case a KeyboardInterrupt message arrives
        # so the user sees something on the wire.
        settle_deadline = time.time() + INTERRUPT_SETTLE_S
        while time.time() < settle_deadline:
            try:
                msg = client.get_iopub_msg(timeout=0.2)
            except Exception:
                continue
            if msg.get("parent_header", {}).get("msg_id") != parent:
                continue
            kind = msg["msg_type"]
            content = msg["content"]
            if kind == "error":
                error = {
                    "ename": content.get("ename", "TimeoutError"),
                    "evalue": "Execution exceeded the timeout and was interrupted.",
                    "traceback": list(content.get("traceback") or []),
                    "reason": "timeout",
                }
                break
            if kind == "status" and content.get("execution_state") == "idle":
                break
        if error is None:
            error = {
                "ename": "TimeoutError",
                "evalue": f"Execution did not complete within {timeout_s:.0f}s.",
                "traceback": [],
                "reason": "timeout",
            }

    status = "ok" if error is None else "error"
    return {
        "status": status,
        "output": captured,
        "execution_count": exec_count,
        "error": error,
    }


def _result_entry(
    data: dict,
    *,
    output_type: str,
    images: str,
    metadata: dict | None = None,
    execution_count: int | None = None,
) -> dict[str, Any]:
    """Pick which mime types to keep in the response.

    Text-bearing mime types are always kept. Image mime types are kept only
    when ``images='base64'``; otherwise they are dropped to keep responses
    small and avoid sending binary over the MCP wire.

    ``display_data`` and ``execute_result`` both require a ``metadata`` field
    per the nbformat schema, so it is always present, defaulted to ``{}`` if
    the kernel did not send one. ``execute_result`` additionally requires
    ``execution_count``.
    """
    text_keys = ("text/plain", "text/html", "text/latex", "application/json")
    image_keys = ("image/png", "image/jpeg", "image/svg+xml")

    payload: dict[str, Any] = {}
    for key in text_keys:
        if key in data:
            payload[key] = data[key]
    if images == "base64":
        for key in image_keys:
            if key in data:
                payload[key] = data[key]
    else:
        for key in image_keys:
            if key in data:
                payload.setdefault("_truncated_mime_types", []).append(key)
    if not payload:
        payload = {"text/plain": ""}

    entry: dict[str, Any] = {"output_type": output_type, "data": payload}
    if output_type in ("display_data", "execute_result"):
        entry["metadata"] = metadata if metadata is not None else {}
    if output_type == "execute_result":
        # The schema requires this. The kernel normally provides it, but if a
        # streaming-output edge case ever arrives without one we still need a
        # well-formed notebook on disk.
        entry["execution_count"] = execution_count
    return entry


def _persist(cell: dict, result: dict[str, Any], *, duration_ms: int) -> None:
    """Write the outputs back into the cell.

    Matches the Jupyter convention: outputs and ``execution_count`` live on
    the cell itself so opening the notebook elsewhere shows the result.
    """
    if cell.get("cell_type") != "code":
        return
    cell["outputs"] = result["output"]
    cell["execution_count"] = result.get("execution_count")
    cell.setdefault("metadata", {})["duration_ms"] = duration_ms
    if result.get("error"):
        cell["metadata"].setdefault("last_error", {})["ename"] = result["error"].get(
            "ename"
        )
