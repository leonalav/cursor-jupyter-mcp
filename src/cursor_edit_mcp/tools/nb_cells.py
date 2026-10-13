"""The six notebook cell tools, operating on ``.ipynb`` and ``.py``.

``nb_outline`` and ``nb_cell_read`` are the two you call first: the outline
gives you stable ``cell_id`` values and the read gives you the source to edit.
The four mutators then take a ``cell_id`` rather than a number, because numbers
shift the moment anything is inserted or removed.

The tools accept both ``.ipynb`` files and percent-format ``.py`` files. The
shape of every result is the same; the only difference is the engine that
loads and writes the file, and whether the cell's outputs are persisted back
to the file (always for notebooks, never for percent scripts).
"""

from __future__ import annotations

from typing import Any

from ..errors import EditMcpError, ErrorCode
from ..logging_setup import get_logger
from ..notebook import cells as cell_ops
from ..notebook.dispatch import load_session
from ..notebook.engine import CELL_TYPES, source_text
from ..notebook.session import VALIDATE_OFF, VALIDATE_WARN
from ..settings import Settings, get_settings

_LOGGER = get_logger(__name__)

OUTLINE_DESCRIPTION = """\
List a notebook's cells. Start here — this is how you get the `cell_id` values \
every other notebook tool uses.

Works on `.ipynb` files and on percent-format `.py` scripts (`# %%` cell
markers). The cell list, ids, and numbering are identical for both.

Returns, for each cell in file order: `cell_number` (1-based, counting every \
cell including markdown, matching the number Cursor shows), `cell_id` (stable \
and preferred for editing), `type`, the cell's first non-empty line, its line \
count, and a one-line output summary for code cells.

After inserting, deleting, or moving cells, `cell_number` values shift but \
`cell_id` values do not. Re-read the outline after a structural change rather \
than reusing numbers you read earlier."""

NB_READ_DESCRIPTION = """\
Read one or more notebook cells in full.

Works on `.ipynb` and percent-format `.py` files. Pass `cell_id` (preferred,
stable) or `cell_number` (1-based, shifts as cells move). Read the cells
before editing them: notebook line numbers are cell-relative, so `range` in
`nb_cell_update` counts from the start of that cell and means nothing without
seeing it.

`include_outputs: true` returns each code cell's outputs, which is how you \
check what a cell actually did rather than inferring it from the source."""

INSERT_DESCRIPTION = """\
Insert a new cell into a notebook.

Works on `.ipynb` and percent-format `.py` files. Position it with exactly
one of: `after_cell_id` (preferred, stable), `index` (0-based position among
cells), `at_start: true`, or `at_end: true`.

Returns the new cell's `cell_id` and `cell_number`, plus the updated outline so \
you do not have to re-read it.

Code cells start with no outputs and `execution_count: null`. Existing cells \
are never touched."""

UPDATE_DESCRIPTION = """\
Replace a cell's source, or part of it.

Works on `.ipynb` and percent-format `.py` files. Identify the cell with
`cell_id` (preferred) or `cell_number`.

Pass `source` to replace the whole cell, or `range` with `source` to replace \
only those cell-relative lines: `range` counts from line 1 of that cell, not \
from the top of the notebook. Use `expect` with a ranged edit so the change is \
refused if the cell has moved on.

Changing a code cell's source clears its outputs and `execution_count`, because \
an output that no longer matches its code is misleading. Pass \
`keep_outputs: true` to retain them.

Pass `cell_type` to change a cell between `code`, `markdown`, and `raw`."""

DELETE_DESCRIPTION = """\
Delete cells from a notebook.

Works on `.ipynb` and percent-format `.py` files. Identify cells with
`cell_id` values from `nb_outline`. `cell_number` is also accepted but shifts
as the notebook changes, so prefer ids.

Deletes in a single call are applied together and the notebook is validated \
afterwards. Returns the updated outline."""

MOVE_DESCRIPTION = """\
Move a cell to a new position.

Works on `.ipynb` and percent-format `.py` files. Identify the cell with
`cell_id` and give `destination` as a 0-based index among cells: 0 is the
first cell, and the last valid value is one less than the cell count.

Returns the cell's new `cell_number` and the updated outline. Other cells keep \
their ids; only the numbering shifts."""


def _check_cell_type(cell_type: str) -> None:
    if cell_type not in CELL_TYPES:
        raise EditMcpError(
            ErrorCode.CELL_TYPE_INVALID,
            f"Unknown cell type {cell_type!r}.",
            hint="Use 'code', 'markdown', or 'raw'.",
            details={"cell_type": cell_type, "valid": list(CELL_TYPES)},
        )


