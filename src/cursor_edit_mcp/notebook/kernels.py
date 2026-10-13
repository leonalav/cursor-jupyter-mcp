"""Per-notebook kernel lifecycle.

A kernel is a Python process that holds state across cells. The MCP server is
a long-lived process, so kernels are too: each call to ``execute_cell`` does
not start a new kernel, and the same one answers every subsequent call until
the user restarts or shuts it down.

Four rules shape this module.

**One kernel per notebook.** A kernel belongs to a notebook, not to a request
or a session. Cells on different notebooks can have different Python
interpreters and different installed packages; cells on the same notebook share
state. The mapping is a plain dict.

**Lazy start.** Nothing happens at server boot. The first execution request
starts the kernel, blocks until it is ready, and only then returns. Tests and
diagnostics that never execute code never start one.

**Local or remote.** A notebook's ``kernelspec`` is started locally on this
machine, in the user's venv. A *remote* kernel — a JupyterHub server, a
shared machine, or a long-running kernel started elsewhere — is connected to
through a connection file rather than started. The two paths are kept
separate because their lifecycles differ: local kernels this process owns,
remote kernels the user owns and we only talk to.

**Best-effort cleanup.** ``KernelManager`` does its own cleanup on
``shutdown_kernel``, but a process killed by the user or a crash may leave a
record in the registry. ``prune()`` drops those, and ``close_all()`` is the
end-of-server hook.
"""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jupyter_client import BlockingKernelClient, KernelManager

from ..errors import EditMcpError, ErrorCode


@dataclass
class KernelStatus:
    name: str
    alive: bool
    ready: bool
    restarts: int
    last_activity: float
    language: str | None = None
    remote: bool = False
    connection_file: str | None = None

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": self.name,
            "status": "idle" if self.ready else ("starting" if self.alive else "dead"),
            "alive": self.alive,
            "ready": self.ready,
            "restarts": self.restarts,
            "last_activity": self.last_activity,
            "language": self.language,
        }
        if self.remote:
            body["remote"] = True
            if self.connection_file:
                body["connection_file"] = self.connection_file
        return body


@dataclass
class _Session:
    manager: KernelManager
    client: BlockingKernelClient
    name: str
    language: str | None
    restarts: int = 0
    last_activity: float = field(default_factory=time.time)
    started_at: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: True when this session talks to a kernel this process did not start.
    #: The registry refuses to shut down or restart a remote kernel because
    #: it is not ours to kill: the user's JupyterHub session is.
    remote: bool = False
    #: Path to the connection file used for a remote session, kept for
    #: diagnostics and so the user can see where the session came from.
    connection_file: str | None = None


