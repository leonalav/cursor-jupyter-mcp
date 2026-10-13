"""Linting percent-format cells.

A percent script is text on disk and a list of cells in memory. The
notebook linter runs on a virtual notebook, so the same trick works here:
stage the cells into a notebook document, run Ruff with ``--stdin-
filename`` so its config resolution matches the user's, then map
diagnostics back to ``cell_id`` and cell-relative line.

The virtual notebook uses the script's path as the ``--stdin-filename``,
so Ruff reads the same ``pyproject.toml`` it would for the script itself.
This is what makes the lint match what a developer would see in their
editor with format-on-save turned on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..runners import ruff
from ..runners.base import CallSpec, Resolver, run
from .script import Script, source_text


@dataclass(frozen=True)
class CellDiagnostic:
    """A diagnostic in script coordinates."""

    cell_id: str
    cell_number: int
    line: int
    column: int
    code: str
    message: str
    severity: str
    source_line: str

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


def _neutralize_magics(text: str) -> str:
    """Comment out IPython magics while keeping line numbers stable.

    The notebook linter does the same trick for the same reason: a cell
    that starts with ``%matplotlib inline`` would be a syntax error to
    Python, and that error would mask every real problem in the cell.
    The comment is the same length as the magic, so columns stay
    aligned and any line number that survives is still right.
    """
    lines = text.split("\n")
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped or not stripped.startswith(("%", "!")) or stripped.startswith("#"):
            continue
        indent = line[: len(line) - len(stripped)]
        lines[index] = f"{indent}#{stripped[1:]}"
    return "\n".join(lines)


def lint(
    script: Script,
    *,
    path: Path,
    resolver: Resolver,
    timeout: float = 10.0,
) -> tuple[CellDiagnostic, ...]:
    """Run Ruff on every code cell, mapped back to ``cell_id``."""
    code_cells = [c for c in script.cells if isinstance(c, dict) and c.get("cell_type") == "code"]
    if not code_cells:
        return ()

    tool = resolver.status(ruff.RUFF)
    if not tool.available:
        return ()

    # Build a virtual notebook: each cell is staged with magics neutralised.
    # Cell numbers from the virtual notebook match the script's 1-based cell
    # numbers because the staging preserves the order and only emits code
    # cells (which the linter can read), so a diagnostic at cell N lines up
    # with the script's cell N.
    virtual = {
        "cells": [
            {
                "cell_type": "code",
                "id": cell.get("id", ""),
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": _neutralize_magics(source_text(cell.get("source"))).splitlines(
                    keepends=True
                ),
            }
            for cell in code_cells
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }

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
            stdin=json.dumps(virtual, indent=1),
            timeout=timeout,
            env={"RUFF_CACHE_DIR": str(path.parent / ".ruff-cache-mcp")},
        )
    )
    if not outcome.ran or outcome.timed_out or outcome.error is not None:
        return ()

    return _parse(outcome.stdout, code_cells)


def _parse(stdout: str, code_cells: list[dict]) -> tuple[CellDiagnostic, ...]:
    """Map Ruff's notebook diagnostics back to script ``cell_id`` values."""
    stripped = (stdout or "").strip()
    if not stripped:
        return ()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return ()
    if not isinstance(payload, list):
        return ()

    # Build a 1-based mapping from virtual-cell-number to script-cell-number.
    # The virtual document only contains code cells, so cell 1 in Ruff's
    # output is the first code cell in the script. Markdown and raw cells
    # are skipped in staging and never produce diagnostics, so the offset is
    # always zero for the first code cell and increases by one for each
    # subsequent code cell.
    code_positions: list[int] = []
    for index, cell in enumerate(script_cells(code_cells), start=1):
        # ``index`` is the 1-based position among the script's cells.
        code_positions.append(index)

    out: list[CellDiagnostic] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        cell_number = entry.get("cell")
        if not isinstance(cell_number, int) or not 1 <= cell_number <= len(code_cells):
            continue

        # The script's cell number for the Nth code cell. Diagnostics are
        # attached to this number so ``nb_outline``'s numbering matches.
        try:
            script_cell_number = code_positions[cell_number - 1]
        except IndexError:
            continue
        cell = code_cells[cell_number - 1]
        if not isinstance(cell, dict):
            continue

        location = entry.get("location") or {}
        row = int(location.get("row") or 1)
        source_lines = source_text(cell.get("source")).split("\n")

        out.append(
            CellDiagnostic(
                cell_id=str(cell.get("id") or ""),
                cell_number=script_cell_number,
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


def script_cells(code_cells: list[dict]) -> list[dict]:
    """Placeholder kept for symmetry with the notebook linter.

    The linter is fed a flat list of code cells, not the script, so this
    helper exists only to make the ``_parse`` call site read the way the
    notebook engine reads. It returns its input unchanged.
    """
    return code_cells
