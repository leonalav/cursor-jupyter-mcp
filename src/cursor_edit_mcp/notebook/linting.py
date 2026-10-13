"""Linting notebook cells.

Ruff reads ``.ipynb`` natively and reports ``cell N:line:col``, which is almost
everything needed. Two things have to happen around it.

**Magics are not Python.** ``%matplotlib inline``, ``%%time``, and ``!pip
install x`` are IPython syntax. Left alone they are a syntax error on every
line of every cell that uses them, which buries the real problems. Each is
replaced with an equal-length comment so that columns stay aligned and line
numbers never move.

**A notebook's diagnostics need to come back in notebook coordinates.** A
report saying "line 47" is useless when the notebook has four cells of wildly
different lengths. Every diagnostic is mapped back to its `cell_id` and
cell-relative line.

Diagnostics are fingerprinted by cell *content*, not cell number, so inserting
a cell does not make the whole notebook look newly broken.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..runners import ruff
from ..runners.base import CallSpec, Resolver, run
from ..validate.diagnostics import Diagnostic, diff

#: Cell types that are never linted. Markdown is prose, raw is not code.
LINTABLE = ("code",)


@dataclass(frozen=True)
class CellDiagnostic:
    """A diagnostic in notebook coordinates."""

    cell_id: str
    cell_number: int
    line: int
    column: int
    code: str
    message: str
    severity: str
    source_line: str

    @property
    def is_syntax(self) -> bool:
        return self.code == ruff.SYNTAX_CODE

    def as_diagnostic(self) -> Diagnostic:
        """Convert for baseline diffing alongside file diagnostics."""
        return Diagnostic(
            tool="ruff",
            # The cell id is part of the fingerprint, so a diagnostic stays
            # attached to its cell even as cells are renumbered.
            code=self.code,
            message=self.message,
            severity=self.severity,  # type: ignore[arg-type]
            line=self.line,
            column=self.column,
            source_line=self.source_line,
            is_syntax=self.is_syntax,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "cell_id": self.cell_id,
            "cell_number": self.cell_number,
            "line": self.line,
            "column": self.column,
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "source_line": self.source_line,
        }


def neutralize_magics(source: str) -> tuple[str, bool]:
    """Comment out IPython magics in place, preserving positions.

    Replaces the magic with spaces and a trailing ``#``, so every character on
    the line keeps its column and the line keeps its number. Only the cell's own
    source is affected; the notebook is never modified by linting.
    """
    lines = source.split("\n")
    changed = False

    for index, line in enumerate(lines):
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]

        # Indentation is stripped before matching because an indented magic
        # inside a function body is still a magic to IPython. Commenting it out
        # too is deliberate: leaving it as a syntax error would make the whole
        # cell unlintable and hide every real problem in it.
        if not stripped.startswith(("%", "!")) or stripped.startswith("#"):
            continue

        # Overwrite the magic's own first character with '#' rather than
        # inserting a comment before it. Replacing keeps the line exactly as
        # long, so every remaining column still lines up and line numbers do
        # not move. It also avoids producing '#!', which linters read as a
        # misplaced shebang.
        lines[index] = f"{indent}#{stripped[1:]}"
        changed = True

    return "\n".join(lines), changed


def lint(
    data: dict,
    *,
    path: Path,
    resolver: Resolver,
    timeout: float = 10.0,
) -> tuple[CellDiagnostic, ...]:
    """Lint every code cell in a notebook structure."""
    cells = data.get("cells") or []
    if not any(isinstance(c, dict) and c.get("cell_type") in LINTABLE for c in cells):
        return ()

    tool = resolver.status(ruff.RUFF)
    if not tool.available:
        return ()

    # Ruff resolves the project's own config from this path, so keep it real
    # rather than inventing a scratch location.
    staged = _stage(cells, path)

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
            stdin=staged,
            timeout=timeout,
            env={"RUFF_CACHE_DIR": str(path.parent / ".ruff-cache-mcp")},
        )
    )
    if not outcome.ran or outcome.timed_out or outcome.error is not None:
        return ()

    return _parse(outcome.stdout, cells)


def diff_cells(
    before: tuple[CellDiagnostic, ...],
    after: tuple[CellDiagnostic, ...],
):
    """Diff notebook diagnostics on content, not position."""
    return diff(
        tuple(d.as_diagnostic() for d in before),
        tuple(d.as_diagnostic() for d in after),
    )


# --- internals ------------------------------------------------------------


def _stage(cells: list[dict], path: Path) -> str:
    """Build the notebook document Ruff will read.

    Every cell is staged, not just the code ones. Ruff numbers cells by their
    position in the document, so dropping the markdown and raw cells would
    renumber everything and every diagnostic would come back attached to the
    wrong cell.

    Magics are neutralized in code cells only. Markdown and raw cells are never
    linted by Ruff, so their source is passed through untouched.
    """
    staged_cells: list[dict] = []
    for cell in cells:
        copy = dict(cell)
        if copy.get("cell_type") in LINTABLE:
            text = "".join(cell.get("source") or [])
            copy["source"] = neutralize_magics(text)[0].splitlines(keepends=True)
        staged_cells.append(copy)

    return json.dumps(
        {
            "cells": staged_cells,
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        },
        indent=1,
    )


def _parse(stdout: str, cells: list[dict]) -> tuple[CellDiagnostic, ...]:
    """Map Ruff's notebook diagnostics back to cell ids."""
    stripped = (stdout or "").strip()
    if not stripped:
        return ()

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return ()

    if not isinstance(payload, list):
        return ()

    out: list[CellDiagnostic] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue

        cell_number = entry.get("cell")
        # ``cell`` is present only for notebooks, and is 1-based over *all*
        # cells, which is exactly nb_outline's numbering.
        if not isinstance(cell_number, int) or not 1 <= cell_number <= len(cells):
            continue

        cell = cells[cell_number - 1]
        if not isinstance(cell, dict) or cell.get("cell_type") not in LINTABLE:
            continue

        location = entry.get("location") or {}
        row = int(location.get("row") or 1)
        source_lines = "".join(cell.get("source") or []).split("\n")

        out.append(
            CellDiagnostic(
                cell_id=str(cell.get("id") or ""),
                cell_number=cell_number,
                line=row,
                column=int(location.get("column") or 1),
                code=str(entry.get("code") or "unknown"),
                message=str(entry.get("message") or ""),
                severity=str(entry.get("severity") or "error"),
                source_line=(
                    source_lines[row - 1] if 1 <= row <= len(source_lines) else ""
                ),
            )
        )

    return tuple(out)