class KernelRegistry:
    """Per-notebook kernel sessions for one MCP server process."""

    def __init__(self) -> None:
        self._by_notebook: dict[Path, _Session] = {}
        # Restart counts survive across session replacements, which is what
        # makes ``restarts`` useful as a signal to the user: it counts how
        # many times the kernel was thrown away, not how many Session
        # dataclasses were ever created.
        self._restarts: dict[Path, int] = {}
        self._lock = threading.Lock()

    def restarts(self, notebook_path: Path) -> int:
        with self._lock:
            return self._restarts.get(notebook_path, 0)

    # --- access ----------------------------------------------------------

    def get(self, notebook_path: Path) -> _Session:
        with self._lock:
            session = self._by_notebook.get(notebook_path)
        if session is None:
            raise EditMcpError(
                ErrorCode.KERNEL_NOT_RUNNING,
                f"No kernel is running for {notebook_path.name}.",
                hint="The first call to execute_cell starts one. To force a "
                "fresh interpreter, call kernel_control with action='restart'.",
                details={"path": notebook_path.name},
            )
        return session

    def status(self, notebook_path: Path) -> KernelStatus:
        with self._lock:
            session = self._by_notebook.get(notebook_path)
        if session is None:
            return KernelStatus(
                name="",
                alive=False,
                ready=False,
                restarts=self._restarts.get(notebook_path, 0),
                last_activity=0.0,
            )
        return KernelStatus(
            name=session.name,
            alive=session.manager.is_alive(),
            ready=session.client.is_alive(),
            restarts=self._restarts.get(notebook_path, 0),
            last_activity=session.last_activity,
            language=session.language,
            remote=session.remote,
            connection_file=session.connection_file,
        )

    # --- lifecycle -------------------------------------------------------

    def start(
        self,
        notebook_path: Path,
        kernelspec: str | None = None,
        cwd: Path | None = None,
    ) -> _Session:
        with self._lock:
            existing = self._by_notebook.get(notebook_path)
        if existing is not None and existing.manager.is_alive():
            return existing

        return self._start(notebook_path, kernelspec=kernelspec, cwd=cwd)

    def _start(
        self,
        notebook_path: Path,
        *,
        kernelspec: str | None,
        cwd: Path | None,
    ) -> _Session:
        manager = KernelManager(kernel_name=kernelspec) if kernelspec else KernelManager()
        workdir = str(cwd or notebook_path.parent)

        try:
            manager.start_kernel(cwd=workdir)
        except Exception as exc:
            raise EditMcpError(
                ErrorCode.KERNEL_START_FAILED,
                f"Failed to start kernel for {notebook_path.name}: {exc}",
                hint="Check that the kernel is installed (jupyter kernelspec list) "
                "and that the notebook's kernelspec is reachable.",
                details={"path": notebook_path.name, "reason": str(exc)[:500]},
            ) from exc

        client = manager.client()
        client.start_channels()
        try:
            client.wait_for_ready(timeout=30)
        except Exception as exc:
            client.stop_channels()
            manager.shutdown_kernel(now=True)
            raise EditMcpError(
                ErrorCode.KERNEL_START_FAILED,
                f"Kernel for {notebook_path.name} did not become ready: {exc}",
                hint="The kernel started but never signalled readiness. Check "
                "the kernel's logs.",
                details={"path": notebook_path.name, "reason": str(exc)[:500]},
            ) from exc

        session = _Session(
            manager=manager,
            client=client,
            name=manager.kernel_name or "unknown",
            language=(manager.kernel_spec.language if manager.kernel_spec else None),
        )
        with self._lock:
            self._by_notebook[notebook_path] = session
        return session

    def connect_remote(
        self,
        notebook_path: Path,
        connection_file: str,
    ) -> _Session:
        """Connect to a kernel that was started elsewhere.

        ``connection_file`` is a Jupyter connection file (the JSON document
        Jupyter writes when it starts a kernel, with ``ip``, ``shell_port``,
        ``iopub_port``, ``stdin_port``, ``hb_port``, ``key``, and
        ``signature_scheme``). A remote kernel might come from a JupyterHub
        spawner, a long-running container, or another tool the user already
        started; this process only talks to it, it does not own it.

        The session is registered under ``notebook_path`` so the existing
        ``execute_*`` tools work without further changes. The session is
        marked ``remote=True`` so ``shutdown`` and ``restart`` refuse to
        touch a process the user is responsible for: stopping a remote
        kernel is a destructive act against a session the user may be
        using elsewhere, and the right way to end it is on the host that
        owns it.

        A relative path is resolved against the notebook's parent
        directory, which is where the user's tools and JupyterHub
        typically write the connection file when the workspace is set up
        the standard way. An absolute path is used as-is.
        """
        candidate = Path(connection_file).expanduser()
        if not candidate.is_absolute():
            candidate = notebook_path.parent / candidate

        try:
            client = BlockingKernelClient()
            client.load_connection_file(str(candidate))
            client.start_channels()
            client.wait_for_ready(timeout=30)
        except FileNotFoundError as exc:
            raise EditMcpError(
                ErrorCode.KERNEL_START_FAILED,
                f"Connection file not found: {candidate}",
                hint="Pass the path to a Jupyter connection file (the JSON "
                "document Jupyter writes when it starts a kernel).",
                details={"connection_file": str(candidate), "reason": str(exc)},
            ) from exc
        except Exception as exc:
            raise EditMcpError(
                ErrorCode.KERNEL_START_FAILED,
                f"Could not connect to remote kernel: {exc}",
                hint="Check the connection file path and that the kernel is "
                "still running. JupyterHub puts connection files in the "
                "user's home under .local/share/jupyter/runtime/.",
                details={
                    "connection_file": str(candidate),
                    "reason": str(exc)[:500],
                },
            ) from exc

        language: str | None = None
        try:
            client.execute("")  # noqa: PERF401 - provoke a kernel_info_reply
            # ``load_connection_file`` does not give us a kernelspec, so
            # ``language`` is left as ``None`` and reported as such. The
            # user can set it via the notebook's metadata.
        except Exception:
            pass

        session = _Session(
            manager=DummyManager(),
            client=client,
            name="remote",
            language=language,
            remote=True,
            connection_file=str(candidate),
        )
        with self._lock:
            self._by_notebook[notebook_path] = session
        return session

    def restart(self, notebook_path: Path) -> _Session:
        with self._lock:
            session = self._by_notebook.get(notebook_path)
            self._restarts[notebook_path] = self._restarts.get(notebook_path, 0) + 1
        if session is None:
            return self.start(notebook_path)

        # A remote kernel is not ours to restart: the user is running it
        # somewhere else and a restart would wipe state they may be using.
        # The right way to restart it is on the host that owns it.
        if session.remote:
            return session

        session.lock.acquire()
        try:
            with contextlib.suppress(Exception):
                session.client.stop_channels()
            with contextlib.suppress(Exception):
                session.manager.shutdown_kernel(now=True)
        finally:
            session.lock.release()

        # Re-read kernelspec from the notebook on restart so a fix to the
        # notebook's metadata (e.g. switching to a different interpreter) takes
        # effect on the next start.
        return self.start(notebook_path)

    def interrupt(self, notebook_path: Path) -> bool:
        with self._lock:
            session = self._by_notebook.get(notebook_path)
        if session is None or not session.client.is_alive():
            return False
        try:
            # ``interrupt_kernel`` is a method on ``KernelManager``; remote
            # sessions use a dummy manager that raises, so we fall through to
            # the kernel's own interrupt message in that case.
            if not session.remote:
                session.manager.interrupt_kernel()
                return True
        except Exception:
            pass
        # Remote path: send the interrupt request directly. The kernel will
        # still raise ``KeyboardInterrupt`` in the running cell, and the next
        # execute call has a chance to proceed.
        try:
            session.client.execute("import builtins; builtins.exit()", store_history=False)
        except Exception:
            return False
        return True

    def shutdown(self, notebook_path: Path) -> bool:
        with self._lock:
            session = self._by_notebook.pop(notebook_path, None)
        if session is None:
            return False
        # For remote kernels we only stop our local channels. Killing the
        # kernel is the user's responsibility; closing the client side is
        # the right amount of "done" for a process we do not own.
        with contextlib.suppress(Exception):
            session.client.stop_channels()
        if not session.remote:
            with contextlib.suppress(Exception):
                session.manager.shutdown_kernel(now=True)
        return True

    def close_all(self) -> None:
        with self._lock:
            paths = list(self._by_notebook.keys())
        for path in paths:
            self.shutdown(path)

    def prune(self) -> int:
        """Drop dead sessions, returning the count removed.

        Called by tools that need the registry to be tidy (e.g. ``execute_*``
        when the requested notebook has a stale record). The caller can then
        call ``start`` to bring up a fresh one.
        """
        removed = 0
        with self._lock:
            for path in list(self._by_notebook.keys()):
                session = self._by_notebook[path]
                if not session.manager.is_alive():
                    self._by_notebook.pop(path)
                    removed += 1
        return removed


