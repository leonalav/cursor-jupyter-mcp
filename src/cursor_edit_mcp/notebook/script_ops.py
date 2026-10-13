"""Cell operations on percent-format scripts.

A ``Script`` exposes the same ``cells`` shape the notebook engine uses:
each cell is a dict with ``cell_type``, ``id``, ``source`` (list of lines),
and ``metadata``. So the operations here are written against the same
abstraction the notebook engine exposes, and the only difference is in
how the result is serialised.

Addressing is identical: ``cell_id`` is stable, ``cell_number`` is the
1-based position among all cells including markdown, and a number is
rejected when the file has fewer cells than that. Insert and move use
0-based positions the same way the notebook engine does.

Persistence differs: percent scripts have no outputs, so the only thing
that ever changes is the cell's source, the cell's type, and the order
of cells. The session writes the file through the same atomic writer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import EditMcpError, ErrorCode
from . import script as script_engine
from .engine import source_text as _engine_source_text  # noqa: F401  (re-export)
from .script import ID_LENGTH, Script, generate_cell_id, source_text, to_source_list


@dataclass(frozen=True)
class CellRef:
    """A resolved cell, with both ways of naming it."""

    cell_id: str
    cell_number: int
    cell_type: str
    index: int

    def as_dict(self) -> dict[str, object]:
        return {
            "cell_number": self.cell_number,
            "cell_id": self.cell_id,
            "type": self.cell_type,
        }


@dataclass(frozen=True)
class OutlineEntry:
    """One cell as the agent sees it in the outline."""

    cell_number: int
    cell_id: str
    cell_type: str
    first_line: str
    line_count: int
    name: str | None

    def as_dict(self) -> dict[str, object]:
        entry: dict[str, object] = {
            "cell_number": self.cell_number,
            "cell_id": self.cell_id,
            "type": self.cell_type,
            "first_line": self.first_line,
            "line_count": self.line_count,
        }
        if self.name:
            entry["name"] = self.name
        return entry


def outline(script: Script) -> list[OutlineEntry]:
    """Summarize every cell for ``nb_outline``."""
    entries: list[OutlineEntry] = []

    for position, cell in enumerate(script.cells, start=1):
        text = source_text(cell.get("source"))
        lines = text.splitlines()
        first = next((line.strip() for line in lines if line.strip()), "")
        cell_type = str(cell.get("cell_type") or "code")
        metadata = cell.get("metadata") or {}
        name = metadata.get("name") if isinstance(metadata, dict) else None

        entries.append(
            OutlineEntry(
                cell_number=position,
                cell_id=str(cell.get("id") or ""),
                cell_type=cell_type,
                first_line=first[:120],
                line_count=len(lines),
                name=name if isinstance(name, str) and name else None,
            )
        )

    return entries


def resolve(
    script: Script,
    *,
    cell_id: str | None = None,
    cell_number: int | None = None,
) -> CellRef:
    """Find a cell by id or by 1-based number."""
    cells = script.cells

    if cell_id:
        for position, cell in enumerate(cells, start=1):
            if isinstance(cell, dict) and cell.get("id") == cell_id:
                return CellRef(
                    cell_id=cell_id,
                    cell_number=position,
                    cell_type=str(cell.get("cell_type") or "code"),
                    index=position - 1,
                )
        raise EditMcpError(
            ErrorCode.CELL_NOT_FOUND,
            f"No cell with id {cell_id!r} in {script.path.name}.",
            hint="Call nb_outline again: inserting or deleting cells renumbers "
            "them. cell_id is the stable way to refer to a cell.",
            details={
                "path": script.path.name,
                "cell_id": cell_id,
                "available": [str(c.get("id")) for c in cells if isinstance(c, dict)][:50],
            },
        )

    if cell_number is None:
        raise EditMcpError(
            ErrorCode.CELL_NOT_FOUND,
            "Pass either cell_id or cell_number to identify a cell.",
            hint="Use cell_id from nb_outline; it survives insertions.",
            details={"path": script.path.name},
        )

    if not isinstance(cell_number, int) or not 1 <= cell_number <= len(cells):
        raise EditMcpError(
            ErrorCode.RANGE_OUT_OF_BOUNDS,
            f"{script.path.name} has {len(cells)} cell(s), so cell_number "
            f"{cell_number} is out of range.",
            hint="Call nb_outline for the current cell list.",
            details={
                "path": script.path.name,
                "cell_number": cell_number,
                "cell_count": len(cells),
            },
        )

    cell = cells[cell_number - 1]
    return CellRef(
        cell_id=str(cell.get("id") or ""),
        cell_number=cell_number,
        cell_type=str(cell.get("cell_type") or "code"),
        index=cell_number - 1,
    )


def insert(
    script: Script,
    *,
    source: str,
    cell_type: str,
    after_cell_id: str | None = None,
    index: int | None = None,
    at_start: bool = False,
    at_end: bool = False,
) -> CellRef:
    """Insert a cell and return where it landed."""
    if cell_type not in ("code", "markdown", "raw"):
        raise EditMcpError(
            ErrorCode.CELL_TYPE_INVALID,
            f"Unknown cell type {cell_type!r}.",
            hint="Use 'code', 'markdown', or 'raw'.",
            details={"cell_type": cell_type, "valid": ["code", "markdown", "raw"]},
        )

    cells = script.cells
    existing = {str(c.get("id")) for c in cells if isinstance(c, dict)}

    if at_start:
        position = 0
    elif at_end or (after_cell_id is None and index is None):
        position = len(cells)
    elif after_cell_id:
        position = _index_of_id(cells, after_cell_id, script) + 1
    else:
        if not isinstance(index, int) or not 0 <= index <= len(cells):
            raise EditMcpError(
                ErrorCode.RANGE_OUT_OF_BOUNDS,
                f"index {index} is out of range for a script with "
                f"{len(cells)} cell(s).",
                hint="Index 0 is before the first cell; index N is after the last.",
                details={"index": index, "cell_count": len(cells)},
            )
        position = index

    # The cell's id is the content hash of the source the caller provided.
    # If that hash collides with an existing cell, fall back to a fresh
    # random id so two cells never share one. The content-based id is what
    # makes the address stable across reads; the random fallback is the
    # safety net for the case where the user intentionally inserts a
    # duplicate.
    from .script import content_based_id

    source_lines = to_source_list(source)
    cell_id = content_based_id(source_lines)
    if cell_id in existing:
        cell_id = generate_cell_id(existing)

    cell: dict[str, Any] = {
        "cell_type": cell_type,
        "id": cell_id,
        "source": source_lines,
        "metadata": {},
    }
    cells.insert(position, cell)
    return CellRef(
        cell_id=cell["id"],
        cell_number=position + 1,
        cell_type=cell_type,
        index=position,
    )


def update(
    script: Script,
    ref: CellRef,
    *,
    source: str | None = None,
    cell_type: str | None = None,
) -> None:
    """Replace a cell's source and/or its type."""
    if cell_type and cell_type not in ("code", "markdown", "raw"):
        raise EditMcpError(
            ErrorCode.CELL_TYPE_INVALID,
            f"Unknown cell type {cell_type!r}.",
            hint="Use 'code', 'markdown', or 'raw'.",
            details={"cell_type": cell_type, "valid": ["code", "markdown", "raw"]},
        )
    cell = script.cells[ref.index]

    if cell_type and cell_type != cell.get("cell_type"):
        cell["cell_type"] = cell_type

    if source is not None:
        cell["source"] = to_source_list(source)


