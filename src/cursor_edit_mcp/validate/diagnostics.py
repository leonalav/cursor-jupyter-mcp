"""Diagnostics and baseline diffing.

The point of this module is one sentence: **report the problems the edit
introduced, and stay quiet about the ones that were already there.**

A project with existing lint debt should not be uneditable. Diffing before
against after, and reporting only what is genuinely new, is what makes a
validator usable on a real codebase rather than only on a clean one.

Fingerprints are deliberately insensitive to line numbers. An edit at line 10
shifts everything below it, and a diagnostic that merely moved down must not be
reported as a new problem.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from typing import Literal

Severity = Literal["error", "warning", "info"]

#: Runs of whitespace collapse to one space before fingerprinting.
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Diagnostic:
    """One problem, normalized across tools."""

    tool: str
    code: str
    message: str
    severity: Severity
    line: int
    column: int = 1
    end_line: int | None = None
    end_column: int | None = None
    source_line: str = ""
    is_syntax: bool = False

    @property
    def normalized_source(self) -> str:
        return _WHITESPACE.sub(" ", self.source_line).strip()

    @property
    def fingerprint(self) -> str:
        """Identity of this problem, independent of where it now sits.

        Combines rule, message, and the normalized source text. Two runs of the
        same rule on the same code are the same finding; the same rule on moved
        code is still the same finding.
        """
        parts = f"{self.tool}|{self.code}|{self.message}|{self.normalized_source}"
        return hashlib.sha1(parts.encode("utf-8")).hexdigest()[:16]  # noqa: S324

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def as_dict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "tool": self.tool,
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "line": self.line,
            "column": self.column,
        }
        if self.source_line.strip():
            body["source_line"] = self.source_line.strip()[:200]
        return body


@dataclass(frozen=True)
class BaselineDiff:
    """What changed between two diagnostic sets."""

    new: tuple[Diagnostic, ...]
    preexisting_count: int
    resolved_count: int

    @property
    def new_errors(self) -> tuple[Diagnostic, ...]:
        return tuple(d for d in self.new if d.is_error)

    @property
    def has_new_errors(self) -> bool:
        return bool(self.new_errors)

    def as_dict(self, *, cap: int = 20) -> dict[str, object]:
        """Compact report. Diagnostics are capped so a bad edit cannot flood
        the agent's context; the count always tells the truth."""
        return {
            "new": [d.as_dict() for d in self.new[:cap]],
            "new_count": len(self.new),
            "new_error_count": len(self.new_errors),
            "preexisting_count": self.preexisting_count,
            "resolved_count": self.resolved_count,
            "truncated": len(self.new) > cap,
        }


def diff(before: tuple[Diagnostic, ...], after: tuple[Diagnostic, ...]) -> BaselineDiff:
    """Compare two diagnostic sets.

    Matching is by fingerprint, with multiplicity: two identical findings before
    and one after counts as one resolved, not as a new finding.
    """
    before_counts = Counter(d.fingerprint for d in before)
    after_counts = Counter(d.fingerprint for d in after)

    new_fingerprints = after_counts - before_counts
    resolved_count = sum((before_counts - after_counts).values())

    remaining = dict(new_fingerprints)
    new: list[Diagnostic] = []
    for diagnostic in after:
        count = remaining.get(diagnostic.fingerprint, 0)
        if count > 0:
            new.append(diagnostic)
            remaining[diagnostic.fingerprint] = count - 1

    return BaselineDiff(
        new=tuple(new),
        preexisting_count=len(before) - sum((before_counts - after_counts).values()),
        resolved_count=resolved_count,
    )


@dataclass(frozen=True)
class StageResult:
    """One pipeline stage's outcome."""

    name: str
    status: Literal["ok", "skipped", "failed"]
    reason: str | None = None
    diagnostics: tuple[Diagnostic, ...] = ()

    def as_dict(self) -> dict[str, object]:
        body: dict[str, object] = {"stage": self.name, "status": self.status}
        if self.reason:
            body["reason"] = self.reason
        return body


def stage_ok(name: str) -> StageResult:
    return StageResult(name=name, status="ok")


def stage_skipped(name: str, reason: str) -> StageResult:
    return StageResult(name=name, status="skipped", reason=reason)


def stage_failed(name: str, diagnostics: tuple[Diagnostic, ...]) -> StageResult:
    return StageResult(name=name, status="failed", diagnostics=diagnostics)