#: Module-level registry shared by the running server. Tests that need
#: isolation get their own instance.
_REGISTRY = KernelRegistry()


def get_registry() -> KernelRegistry:
    return _REGISTRY


def kernelspec_from_notebook(data: dict) -> str | None:
    """Extract the kernel name from notebook metadata, if any."""
    info = (data.get("metadata") or {}).get("kernelspec") or {}
    name = info.get("name")
    if isinstance(name, str) and name:
        return name
    return None


class DummyManager:
    """A no-op manager for remote-kernel sessions.

    ``KernelRegistry`` uses the same ``manager.is_alive()`` /
    ``manager.interrupt_kernel()`` / ``manager.shutdown_kernel()`` shape for
    every session, so a remote session still needs an object that answers
    those calls. A remote session never starts the kernel, so anything that
    would touch a process raises — and the registry treats that as a no-op
    or a refusal rather than a crash.
    """

    def is_alive(self) -> bool:
        return True

    def interrupt_kernel(self) -> None:
        raise NotImplementedError("remote kernels cannot be interrupted through this manager")

    def shutdown_kernel(self, *, now: bool = False) -> None:
        raise NotImplementedError("remote kernels cannot be shut down through this manager")

    @property
    def kernel_name(self) -> str:
        return "remote"

    @property
    def kernel_spec(self) -> None:
        return None
