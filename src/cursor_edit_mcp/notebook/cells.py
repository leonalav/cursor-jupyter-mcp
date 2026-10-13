"""Addressing cells and performing notebook mutations.

Addressing follows one rule: **`cell_id` is the identity, `cell_number` is a
position.** The number shifts whenever a cell is inserted, deleted, or moved;
the id does not. Tools accept either, because a human reads "cell 4" but only
the id is safe to use across calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..errors import EditMcpError, ErrorCode
from .engine import (
    Notebook,
    canonicalize,
    ensure_notebook_minor,
    generate_cell_id,
    repair_cell_ids,
    source_text,
    to_source_list,
)


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
    execution_count: int | None
    output_summary: str | None

    def as_dict(self) -> dict[str, object]:
        entry: dict[str, object] = {
            "cell_number": self.cell_number,
            "cell_id": self.cell_id,
            "type": self.cell_type,
            "first_line": self.first_line,
            "line_count": self.line_count,
        }
        if self.execution_count is not None:
            entry["execution_count"] = self.execution_count
        if self.output_summary:
            entry["output_summary"] = self.output_summary
        return entry


def outline(notebook: Notebook) -> list[OutlineEntry]:
    """Summarize every cell for ``nb_outline``."""
    entries: list[OutlineEntry] = []

    for position, cell in enumerate(notebook.cells, start=1):
        text = source_text(cell.get("source"))
        lines = text.splitlines()
        first = next((line.strip() for line in lines if line.strip()), "")
        cell_type = str(cell.get("cell_type") or "code")

        execution_count = cell.get("execution_count") if cell_type == "code" else None
        summary = summarize_outputs(cell.get("outputs")) if cell_type == "code" else None

        entries.append(
            OutlineEntry(
                cell_number=position,
                cell_id=str(cell.get("id") or ""),
                cell_type=cell_type,
                # Keep enough to recognize the cell without being a second copy
                # of its contents.
                first_line=first[:120],
                line_count=len(lines),
                execution_count=execution_count,
                output_summary=summary,
            )
        )

    return entries


def summarize_outputs(outputs) -> str | None:
    """One line describing a cell's outputs, or ``None`` if it has none."""
    if not outputs:
        return None

    kinds: list[str] = []
    for output in outputs:
        if not isinstance(output, dict):
            continue
        kind = output.get("output_type")
        if kind == "error":
            kinds.append("error")
        elif kind not in kinds:
            kinds.append(str(kind))

    if "error" in kinds:
        return f"{len(outputs)} output(s), error"
    return f"{len(outputs)} output(s)"


