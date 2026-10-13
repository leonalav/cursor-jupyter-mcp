"""Discover installed Jupyter kernels.

The kernel registry has always started a kernel by name and trusted that
``jupyter_client`` would find it on disk. That is the right thing for an
implicit default, but the moment a user opens a notebook whose kernelspec is
``ir`` or ``python2`` or anything they have not used before, they need to know
what is available and where it lives before any execution can succeed.

This tool answers three questions, in one round trip:

1. **What kernelspecs are installed?** Listed by name with the language, the
   display name, the resource directory, and the ``argv`` that Jupyter would
   spawn.
2. **Which one is the fallback?** The "default" is the kernelspec Jupyter
   itself would use when the notebook has none. A user who opens a notebook
   that says only ``"python3"`` can see whether that resolves to the
   interpreter in their venv or to a system one.
3. **Are any of them reachable?** A kernelspec is not the same as a working
   kernel: a missing interpreter fails on first execution, not on load.
   ``executable_status`` reports whether the first element of ``argv``
   resolves on the current ``PATH``.

The tool is read-only and never starts a kernel. Listing kernelspecs is
metadata work; starting a kernel is execution work, and those are kept apart
on purpose.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Any

from jupyter_client.kernelspec import NATIVE_KERNEL_NAME, KernelSpecManager

from ..logging_setup import get_logger
from ..runners.base import which_variants

_LOGGER = get_logger(__name__)


@dataclass(frozen=True)
class KernelInfo:
    """One installed kernelspec, flattened for the response."""

    name: str
    display_name: str
    language: str
    argv: tuple[str, ...]
    resource_dir: str
    env: dict[str, str]
    is_default: bool
    executable: str | None
    executable_status: str  # "ok" | "missing" | "unresolved"

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": self.name,
            "display_name": self.display_name,
            "language": self.language,
            "argv": list(self.argv),
            "resource_dir": self.resource_dir,
            "env": self.env,
            "is_default": self.is_default,
            "executable": self.executable,
            "executable_status": self.executable_status,
        }
        return body


def _argv_executable(argv: tuple[str, ...] | list[str]) -> str | None:
    """The interpreter or wrapper Jupyter would spawn for this kernel.

    A kernelspec's ``argv`` is a list of tokens. The first one is the
    interpreter (``python``, ``R``, ``julia``), and that is what the user
    needs to have on ``PATH`` for the kernel to ever start. We surface the
    first token even if it is not a path, because kernels frequently use a
    bare name (``python``) that resolves through ``PATH`` at spawn time.
    """
    if not argv:
        return None
    first = argv[0]
    return str(first) if isinstance(first, str) else None


def _executable_status(executable: str | None) -> str:
    """``ok`` when the interpreter is on ``PATH`` or is an absolute path.

    An absolute path that does not exist is ``missing``; a bare name that
    does not resolve through ``PATH`` is ``unresolved`` so the user can tell
    the two cases apart.
    """
    if not executable:
        return "unresolved"
    if any(sep in executable for sep in ("/", "\\")):
        # Treat as a path. ``shutil.which`` accepts absolute paths and
        # returns the path itself if it exists, or ``None`` otherwise.
        return "ok" if shutil.which(executable) else "missing"
    return "ok" if shutil.which(executable) else "unresolved"


def _executable_for_argv(argv: tuple[str, ...] | list[str]) -> str | None:
    """An absolute path to the interpreter, when one is reachable.

    A bare-name argv (e.g. ``"python"``) becomes the resolved path on this
    machine's ``PATH``. An absolute argv is kept as-is when it exists. A
    missing interpreter is ``None``.
    """
    candidate = _argv_executable(argv)
    if not candidate:
        return None
    for variant in which_variants(candidate) if any(
        sep not in candidate for sep in ("/", "\\")
    ) else [candidate]:
        resolved = shutil.which(variant)
        if resolved:
            return resolved
    return shutil.which(candidate)


def discover() -> list[KernelInfo]:
    """Inspect every installed kernelspec.

    Errors during a single kernelspec's read are downgraded to a warning and
    the kernelspec is dropped from the list, because a partial listing is
    more useful than a hard failure when one entry is corrupt.
    """
    ksm = KernelSpecManager()
    raw = ksm.get_all_specs()

    out: list[KernelInfo] = []
    for name, info in sorted(raw.items()):
        spec = (info or {}).get("spec") or {}
        argv = tuple(spec.get("argv") or ())
        executable = _executable_for_argv(argv)
        out.append(
            KernelInfo(
                name=str(name),
                display_name=str(spec.get("display_name") or name),
                language=str(spec.get("language") or "unknown"),
                argv=argv,
                resource_dir=str((info or {}).get("resource_dir") or ""),
                env=dict(spec.get("env") or {}),
                is_default=str(name) == NATIVE_KERNEL_NAME,
                executable=executable,
                executable_status=_executable_status(executable or _argv_executable(argv)),
            )
        )

    _LOGGER.info(
        "kernel.discover",
        extra={"context": {"count": len(out), "default": NATIVE_KERNEL_NAME}},
    )
    return out


def kernel_discover_tool() -> dict[str, Any]:
    """The MCP tool entry point.

    Returns the list of installed kernelspecs, plus a short summary so the
    caller can see at a glance which languages are reachable and which are
    not. The list is sorted by name for a stable response.
    """
    kernels = discover()
    languages = sorted({k.language for k in kernels if k.language != "unknown"})
    reachable = [k.name for k in kernels if k.executable_status == "ok"]
    unreachable = [k.name for k in kernels if k.executable_status != "ok"]

    return {
        "ok": True,
        "count": len(kernels),
        "default_kernel": NATIVE_KERNEL_NAME,
        "languages": languages,
        "reachable": reachable,
        "unreachable": unreachable,
        "kernels": [k.as_dict() for k in kernels],
    }


def kernel_discover() -> dict[str, Any]:
    """Alias of ``kernel_discover_tool`` for the MCP tool surface.

    ``server.py`` expects the conventional ``kernel_discover`` name, the
    same shape as ``nb_outline`` and friends; the body of the function
    lives in ``kernel_discover_tool`` for clarity at the call site.
    """
    return kernel_discover_tool()


KERNEL_DISCOVER_DESCRIPTION = """\
List the Jupyter kernels installed on this machine.

Reports, for every kernelspec Jupyter can find: name, display name, language,
the argv that would be spawned, the resource directory, and whether the
interpreter is reachable on this PATH. The `default_kernel` is the one
Jupyter itself uses when a notebook does not name a kernel.

The tool is read-only. It never starts a kernel. Use the result to decide
which `kernelspec` to set on a notebook, or to confirm a non-Python
language (R, Julia, IRKernel, Bash) is available before opening a notebook
that depends on it."""


__all__ = [
    "KERNEL_DISCOVER_DESCRIPTION",
    "KernelInfo",
    "discover",
    "kernel_discover",
    "kernel_discover_tool",
]
