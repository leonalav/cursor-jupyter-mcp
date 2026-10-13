"""mypy adapter.

mypy reads ``mypy.ini``, ``setup.cfg``, or ``pyproject.toml``'s
``[tool.mypy]`` section from the file's directory. The config-resolve model
is the same as Pyright's: a sibling temp file in the same directory as the
user's file means ``mypy`` finds the project's config the same way it would
for the original.

mypy emits one JSON object per diagnostic, line-delimited, when called with
``--output json``. The runner parses each line and remaps the reported path
back to the user's ``path``.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..validate.diagnostics import Diagnostic, Severity
from .base import CallSpec, Resolver, RunOutcome, ToolStatus, run

MYPY = "mypy"

#: mypy's severity levels, mapped to our ``Diagnostic.severity``.
_SEVERITY_MAP: dict[str, Severity] = {
    "error": "error",
    "warning": "warning",
    "note": "info",
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
    return Resolver(workspace_root).status(MYPY)


def check(
    text: str,
    *,
    path: Path,
    resolver: Resolver,
    timeout: float = 30.0,
) -> CheckResult:
    """Type-check ``text`` as if it were the file at ``path``.

    ``--no-incremental`` keeps the run hermetic: mypy's incremental cache
    would otherwise leak findings from a previous invocation of the same
    file, and the agent expects a single, predictable run. The cache also
    writes to ``.mypy_cache`` in the project root, which is the kind of
    side effect the runner's contract says to avoid.
    """
    tool = resolver.status(MYPY)
    if not tool.available:
        return CheckResult(
            diagnostics=(),
            outcome=RunOutcome(ran=False, reason=tool.reason or "mypy is unavailable"),
            tool_version=tool.version,
        )

    # ``mkstemp`` returns an OS-level handle; the file is on disk the moment
    # the call returns, so mypy can read it. Writing the candidate text to
    # a sibling rather than a temp dir keeps the project's config
    # resolution intact: ``mypy.ini`` is found the same way it would be
    # for the user's file. The name is intentionally not dotfile-prefixed:
    # mypy, like Pyright and many other Python tools, excludes hidden
    # files from analysis, which would silently swallow the diagnostics.
    tmp_handle, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f"_mcp_mypy_{path.stem}_",
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
                    "--no-incremental",
                    "--no-color-output",
                    "--no-error-summary",
                    "--output",
                    "json",
                    str(tmp_path),
                ],
                cwd=path.parent,
                timeout=timeout,
            )
        )
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass

    if not outcome.ran or outcome.timed_out:
        return CheckResult(diagnostics=(), outcome=outcome, tool_version=tool.version)

    # mypy emits the file as it was given to the CLI, which is the absolute
    # path. Its JSON output, however, may keep that absolute or simplify to
    # the basename depending on the run. Match on the basename, which is
    # what uniquely identifies the temp file inside ``path.parent``.
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
    """Parse mypy's line-delimited JSON.

    Each line is one JSON object, so a single malformed line does not
    invalidate the rest. The path reported by mypy (``reported_path``)
    identifies the temp file we wrote; only diagnostics matching that file
    are kept, and they are surfaced under ``target_path`` because that is
    what the user asked about. The match is on the basename: mypy can
    return either absolute or relative paths depending on how it was
    invoked, but the basename of the temp file is unique inside
    ``path.parent``.
    """
    stripped = (stdout or "").strip()
    if not stripped:
        return (), None

    reported_name = reported_path.name

    out: list[Diagnostic] = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # One bad line is not the whole run. Skip and keep going.
            continue
        if not isinstance(entry, dict):
            continue
        entry_path = Path(str(entry.get("file") or ""))
        if entry_path.name != reported_name:
            continue

        severity = _SEVERITY_MAP.get(
            str(entry.get("severity") or "error").lower(), "error"
        )
        code = str(entry.get("code") or "unknown")
        out.append(
            Diagnostic(
                tool="mypy",
                code=code,
                message=str(entry.get("message") or ""),
                severity=severity,
                line=int(entry.get("line") or 1),
                column=int(entry.get("column") or 1),
                end_line=(
                    int(entry["end_line"])
                    if isinstance(entry.get("end_line"), int)
                    else None
                ),
                end_column=(
                    int(entry["end_column"])
                    if isinstance(entry.get("end_column"), int)
                    else None
                ),
                source_line="",
                is_syntax=False,
            )
        )

    return tuple(out), None
