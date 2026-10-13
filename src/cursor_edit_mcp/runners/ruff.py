"""Ruff adapter.

Ruff covers Python syntax, lint, import sorting, and formatting through one
binary, and it resolves the project's own ``pyproject.toml`` / ``ruff.toml`` from
the file path we hand it. That is the whole reason the PRD picked it.

Content always goes in on stdin with ``--stdin-filename`` set to the real path,
so config resolution and ``per-file-ignores`` behave exactly as they would in an
editor, without writing a scratch file into the user's workspace.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..edit.anchors import LineIndex
from ..edit.document import detect_eol, to_eol, to_lf
from ..validate.diagnostics import Diagnostic, Severity
from .base import CallSpec, Resolver, RunOutcome, ToolStatus, run

RUFF = "ruff"

#: Ruff reports syntax errors under this pseudo-code.
SYNTAX_CODE = "invalid-syntax"


@dataclass(frozen=True)
class CheckResult:
    diagnostics: tuple[Diagnostic, ...]
    outcome: RunOutcome

    @property
    def ran(self) -> bool:
        return self.outcome.ran

    @property
    def skipped_reason(self) -> str | None:
        return self.outcome.reason or self.outcome.error


def status(workspace_root: Path) -> ToolStatus:
    return Resolver(workspace_root).status(RUFF)


def version(workspace_root: Path) -> str | None:
    return status(workspace_root).version


def check(
    text: str,
    *,
    path: Path,
    resolver: Resolver,
    timeout: float = 10.0,
    select: str | None = None,
) -> CheckResult:
    """Lint ``text`` as if it were the file at ``path``.

    Returns every diagnostic Ruff reports, including pre-existing ones. Deciding
    which ones are new is the pipeline's job.
    """
    tool = resolver.status(RUFF)
    if not tool.available:
        return CheckResult(diagnostics=(), outcome=_unavailable(tool.reason))

    argv = [
        str(tool.executable),
        "check",
        "--no-cache",
        "--output-format",
        "json",
        "--stdin-filename",
        str(path),
        # Never let Ruff's own exit code decide the outcome: Ruff exits 1 when
        # it finds diagnostics, which is a normal result, not a failure.
        "--exit-zero",
    ]
    if select:
        argv += ["--select", select]

    outcome = run(
        CallSpec(
            argv=argv,
            cwd=path.parent,
            stdin=text,
            timeout=timeout,
            env={"RUFF_CACHE_DIR": _cache_dir(path)},
        )
    )
    if not outcome.ran:
        return CheckResult(diagnostics=(), outcome=outcome)

    if outcome.timed_out:
        return CheckResult(diagnostics=(), outcome=outcome)

    return CheckResult(
        diagnostics=_parse(outcome.stdout, text),
        outcome=RunOutcome(
            ran=True,
            stdout=outcome.stdout,
            returncode=outcome.returncode,
            error=None
            if outcome.returncode in (0, 1)
            else outcome.stderr.strip() or None,
            reason=outcome.reason,
        ),
    )


def format_range(
    text: str,
    *,
    path: Path,
    start_line: int,
    end_line: int,
    resolver: Resolver,
    timeout: float = 10.0,
) -> tuple[str, RunOutcome]:
    """Format only the lines in ``[start_line, end_line]``.

    Ruff's ``--range`` keeps the rest of the file byte-identical, which is
    exactly the guarantee this server needs: an edit must not reformat code the
    agent never touched.
    """
    tool = resolver.status(RUFF)
    if not tool.available:
        return text, _unavailable(tool.reason)

    outcome = run(
        CallSpec(
            argv=[
                str(tool.executable),
                "format",
                "--no-cache",
                "--stdin-filename",
                str(path),
                "--range",
                f"{start_line}-{end_line}",
            ],
            cwd=path.parent,
            stdin=text,
            timeout=timeout,
            env={"RUFF_CACHE_DIR": _cache_dir(path)},
        )
    )
    if not outcome.usable:
        return text, outcome
    # Ruff echoes the formatted source on stdout; an empty echo means no change.
    return (outcome.stdout or text), outcome


def safe_fix(
    text: str,
    *,
    path: Path,
    resolver: Resolver,
    timeout: float = 10.0,
    allowed_lines: set[int] | None = None,
) -> tuple[str, tuple[Diagnostic, ...], RunOutcome]:
    """Apply Ruff's *safe* fixes, limited to ``allowed_lines``.

    Ruff's own ``--fix`` would rewrite the whole file, which is not acceptable
    for an autofix: it must not touch a line outside the edit's scope. So we ask
    Ruff what it *would* fix, take only the fix edits anchored on an allowed
    line, and apply those ourselves.

    ``allowed_lines`` is the edit's touched lines plus the import block; the
    caller computes it because "the import block" is a Python concept.
    """
    tool = resolver.status(RUFF)
    if not tool.available:
        return text, (), _unavailable(tool.reason)

    outcome = run(
        CallSpec(
            argv=[
                str(tool.executable),
                "check",
                "--no-cache",
                "--output-format",
                "json",
                "--stdin-filename",
                str(path),
                "--exit-zero",
            ],
            cwd=path.parent,
            stdin=text,
            timeout=timeout,
            env={"RUFF_CACHE_DIR": _cache_dir(path)},
        )
    )
    if not outcome.usable:
        return text, (), outcome

    payload = _load(outcome.stdout)
    diagnostics = _parse(outcome.stdout, text)

    edits = _collect_safe_edits(payload, allowed_lines)
    if not edits:
        return text, diagnostics, outcome

    fixed = _apply_edits(text, edits)
    return fixed, diagnostics, outcome


def _collect_safe_edits(
    payload: list[dict], allowed_lines: set[int] | None
) -> list[dict]:
    """Gather candidate safe, in-scope fix edits from Ruff's output.

    Applicability is checked, not inferred: only fixes Ruff itself marks
    ``safe`` are eligible. Display and unsafe fixes are never applied here.
    """
    out: list[dict] = []
    for entry in payload:
        fix = entry.get("fix") if isinstance(entry, dict) else None
        if not isinstance(fix, dict) or fix.get("applicability") != "safe":
            continue
        for edit in fix.get("edits") or []:
            if not isinstance(edit, dict):
                continue
            row = int((edit.get("location") or {}).get("row") or 0)
            if allowed_lines is not None and row not in allowed_lines:
                continue
            out.append(edit)
    return out


def _apply_edits(text: str, edits: list[dict]) -> str:
    """Apply non-overlapping fix edits, back-to-front.

    Ruff's fixes for one region are *alternatives*, not independent patches. A
    block of unsorted unused imports produces an I001 sort fix spanning the
    whole block plus an F401 deletion for each individual import, all covering
    the same characters. Applying them all corrupts the file. So the widest fix
    at each position wins, and any fix that would overlap an accepted one is
    dropped.

    Each fix's replacement text is re-ended to the file's own convention. Ruff's
    default ``line-ending = auto`` resolves to CRLF on Windows, so an
    unadjusted fix would splice CRLF into an LF file.
    """
    index = LineIndex(text)
    eol = detect_eol(text)
    spans: list[tuple[int, int, str]] = []

    for edit in edits:
        location = edit.get("location") or {}
        end = edit.get("end_location") or {}
        start = index.offset(
            int(location.get("row") or 1), int(location.get("column") or 1)
        )
        stop = index.offset(int(end.get("row") or 1), int(end.get("column") or 1))
        if stop <= start:
            continue
        content = to_eol(to_lf(str(edit.get("content") or "")), eol)
        spans.append((start, stop, content))

    # Earliest first; at the same start, the widest fix wins because it is the
    # one that owns the whole region.
    spans.sort(key=lambda span: (span[0], -span[1]))

    accepted: list[tuple[int, int, str]] = []
    covered_to = 0
    for start, stop, content in spans:
        if start < covered_to:
            continue
        accepted.append((start, stop, content))
        covered_to = stop

    out = text
    for start, stop, content in reversed(accepted):
        out = out[:start] + content + out[stop:]
    return out


def parse_errors_only(
    text: str, *, path: Path, resolver: Resolver, timeout: float = 10.0
) -> tuple[Diagnostic, ...]:
    """Only syntax errors, ignoring lint rules.

    The syntax stage runs before lint so a file that cannot be parsed does not
    generate a cascade of misleading lint output.
    """
    result = check(text, path=path, resolver=resolver, timeout=timeout)
    return tuple(d for d in result.diagnostics if d.code == SYNTAX_CODE)


# --- internals ------------------------------------------------------------


def _parse(stdout: str, text: str) -> tuple[Diagnostic, ...]:
    """Turn Ruff's JSON into diagnostics."""
    lines = text.splitlines()
    out: list[Diagnostic] = []
    for entry in _load(stdout):
        if not isinstance(entry, dict):
            continue
        location = entry.get("location") or {}
        end = entry.get("end_location") or {}
        row = int(location.get("row") or 1)
        code = str(entry.get("code") or "unknown")
        source = lines[row - 1] if 1 <= row <= len(lines) else ""

        out.append(
            Diagnostic(
                tool="ruff",
                code=code,
                message=str(entry.get("message") or ""),
                severity=_severity(str(entry.get("severity") or "error")),
                line=row,
                column=int(location.get("column") or 1),
                end_line=int(end.get("row") or row),
                end_column=int(end.get("column") or 0),
                source_line=source,
                is_syntax=code == SYNTAX_CODE,
            )
        )
    return tuple(out)


def _load(stdout: str) -> list[dict]:
    """Parse Ruff's JSON, tolerating empty or noisy output."""
    stripped = (stdout or "").strip()
    if not stripped:
        return []
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def _severity(value: str) -> Severity:
    mapping = {"error": "error", "warning": "warning", "info": "info"}
    return mapping.get(value, "error")  # type: ignore[return-value]


def _is_fixable(diagnostic: Diagnostic, only_lines: set[int] | None) -> bool:
    if only_lines is None:
        return True
    return diagnostic.line in only_lines


def _unavailable(reason: str | None) -> RunOutcome:
    return RunOutcome(ran=False, reason=reason or "ruff is unavailable")


def _cache_dir(path: Path) -> str:
    """Keep Ruff's cache out of the user's source tree."""
    return str(path.parent / ".ruff-cache-mcp")
