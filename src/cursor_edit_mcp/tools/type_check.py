"""``type_check``: opt-in type checking with ``pyright`` or ``mypy``.

Type checking is a separate concern from the validation pipeline. Ruff's
syntax + lint stage is fast and runs on every edit; a real type checker is
slow and noisy on a project that has not opted in. The M6 PRD decision
("Type-checking: ``pyright`` and ``mypy`` are P2, not in v1, because of
latency.") is what kept the pipeline clean: this tool is the place to
reach for type checking when the user actually asks.

**Tool selection.** ``tool: "auto"`` (the default) tries Pyright first and
falls back to mypy when Pyright is not installed. Pyright is faster and
its output is closer to what an editor would show, so it wins on a
developer machine; mypy is the more conservative choice for a CI
environment where the project's own config is set up for it. ``tool:
"pyright"`` or ``tool: "mypy"`` is an explicit override.

**Proposed content.** Passing ``content`` checks that text as if it were
the file. The check runs on a sibling temp file in the same directory so
the project's own ``pyrightconfig.json`` / ``[tool.mypy]`` / ``mypy.ini``
is found exactly the way it would be for the user's file. The temp file
is deleted when the run ends, even on a crash.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..edit.document import read_bytes
from ..errors import EditMcpError, ErrorCode
from ..logging_setup import get_logger
from ..runners import mypy, pyright
from ..runners.base import Resolver
from ..security.paths import assert_mutable, resolve_path
from ..settings import Settings, get_settings

_LOGGER = get_logger(__name__)

TYPE_CHECK_DESCRIPTION = """\
Run a type checker on a Python file, or on a block of proposed content.

Use this when the question is "does this type-check?" — for example, after
editing a function signature, when adding or changing type hints, or before
claiming a refactor is safe. The validation pipeline covers syntax and lint
on every edit; type checking is opt-in because it is slower and noisier on
a project that has not configured for it.

`tool` picks the checker: `pyright` (default, fast), `mypy`, or `auto`
(try pyright, fall back to mypy). `level` is the minimum diagnostic
level: `error` (default) reports only errors, `warning` reports errors
and warnings. `content` validates the proposed text as if it were the
file's contents — the same pattern `validate_file` uses.

The tool is read-only: it never writes to the file, and the temp file
Pyright and mypy need is created and removed in the same call. The
result reports the version of the tool that ran, so a `pyright` result
is never mistaken for a `mypy` result."""


@dataclass(frozen=True)
class _ToolChoice:
    name: str  # "pyright" or "mypy"
    version: str | None
    result: Any  # CheckResult


def _run_check(
    *,
    text: str,
    path: Path,
    resolver: Resolver,
    tool: str,
    level: str,
    timeout: float,
) -> _ToolChoice:
    """Dispatch to Pyright or mypy and return the first that produced a result.

    The "auto" path tries Pyright first, then mypy. A tool is "available" only
    when its executable is on ``PATH``; a missing tool is reported as
    ``skipped`` with the reason and the runner moves on. Both tools are tried
    so the agent gets an answer when at least one is installed.
    """
    if tool == "pyright":
        return _check_pyright(text, path, resolver, level, timeout)
    if tool == "mypy":
        return _check_mypy(text, path, resolver, timeout)
    if tool == "auto":
        first = _check_pyright(text, path, resolver, level, timeout)
        if first.result.outcome.ran:
            return first
        # Pyright missing or broke; try mypy. Mypy's outcome carries the
        # reason when it cannot run, so the user can see both.
        second = _check_mypy(text, path, resolver, timeout)
        if second.result.outcome.ran:
            return second
        # Neither tool ran: return Pyright's reason because that is the
        # default and the user is more likely to install it.
        return first
    raise EditMcpError(
        ErrorCode.INVALID_EDIT,
        f"Unknown type checker {tool!r}.",
        hint="Use 'auto', 'pyright', or 'mypy'.",
        details={"tool": tool, "valid": ["auto", "pyright", "mypy"]},
    )


def _check_pyright(
    text: str, path: Path, resolver: Resolver, level: str, timeout: float
) -> _ToolChoice:
    result = pyright.check(
        text, path=path, resolver=resolver, timeout=timeout, level=level
    )
    return _ToolChoice(name="pyright", version=result.tool_version, result=result)


def _check_mypy(text: str, path: Path, resolver: Resolver, timeout: float) -> _ToolChoice:
    result = mypy.check(text, path=path, resolver=resolver, timeout=timeout)
    return _ToolChoice(name="mypy", version=result.tool_version, result=result)


def type_check(
    path: str,
    tool: str = "auto",
    level: str = "error",
    content: str | None = None,
    timeout_s: float = 30.0,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Type-check ``path`` (or the proposed ``content``) and return diagnostics."""
    settings = settings or get_settings()
    resolved = resolve_path(path, settings)
    assert_mutable(resolved, path, settings)

    if content is not None:
        subject = content
        origin = "proposed"
    else:
        data = read_bytes(resolved, path, settings.max_file_bytes)
        subject = data.decode("utf-8", errors="replace")
        origin = "file"

    resolver = Resolver(settings.workspace_root)
    choice = _run_check(
        text=subject,
        path=resolved,
        resolver=resolver,
        tool=tool,
        level=level,
        timeout=timeout_s,
    )
    diagnostics = choice.result.diagnostics
    outcome = choice.result.outcome

    body: dict[str, Any] = {
        "ok": not any(d.is_error for d in diagnostics),
        "path": path,
        "validated": origin,
        "tool": choice.name,
        "tool_version": choice.version,
        "level": level,
        "diagnostics": {
            "new": [d.as_dict() for d in diagnostics[:50]],
            "new_count": len(diagnostics),
            "new_error_count": sum(1 for d in diagnostics if d.is_error),
            "truncated": len(diagnostics) > 50,
        },
    }
    if not outcome.ran:
        body["skipped"] = True
        body["reason"] = outcome.reason or "type checker is unavailable"
    if outcome.timed_out:
        body["timed_out"] = True
        body["reason"] = f"type checker did not complete within {timeout_s:.0f}s"

    _LOGGER.info(
        "type_check",
        extra={
            "context": {
                "path": resolved.as_posix(),
                "tool": choice.name,
                "level": level,
                "validated": origin,
                "ran": outcome.ran,
                "diagnostic_count": len(diagnostics),
            }
        },
    )
    return body


__all__ = ["TYPE_CHECK_DESCRIPTION", "type_check"]