def resolve(
    notebook: Notebook,
    *,
    cell_id: str | None = None,
    cell_number: int | None = None,
) -> CellRef:
    """Find a cell by id or by 1-based number.

    An unknown id lists what *is* there, because the id usually came from an
    outline that has since shifted.
    """
    cells = notebook.cells

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
            f"No cell with id {cell_id!r} in {notebook.path.name}.",
            hint="Call nb_outline again: inserting or deleting cells renumbers "
            "them. cell_id is the stable way to refer to a cell.",
            details={
                "path": notebook.path.name,
                "cell_id": cell_id,
                "available": [str(c.get("id")) for c in cells if isinstance(c, dict)][
                    :50
                ],
            },
        )

    if cell_number is None:
        raise EditMcpError(
            ErrorCode.CELL_NOT_FOUND,
            "Pass either cell_id or cell_number to identify a cell.",
            hint="Use cell_id from nb_outline; it survives insertions.",
            details={"path": notebook.path.name},
        )

    if not isinstance(cell_number, int) or not 1 <= cell_number <= len(cells):
        raise EditMcpError(
            ErrorCode.RANGE_OUT_OF_BOUNDS,
            f"{notebook.path.name} has {len(cells)} cell(s), so cell_number "
            f"{cell_number} is out of range.",
            hint="Call nb_outline for the current cell list.",
            details={
                "path": notebook.path.name,
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
    notebook: Notebook,
    *,
    source: str,
    cell_type: str,
    after_cell_id: str | None = None,
    index: int | None = None,
    at_start: bool = False,
    at_end: bool = False,
) -> CellRef:
    """Insert a cell and return where it landed."""
    cells = notebook.cells
    existing = {str(c.get("id")) for c in cells if isinstance(c, dict)}

    if at_start:
        position = 0
    elif at_end or (after_cell_id is None and index is None):
        position = len(cells)
    elif after_cell_id:
        position = _index_of_id(cells, after_cell_id, notebook) + 1
    else:
        if not isinstance(index, int) or not 0 <= index <= len(cells):
            raise EditMcpError(
                ErrorCode.RANGE_OUT_OF_BOUNDS,
                f"index {index} is out of range for a notebook with "
                f"{len(cells)} cell(s).",
                hint="Index 0 is before the first cell; index N is after the last.",
                details={"index": index, "cell_count": len(cells)},
            )
        position = index

    cell = {
        "cell_type": cell_type,
        "id": generate_cell_id(existing),
        "metadata": {},
        "source": to_source_list(source),
    }
    if cell_type == "code":
        cell["outputs"] = []
        cell["execution_count"] = None

    cells.insert(position, cell)
    return CellRef(
        cell_id=cell["id"],
        cell_number=position + 1,
        cell_type=cell_type,
        index=position,
    )


def update(
    notebook: Notebook,
    ref: CellRef,
    *,
    source: str | None = None,
    keep_outputs: bool = False,
    cell_type: str | None = None,
) -> None:
    """Replace a cell's source, and optionally its type.

    Outputs and the execution count are cleared when the source changes. A cell
    whose printed output no longer matches its code is worse than a cell with no
    output, because it looks trustworthy.
    """
    cell = notebook.cells[ref.index]

    if cell_type and cell_type != cell.get("cell_type"):
        old = str(cell.get("cell_type"))
        if cell_type == "code":
            cell.setdefault("outputs", [])
            cell.setdefault("execution_count", None)
        else:
            # Outputs and execution_count are not valid on non-code cells, and
            # leaving them in place fails schema validation.
            cell.pop("outputs", None)
            cell.pop("execution_count", None)
        cell["cell_type"] = cell_type
        keep_outputs = True if old == "code" and cell_type != "code" else keep_outputs

    if source is not None:
        cell["source"] = to_source_list(source)
        if cell.get("cell_type") == "code" and not keep_outputs:
            cell["outputs"] = []
            cell["execution_count"] = None


def delete(notebook: Notebook, refs: list[CellRef]) -> int:
    """Remove cells by index, highest first so indices stay valid."""
    for ref in sorted(refs, key=lambda r: r.index, reverse=True):
        del notebook.cells[ref.index]
    return len(refs)


def move(notebook: Notebook, ref: CellRef, destination: int) -> CellRef:
    """Move a cell to a new 0-based position among the cells.

    The cell is removed first and the destination is interpreted against the
    list *without* it, so "move to the end" means what a user expects rather
    than landing one short.
    """
    cells = notebook.cells
    total = len(cells)
    if not 0 <= destination < total:
        raise EditMcpError(
            ErrorCode.RANGE_OUT_OF_BOUNDS,
            f"destination {destination} is out of range for a notebook with "
            f"{total} cell(s).",
            hint=f"Destination is 0-based: 0 is the first cell, {total - 1} the last.",
            details={"destination": destination, "cell_count": total},
        )

    cell = cells.pop(ref.index)
    # After removal the list is one shorter; a destination past the old index
    # refers to a slot that shifted down.
    position = destination if destination <= ref.index else destination
    cells.insert(min(position, len(cells)), cell)

    return CellRef(
        cell_id=str(cell.get("id") or ""),
        cell_number=cells.index(cell) + 1,
        cell_type=str(cell.get("cell_type") or "code"),
        index=cells.index(cell),
    )


def finalize(notebook: Notebook) -> tuple[str, ...]:
    """Prepare a mutated notebook for writing.

    Runs after every mutation, so the guarantees hold no matter which tool
    made the change: sources canonical, ids unique, ids declared by the
    minor version, and the schema valid.
    """
    repaired = repair_cell_ids(notebook.cells)
    canonicalize(notebook.data)
    ensure_notebook_minor(notebook.data)
    return repaired


def _index_of_id(cells: list[dict], cell_id: str, notebook: Notebook) -> int:
    for position, cell in enumerate(cells):
        if isinstance(cell, dict) and cell.get("id") == cell_id:
            return position
    raise EditMcpError(
        ErrorCode.CELL_NOT_FOUND,
        f"No cell with id {cell_id!r} in {notebook.path.name}.",
        hint="Call nb_outline for the current cell list.",
        details={"path": notebook.path.name, "cell_id": cell_id},
    )


def notebook_path(path: Path) -> Path:
    """Reject paths that are not notebooks before anything reads them."""
    if path.suffix.lower() != ".ipynb":
        raise EditMcpError(
            ErrorCode.NOTEBOOK_INVALID,
            f"{path.name} is not an .ipynb file.",
            hint="Notebook tools operate on .ipynb files. Use edit_apply for "
            "percent-format .py scripts.",
            details={"path": path.name, "suffix": path.suffix},
        )
    return path