def delete(script: Script, refs: list[CellRef]) -> int:
    """Remove cells by index, highest first so indices stay valid."""
    for ref in sorted(refs, key=lambda r: r.index, reverse=True):
        del script.cells[ref.index]
    return len(refs)


def move(script: Script, ref: CellRef, destination: int) -> CellRef:
    """Move a cell to a new 0-based position."""
    cells = script.cells
    total = len(cells)
    if not 0 <= destination < total:
        raise EditMcpError(
            ErrorCode.RANGE_OUT_OF_BOUNDS,
            f"destination {destination} is out of range for a script with "
            f"{total} cell(s).",
            hint=f"Destination is 0-based: 0 is the first cell, {total - 1} the last.",
            details={"destination": destination, "cell_count": total},
        )

    cell = cells.pop(ref.index)
    position = destination if destination <= ref.index else destination
    cells.insert(min(position, len(cells)), cell)

    return CellRef(
        cell_id=str(cell.get("id") or ""),
        cell_number=cells.index(cell) + 1,
        cell_type=str(cell.get("cell_type") or "code"),
        index=cells.index(cell),
    )


def notebook_path_or_script(path: Path) -> str:
    """Classify a path so the dispatch in the tools can pick the right engine.

    Returns one of ``"ipynb"``, ``"py"``, or raises. The ``"py"`` branch is
    silent about whether the file is actually a percent script: a percent
    file without markers is rejected at ``load`` time, where the user sees a
    clear ``SCRIPT_INVALID`` instead of a confusing ``NB_OUTLINE`` against
    an empty result.
    """
    suffix = path.suffix.lower()
    if suffix == ".ipynb":
        return "ipynb"
    if suffix == ".py":
        return "py"
    raise EditMcpError(
        ErrorCode.NOTEBOOK_INVALID,
        f"{path.name} is not an .ipynb or .py file.",
        hint="Notebook tools operate on .ipynb files and percent-format "
        ".py files. Use edit_apply for ordinary .py files.",
        details={"path": path.name, "suffix": suffix},
    )


def _index_of_id(cells: list[dict], cell_id: str, script: Script) -> int:
    for position, cell in enumerate(cells):
        if isinstance(cell, dict) and cell.get("id") == cell_id:
            return position
    raise EditMcpError(
        ErrorCode.CELL_NOT_FOUND,
        f"No cell with id {cell_id!r} in {script.path.name}.",
        hint="Call nb_outline for the current cell list.",
        details={"path": script.path.name, "cell_id": cell_id},
    )
