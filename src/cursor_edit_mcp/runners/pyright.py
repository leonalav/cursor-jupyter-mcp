"""Pyright adapter.

Pyright reads ``pyrightconfig.json`` or ``pyproject.toml``'s ``[tool.pyright]``
section from the file's directory. That is the same resolution model Ruff
uses for ``pyproject.toml``, which is what the rest of this server expects:
the project owns its config and the server does not invent one.

Pyright does not read a single file from stdin with a custom filename the way
Ruff does, so the runner writes the candidate text to a sibling temp file,
runs Pyright on that, and remaps the reported file path back to the user's
``path``. The temp file lives in the same directory as ``path`` so
``pyrightconfig.json`` and any ``--project`` settings resolve exactly as they
would for the user's file. The temp file is deleted in a ``finally`` block so
a crash cannot leave it behind.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..validate.diagnostics import Diagnostic, Severity
from .base import CallSpec, Resolver, RunOutcome, ToolStatus, run

PYRIGHT = "pyright"

#: Pyright's severity levels, mapped to our ``Diagnostic.severity``.
_SEVERITY_MAP: dict[str, Severity] = {
    "error": "error",
    "warning": "warning",
    "information": "info",
}


@dataclass(frozen=True)
class CheckResult:
    diagnostics: tuple[Diagnostic, ...]
    outcome: RunOutcome
    tool_version: str | None = None

    @property
    def ran(self) -> bool:
        return self.outcome.ran

    @property
    def skipped_reason(self) -> str | None:
        return self.outcome.reason or self.outcome.error


def status(workspace_root: Path) -> ToolStatus:
    return Resolver(workspace_root).status(PYRIGHT)


def check(
    text: str,
    *,
    path: Path,
    resolver: Resolver,
    timeout: float = 30.0,
    level: str = "error",
) -> CheckResult:
    """Type-check ``text`` as if it were the file at ``path``.

    Pyright does not accept content on stdin with a custom filename, so the
    candidate text is written to a sibling temp file in the same directory.
    That preserves config resolution (``pyrightconfig.json`` and the
    ``[tool.pyright]`` block), and the runner reports the diagnostics
    against ``path`` rather than the temp name.

    ``level`` is Pyright's own: ``error`` reports only errors, ``warning``
    reports errors and warnings. The default of ``error`` matches the
    intuition of a type-check stage: it is for catching mistakes, not
    nit-picks.
    """
    if level not in ("error", "warning"):
        return CheckResult(
            diagnostics=(),
            outcome=RunOutcome(ran=False, reason=f"unknown pyright level {level!r}"),
        )

    tool = resolver.status(PYRIGHT)
    if not tool.available:
        return CheckResult(
            diagnostics=(),
            outcome=RunOutcome(ran=False, reason=tool.reason or "pyright is unavailable"),
            tool_version=tool.version,
        )

    # ``mkstemp`` returns an OS-level handle; the file is on disk the moment
    # the call returns, so Pyright can read it. Writing the candidate text
    # to a sibling rather than a temp dir keeps the project's config
    # resolution intact: ``pyrightconfig.json`` is found the same way it
    # would be for the user's file. The name is intentionally not dotfile-
    # prefixed: Pyright, like many Python tools, excludes hidden files from
    # analysis, which would silently swallow the diagnostics.
    tmp_handle, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f"_mcp_pyright_{path.stem}_",
        suffix=".py",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_handle, "w", encoding="utf-8") as handle:
            handle.write(text)

        outcome = run(
            CallSpec(
                argv=[
                    str(tool.executable),
                    "--outputjson",
                    f"--level={level}",
                    str(tmp_path),
                ],
                cwd=path.parent,
                timeout=timeout,
            )
        )
    finally:
        # Delete the temp file in every path. Leaving it would let Pyright's
        # cache re-create the file at its own path and confuse the user.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass

    if not outcome.ran or outcome.timed_out:
        return CheckResult(diagnostics=(), outcome=outcome, tool_version=tool.version)

    # Pyright echoes the file path it actually analyzed. The temp file
    # gets a randomized suffix, so we match on its basename, which is
    # unique inside ``path.parent``.
    diagnostics, error = _parse(outcome.stdout, tmp_path)
    return CheckResult(
        diagnostics=diagnostics,
        outcome=RunOutcome(
            ran=True,
            stdout=outcome.stdout,
            returncode=outcome.returncode,
            error=error,
            reason=outcome.reason,
        ),
        tool_version=tool.version,
    )


# --- internals ------------------------------------------------------------


def _parse(
    stdout: str, reported_path: Path
) -> tuple[tuple[Diagnostic, ...], str | None]:
    """Turn Pyright's JSON into diagnostics.

    Pyright reports the file path it actually checked (``reported_path``,
    which is the temp file we wrote). Every diagnostic's ``file`` field is
    compared by basename because Pyright may report absolute or relative
    paths depending on how it was invoked, and the temp file's basename
    is the only thing that is reliably unique. Diagnostics for any other
    file are dropped: they would be the result of an unrelated import
    and are not what was asked for.
    """
    stripped = (stdout or "").strip()
    if not stripped:
        return (), None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        # A malformed response is the tool's problem to report, not ours to
        # silently drop. The ``error`` field is what the call site surfaces.
        return (), f"pyright returned invalid JSON: {exc}"

    if not isinstance(payload, dict):
        return (), "pyright returned an unexpected payload shape"

    reported_name = reported_path.name

    out: list[Diagnostic] = []
    for entry in payload.get("generalDiagnostics") or []:
        if not isinstance(entry, dict):
            continue
        if Path(str(entry.get("file") or "")).name != reported_name:
            # Drop diagnostics for files we did not ask about. Pyright
            # follows imports in some modes, and we only care about the
            # file the user named.
            continue

        rng = entry.get("range") or {}
        start = rng.get("start") or {}
        end = rng.get("end") or {}
        line = int(start.get("line") or 0) + 1  # Pyright is 0-based; we are 1-based
        column = int(start.get("character") or 0) + 1
        end_line = int(end.get("line") or 0) + 1
        end_column = int(end.get("character") or 0) + 1
        code = str(entry.get("rule") or "unknown")
        message = str(entry.get("message") or "")
        severity = _SEVERITY_MAP.get(str(entry.get("severity") or "error").lower(), "error")

        out.append(
            Diagnostic(
                tool="pyright",
                code=code,
                message=message,
                severity=severity,
                line=line,
                column=column,
                end_line=end_line,
                end_column=end_column,
                source_line="",
                is_syntax=False,
            )
        )

    return tuple(out), None
