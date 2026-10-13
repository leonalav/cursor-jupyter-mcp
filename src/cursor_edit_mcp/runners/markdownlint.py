"""markdownlint-cli2 adapter.

Only runs when the project has a markdownlint config. Without one, the tool's
default rule set would fire on every document in the workspace and the agent
would spend its turn chasing style opinions nobody asked for.

Neither markdownlint nor Prettier can be given a line range, so the pipeline
applies their whole-file output through hunk filtering: format the whole
document, then keep only the hunks that overlap the lines the edit touched.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from pathlib import Path

from ..validate.diagnostics import Diagnostic
from .base import CallSpec, Resolver, RunOutcome, ToolStatus, run

MARKDOWNLINT = "markdownlint-cli2"

#: Exit code markdownlint-cli2 uses when it found problems.
_HAS_ISSUES = 1


@dataclass(frozen=True)
class LintResult:
    diagnostics: tuple[Diagnostic, ...]
    outcome: RunOutcome


def status(workspace_root: Path) -> ToolStatus:
    return Resolver(workspace_root).status(MARKDOWNLINT, ["--version"])


def lint(
    text: str,
    *,
    path: Path,
    resolver: Resolver,
    config_dir: Path | None = None,
    timeout: float = 15.0,
) -> LintResult:
    """Lint ``text`` as if it were the file at ``path``.

    markdownlint-cli2 does not read stdin, so the content is staged in a
    temporary file that carries the real name inside the config directory. That
    keeps the tool's own config resolution working, which is the whole point of
    deferring to it.
    """
    tool = resolver.status(MARKDOWNLINT, ["--version"])
    if not tool.available:
        return LintResult((), _unavailable(tool.reason))

    directory = config_dir or path.parent
    staged = directory / f".{path.name}.mcp-lint.md"

    try:
        staged.write_text(text, encoding="utf-8")
    except OSError as exc:
        return LintResult((), RunOutcome(ran=False, error=f"could not stage: {exc}"))

    try:
        outcome = run(
            CallSpec(
                argv=[
                    str(tool.executable),
                    str(staged.name),
                    "--json",
                    "--no-globs",
                ],
                cwd=directory,
                timeout=timeout,
            )
        )
        return LintResult(_parse(outcome.stdout, text), outcome)
    finally:
        # Best effort: a leftover staging file is untidy, not worth failing over.
        with contextlib.suppress(OSError):
            staged.unlink(missing_ok=True)


def _parse(stdout: str, text: str) -> tuple[Diagnostic, ...]:
    stripped = (stdout or "").strip()
    if not stripped:
        return ()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return ()

    lines = text.split("\n")
    out: list[Diagnostic] = []
    for entry in payload if isinstance(payload, list) else []:
        for name in entry.get("files") or []:
            for item in name.get("errorContext") or [{}]:
                line_number = int(item.get("lineNumber") or 1)
                rule = item.get("ruleNames") or ["?"]
                out.append(
                    Diagnostic(
                        tool="markdownlint",
                        code=f"MD-{rule[0]}",
                        message=str(item.get("ruleDescription") or "Markdown rule"),
                        severity=_severity(str(item.get("errorSeverity") or "ERROR")),
                        line=line_number,
                        column=_column_of(item.get("errorRange")),
                        source_line=(
                            lines[line_number - 1]
                            if 1 <= line_number <= len(lines)
                            else ""
                        ),
                    )
                )
    return tuple(out)


def _column_of(error_range) -> int:
    """First column of a markdownlint error range, or 1."""
    if isinstance(error_range, list) and error_range:
        return int(error_range[0])
    return 1


def _severity(value: str) -> str:
    return {"ERROR": "error", "WARNING": "warning", "INFO": "info"}.get(
        value.upper(), "error"
    )


def _unavailable(reason: str | None) -> RunOutcome:
    return RunOutcome(ran=False, reason=reason or "markdownlint-cli2 is unavailable")