def nb_outline(
    path: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """List every cell with its number, id, type, and output summary."""
    settings = settings or get_settings()
    dispatched = load_session(path, settings)
    entries = [entry.as_dict() for entry in dispatched.session.outline()]

    body: dict[str, Any] = {
        "ok": True,
        "path": path,
        "hash": dispatched.session.current_hash,
        "cell_count": len(entries),
        "format": dispatched.kind,
        "cells": entries,
    }
    if not entries:
        body["note"] = "This file has no cells. Use nb_cell_insert to add one."
    return body


def nb_cell_read(
    path: str,
    cell_id: str | None = None,
    cell_number: int | None = None,
    include_outputs: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Read one or more cells in full."""
    settings = settings or get_settings()
    dispatched = load_session(path, settings)

    if dispatched.kind == "ipynb":
        ref = dispatched.session.resolve(cell_id=cell_id, cell_number=cell_number)
        cell = dispatched.session.notebook.cells[ref.index]
        text = source_text(cell.get("source"))
        entry: dict[str, Any] = {
            **ref.as_dict(),
            "source": text,
            "metadata": cell.get("metadata") or {},
        }
        if ref.cell_type == "code":
            entry["execution_count"] = cell.get("execution_count")
            if include_outputs:
                entry["outputs"] = cell.get("outputs") or []
        cells_out = [entry]
    else:
        ref = dispatched.session.resolve(cell_id=cell_id, cell_number=cell_number)
        cell = dispatched.session.script.cells[ref.index]
        text = source_text(cell.get("source"))
        entry = {
            **ref.as_dict(),
            "source": text,
            "metadata": cell.get("metadata") or {},
        }
        if ref.cell_type == "code" and include_outputs:
            # Percent scripts have no outputs on disk. The agent asked for
            # them, so we tell it they are absent rather than fabricating.
            entry["outputs"] = []
            entry["note"] = "Percent-format scripts do not persist outputs."
        cells_out = [entry]

    body: dict[str, Any] = {
        "ok": True,
        "path": path,
        "hash": dispatched.session.current_hash,
        "format": dispatched.kind,
        "cells": cells_out,
    }
    if include_outputs:
        body["outputs_included"] = True
    return body


def nb_cell_insert(
    path: str,
    source: str,
    cell_type: str = "code",
    after_cell_id: str | None = None,
    index: int | None = None,
    at_start: bool = False,
    at_end: bool = False,
    base_hash: str | None = None,
    validate: str = VALIDATE_WARN,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Insert a cell at a position."""
    settings = settings or get_settings()
    _check_cell_type(cell_type)
    dispatched = load_session(path, settings, base_hash=base_hash)

    ref = dispatched.session.insert(
        source=source,
        cell_type=cell_type,
        after_cell_id=after_cell_id,
        index=index,
        at_start=at_start,
        at_end=at_end,
    )

    if dispatched.kind == "ipynb":
        return dispatched.session.finish(
            validate=validate,
            extra={"inserted": ref.as_dict()},
        )
    return dispatched.session.finish(extra={"inserted": ref.as_dict()})


def nb_cell_update(
    path: str,
    cell_id: str | None = None,
    cell_number: int | None = None,
    source: str | None = None,
    range: dict[str, int] | None = None,
    expect: str | None = None,
    cell_type: str | None = None,
    keep_outputs: bool = False,
    base_hash: str | None = None,
    validate: str = VALIDATE_WARN,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Replace a cell's whole source, or a cell-relative range of it."""
    settings = settings or get_settings()
    if cell_type:
        _check_cell_type(cell_type)
    dispatched = load_session(path, settings, base_hash=base_hash)

    ref = dispatched.session.resolve(cell_id=cell_id, cell_number=cell_number)

    if range is not None:
        if dispatched.kind == "ipynb":
            cell_text = source_text(dispatched.session.notebook.cells[ref.index].get("source"))
        else:
            cell_text = source_text(dispatched.session.script.cells[ref.index].get("source"))
        source = _replace_in_range(
            cell_text,
            range,
            source,
            expect,
            path=path,
            cell_number=ref.cell_number,
        )

    if source is None and cell_type is None:
        raise EditMcpError(
            ErrorCode.INVALID_EDIT,
            "Pass source, range, or cell_type — there is nothing to update.",
            hint="Pass `source` to replace the cell, or `range` plus `source` "
            "to replace only some of its lines.",
            details={"path": path, "cell_id": ref.cell_id},
        )

    if dispatched.kind == "ipynb":
        dispatched.session.update(
            ref,
            source=source,
            keep_outputs=keep_outputs,
            cell_type=cell_type,
        )
        return dispatched.session.finish(
            validate=validate,
            extra={"updated": ref.as_dict(), "outputs_cleared": not keep_outputs},
        )
    dispatched.session.update(ref, source=source, cell_type=cell_type)
    return dispatched.session.finish(extra={"updated": ref.as_dict()})


def nb_cell_delete(
    path: str,
    cell_id: str | None = None,
    cell_number: int | None = None,
    base_hash: str | None = None,
    validate: str = VALIDATE_WARN,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Delete one or more cells by id."""
    settings = settings or get_settings()
    dispatched = load_session(path, settings, base_hash=base_hash)

    ids = [cell_id] if cell_id else _numbers_to_ids(dispatched.session, cell_number, path)
    refs = [
        dispatched.session.resolve(cell_id=one) for one in ids  # noqa: PERF401
    ]

    distinct = {ref.index for ref in refs}
    if len(distinct) != len(refs):
        raise EditMcpError(
            ErrorCode.INVALID_EDIT,
            "The same cell was listed more than once.",
            hint="Delete each cell once.",
            details={"path": path, "cell_ids": ids},
        )

    removed = dispatched.session.delete(refs)
    if dispatched.kind == "ipynb":
        return dispatched.session.finish(
            validate=validate,
            extra={"deleted": [ref.as_dict() for ref in refs], "deleted_count": removed},
        )
    return dispatched.session.finish(
        extra={"deleted": [ref.as_dict() for ref in refs], "deleted_count": removed},
    )


def nb_cell_move(
    path: str,
    cell_id: str,
    destination: int,
    base_hash: str | None = None,
    validate: str = VALIDATE_WARN,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Move a cell to a 0-based destination index."""
    settings = settings or get_settings()
    dispatched = load_session(path, settings, base_hash=base_hash)

    ref = dispatched.session.resolve(cell_id=cell_id)
    moved = dispatched.session.move(ref, destination)
    if dispatched.kind == "ipynb":
        return dispatched.session.finish(
            validate=validate,
            extra={"moved": moved.as_dict(), "from": ref.as_dict()},
        )
    return dispatched.session.finish(
        extra={"moved": moved.as_dict(), "from": ref.as_dict()},
    )


# --- helpers --------------------------------------------------------------


def _replace_in_range(
    text: str,
    span: dict[str, int],
    source: str | None,
    expect: str | None,
    *,
    path: str,
    cell_number: int,
) -> str:
    """Replace cell-relative lines, honouring ``expect``."""
    start = int(span.get("start_line") or 0)
    end = int(span.get("end_line") or start)
    lines = text.splitlines()

    if not 1 <= start <= end <= len(lines):
        raise EditMcpError(
            ErrorCode.RANGE_OUT_OF_BOUNDS,
            f"range {start}-{end} is outside cell {cell_number}, which has "
            f"{len(lines)} line(s).",
            hint="Cell-relative ranges start at 1 within that cell. Read the "
            "cell with nb_cell_read to see its contents.",
            details={
                "path": path,
                "cell_number": cell_number,
                "start_line": start,
                "end_line": end,
                "cell_line_count": len(lines),
            },
        )

    current = "\n".join(lines[start - 1 : end])
    if expect is not None and expect not in current:
        raise EditMcpError(
            ErrorCode.EXPECTED_TEXT_MISMATCH,
            f"`expect` text is not in cell {cell_number} lines {start}-{end}.",
            hint="The cell has changed since you read it. Use current_text, or "
            "read the cell again.",
            details={
                "path": path,
                "cell_number": cell_number,
                "expect": expect,
                "current_text": current,
            },
        )

    if source is None:
        raise EditMcpError(
            ErrorCode.INVALID_EDIT,
            "Pass `source` alongside `range` to say what the lines become.",
            hint="`range` selects lines; `source` replaces them.",
            details={"path": path, "cell_number": cell_number},
        )

    replacement = source.split("\n")
    merged = lines[: start - 1] + replacement + lines[end:]
    return "\n".join(merged)


def _numbers_to_ids(session, cell_number: int | None, path: str) -> list[str]:
    """Accept `cell_number` as a fallback, with a clear error when absent."""
    if cell_number is None:
        raise EditMcpError(
            ErrorCode.CELL_NOT_FOUND,
            "Pass cell_id to say which cell to delete.",
            hint="Call nb_outline to get cell_id values; they are stable when "
            "cell numbers are not.",
            details={"path": path},
        )
    ref = session.resolve(cell_number=cell_number)
    return [ref.cell_id]


__all__ = [
    "DELETE_DESCRIPTION",
    "INSERT_DESCRIPTION",
    "MOVE_DESCRIPTION",
    "OUTLINE_DESCRIPTION",
    "NB_READ_DESCRIPTION",
    "UPDATE_DESCRIPTION",
    "nb_cell_delete",
    "nb_cell_insert",
    "nb_cell_move",
    "nb_cell_read",
    "nb_outline",
    "nb_cell_update",
    "VALIDATE_OFF",
]
