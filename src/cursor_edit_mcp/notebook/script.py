"""Percent-format scripts: ``# %%`` cells in a ``.py`` file.

A percent-format script is a plain Python file whose lines starting with
``# %%`` (column zero, optionally followed by a name) mark cell boundaries.
VS Code, Spyder, and Jupyter treat these the same as a notebook: each
section runs as a cell and the kernel keeps state between them.

**Cell markers.** A line that starts with ``# %%`` (and only that) opens a
new cell. Anything after the marker on the same line is metadata:

.. code-block:: python

    # %%                              # code cell, unnamed
    # %% my cell                      # code cell, named "my cell"
    # %% [markdown]                   # markdown cell
    # %% [raw]                        # raw cell
    # %% [markdown] Notes             # markdown cell, named "Notes"

The first non-marker line after the marker starts the cell's content. The
content runs until the next ``# %%`` marker or the end of the file.

**Stable cell ids.** ``nb_outline`` and friends return an ``id`` for every
cell. Notebook cells get their id from the JSON; percent cells get one from
a content hash, with an optional ``# id:`` override written into the file.

The id scheme is two-layered:

1. **In the file.** After a mutation, every cell is preceded by a comment
   line ``# id: a1b2c3d4`` in the metadata block. The id is written the
   first time the cell is mutated, and the cell tools read it on every
   subsequent load. The id is content-derived (the first 8 hex characters
   of the SHA-1 of the cell's source) so that re-running a mutation does
   not change the id, and so that two cells that happen to share the same
   id are detected and re-hashed on save.

2. **In memory.** A cell without a ``# id:`` line is assigned one as soon
   as the file is loaded. The id is content-derived, so reading the file
   twice produces the same id, even without writing it back. This is what
   makes ``nb_outline`` followed by ``nb_cell_update`` work without an
   intermediate write.

The two together mean a cell has the same id whether the user reads it
before or after a save, and the id is preserved across edits as long as
the cell's source did not change.

**Cell types.** ``[markdown]`` and ``[raw]`` are explicit; everything else is
code. A ``# %% [markdown]`` cell is never executed.

**Why not parse a virtual notebook?** It would round-trip the file through
``.ipynb`` and back, which would write magic lines, strip comments, and lose
imports the user typed outside any cell. Keeping the file as a ``.py`` and
operating on the marker lines preserves what the user wrote.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import EditMcpError, ErrorCode

#: Matches a cell marker at the start of a line. Group 1 is the cell name
#: (without brackets); group 2 is the bracketed type, lower-cased inside
#: (``[markdown]`` or ``[raw]``); both may be ``None``. Trailing ``[name]``
#: after a type is permitted, e.g. ``# %% [markdown] Notes``.
_CELL_MARKER = re.compile(
    r"^# %%(?:\s+\[([a-z]+)\])?\s*(.*?)\s*$",
    re.MULTILINE,
)

#: Recognized cell types in the bracketed slot.
_MARKDOWN_TYPE = "markdown"
_RAW_TYPE = "raw"
_VALID_TYPES = ("code", "markdown", "raw")

#: Recognized cell names (anything else becomes a free-form label).
_TYPE_FROM_BRACKET = {
    "markdown": _MARKDOWN_TYPE,
    "md": _MARKDOWN_TYPE,
    "raw": _RAW_TYPE,
    "code": "code",
    "py": "code",
}

#: A metadata comment line. ``# key: value``. Key is a single token.
_METADATA_LINE = re.compile(r"^#\s*([A-Za-z_][\w-]*)\s*:\s*(.*)$")

#: The id is stored under this key in the metadata block.
_ID_KEY = "id"

#: Cell ids are 8 hex characters, mirroring the notebook engine.
ID_LENGTH = 8


@dataclass
class Script:
    """A parsed percent-format script.

    The ``cells`` list is the in-memory representation. The ``preamble`` is
    everything before the first ``# %%`` marker (imports, module docstring,
    constant definitions); it is preserved verbatim and is not a cell,
    because users expect to write a percent script and have the import
    section be there, not buried in cell 1.
    """

    path: Path
    preamble: str
    cells: list[dict] = field(default_factory=list)
    eol: str = "\n"
    trailing_newline: bool = True
    #: Cell ids that were generated to make them unique. The user sees them
    #: in the file on next save; reported in the same way the notebook
    #: engine reports repaired ids, so a tool that mutates many cells does
    #: not silently change more than the user asked for.
    repaired_ids: tuple[str, ...] = ()
    #: Cell types outside the nbformat canonical set are tolerated the same
    #: way the notebook engine tolerates unknown fields, because real scripts
    #: do carry them.
    unknown_types: tuple[str, ...] = ()


def load(path: Path, raw: bytes) -> Script:
    """Parse a percent-format script into cells.

    A file with no ``# %%`` markers is rejected with ``SCRIPT_INVALID``: the
    cell tools are for percent-format scripts, and silently accepting an
    ordinary ``.py`` would let the agent treat it as a notebook with one
    cell, which is the wrong shape and would surprise the user.
    """
    text = _decode(raw)
    if not _CELL_MARKER.search(text):
        raise EditMcpError(
            ErrorCode.SCRIPT_INVALID,
            f"{path.name} has no '# %%' cell markers.",
            hint="Percent-format scripts start each cell with a line that "
            "begins with '# %%'. Use edit_apply on a regular .py file, or "
            "add at least one '# %%' marker.",
            details={"path": path.name},
        )

    preamble, cells, unknown = _parse_cells(text)
    return Script(
        path=path,
        preamble=preamble,
        cells=cells,
        eol=detect_eol(text),
        trailing_newline=text.endswith("\n"),
        unknown_types=tuple(sorted(unknown)),
    )


def _parse_cells(text: str) -> tuple[str, list[dict], set[str]]:
    """Walk the file line by line, building cells.

    A marker line starts a new cell. The metadata comment block immediately
    after the marker is read for the cell id; everything else is the cell's
    content. A trailing blank line before the next marker is dropped from
    the cell so the source round-trips without an extra newline.

    Returns the preamble (text before the first marker), the cells, and the
    set of cell types that did not match a known one. The cell type is
    taken from the bracketed slot, falling back to ``code`` when absent.
    """
    lines = text.splitlines(keepends=True)
    preamble_end = 0
    first_marker = -1

    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if _CELL_MARKER.match(stripped):
            first_marker = index
            break
        preamble_end = index + 1

    preamble = "".join(lines[:preamble_end]) if first_marker >= 0 else text
    if first_marker < 0:
        return preamble, [], set()

    cells: list[dict] = []
    unknown: set[str] = set()

    cursor = first_marker
    while cursor < len(lines):
        stripped = lines[cursor].lstrip()
        match = _CELL_MARKER.match(stripped)
        if not match:
            cursor += 1
            continue

        bracket, name = match.group(1), match.group(2)
        cell_type = _TYPE_FROM_BRACKET.get(bracket.lower() if bracket else "code", "code")
        if bracket and cell_type not in _VALID_TYPES:
            unknown.add(bracket)
            cell_type = "code"

        # The metadata block is the run of ``# key: value`` lines that
        # immediately follows the marker. As soon as a non-metadata line
        # appears, the cell content starts.
        cursor += 1
        metadata: dict[str, str] = {}
        while cursor < len(lines):
            meta_line = lines[cursor]
            meta_stripped = meta_line.lstrip()
            if _CELL_MARKER.match(meta_stripped):
                break
            meta_match = _METADATA_LINE.match(meta_stripped)
            if not meta_match:
                break
            metadata[meta_match.group(1).strip()] = meta_match.group(2).strip()
            cursor += 1

        # Cell content is the lines from here until the next marker or EOF.
        content_start = cursor
        while cursor < len(lines):
            if _CELL_MARKER.match(lines[cursor].lstrip()):
                break
            cursor += 1
        content = lines[content_start:cursor]

        # Drop exactly one trailing blank line so two adjacent cells stay
        # separated by the marker alone and do not gain a stray newline.
        if content and content[-1].strip() == "":
            content = content[:-1]

        # The id scheme: if the file has a ``# id:`` line, use that. The id
        # is content-derived when missing, which gives a stable id even
        # before the file is rewritten. This is what makes ``nb_outline``
        # followed by ``nb_cell_update`` work without an intermediate write:
        # both calls compute the same id from the same source.
        cell_id = metadata.get(_ID_KEY) or content_based_id(content)
        cells.append(
            {
                "cell_type": cell_type,
                "id": cell_id,
                "source": list(content),
                "metadata": {"name": name} if name else {},
            }
        )

    return preamble, cells, unknown


def to_bytes(script: Script) -> bytes:
    """Serialize a percent-format script the way the user wrote it.

    The preamble is kept first, then each cell as ``# %% [name]`` followed
    by its metadata block (id, plus whatever the user stored) and the
    cell's source. The id is written into the file because removing it on
    every save would mean the cell identity is lost the moment the user
    closes the editor; persisting it makes the script self-describing.
    """
    out: list[str] = []

    if script.preamble:
        out.append(script.preamble)
        if not script.preamble.endswith("\n"):
            out[-1] = out[-1] + "\n"
        # A single blank line between the preamble and the first marker is
        # what a human writes; the parser tolerates any number, but emitting
        # one keeps the file readable.
        if not out[-1].endswith("\n\n"):
            out[-1] = out[-1].rstrip("\n") + "\n\n"

    for cell in script.cells:
        bracket = ""
        if cell["cell_type"] == "markdown":
            bracket = " [markdown]"
        elif cell["cell_type"] == "raw":
            bracket = " [raw]"

        name = (cell.get("metadata") or {}).get("name", "")
        marker = f"# %%{bracket}"
        if name:
            marker = f"{marker} {name}"
        out.append(marker + "\n")

        # The id is the only metadata we always write. The user's other
        # metadata is preserved in memory and rewritten on the next save,
        # but we keep the on-disk surface minimal: an id line is enough for
        # the cell tools to round-trip, and the user's eyes don't need to
        # see anything else.
        cell_id = cell.get("id", "")
        if cell_id:
            out.append(f"# {_ID_KEY}: {cell_id}\n")

        out.extend(cell.get("source") or [])
        if not out[-1].endswith("\n"):
            out[-1] = out[-1] + "\n"
        out.append("\n")  # blank line between cells

    text = "".join(out)
    eol = script.eol
    if eol != "\n":
        text = text.replace("\n", eol)
    if script.trailing_newline and not text.endswith(eol):
        text += eol
    return text.encode("utf-8")


def source_text(source) -> str:
    """Join a cell's source lines back into text (the same as the notebook engine)."""
    if isinstance(source, list):
        return "".join(part for part in source if isinstance(part, str))
    return source if isinstance(source, str) else ""


def content_based_id(source_lines: list[str]) -> str:
    """A stable, content-derived id for a percent-format cell.

    The id is the first 8 hex characters of the SHA-1 of the cell's
    source, including trailing newlines. Two cells with the same source
    have the same id; an edit changes the id. This is what makes the
    cell addressable before the file is rewritten with ``# id:`` lines:
    a read followed by an edit sees the same id, because the source has
    not changed in between.

    The 8-character length matches the notebook engine's ``ID_LENGTH``,
    so the cell id is the same shape regardless of file format.
    """
    text = source_text(source_lines)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:ID_LENGTH]


def to_source_list(source) -> list[str]:
    """Normalize a cell's source to a list of lines."""
    if isinstance(source, list):
        return [line if isinstance(line, str) else str(line) for line in source]
    if isinstance(source, str):
        return source.splitlines(keepends=True)
    if source is None:
        return []
    return str(source).splitlines(keepends=True)


def set_source(cell: dict, text: str) -> None:
    """Write a cell's source in canonical form (list of lines)."""
    cell["source"] = to_source_list(text)


def generate_cell_id(existing: set[str]) -> str:
    """A fresh 8-character id that does not collide with ``existing``."""
    while True:
        candidate = uuid.uuid4().hex[:ID_LENGTH]
        if candidate not in existing:
            return candidate


def repair_cell_ids(cells: list[dict]) -> tuple[str, ...]:
    """Make every cell id unique, regenerating only what must change.

    Ids that are already non-empty and unique are left untouched. Only cells
    that lack an id, or that share an id with another cell, are regenerated.
    This is called on every write so a freshly created cell (which was assigned
    a temp id in memory) gets a permanent id in the file, but cells that
    already have a stable id written to disk are not silently changed.
    """
    seen: set[str] = set()
    assigned: list[str] = []
    needs_repair: list[tuple[int, dict]] = []  # (index, cell) pairs that must be touched

    for idx, cell in enumerate(cells):
        if not isinstance(cell, dict):
            continue
        cell_id = cell.get("id")
        if not isinstance(cell_id, str) or not cell_id:
            needs_repair.append((idx, cell))
        elif cell_id in seen:
            needs_repair.append((idx, cell))
        else:
            seen.add(cell_id)

    for idx, cell in needs_repair:
        new_id = generate_cell_id(seen)
        cell["id"] = new_id
        seen.add(new_id)
        assigned.append(new_id)

    return tuple(assigned)


def finalize(script: Script) -> tuple[str, ...]:
    """Prepare a mutated script for writing.

    Mirrors ``notebook.cells.finalize``: sources canonical, ids unique, and
    the script is well-formed by the time it is serialised.
    """
    repaired = repair_cell_ids(script.cells)
    return repaired


# --- internals ------------------------------------------------------------


def _decode(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return raw.decode("utf-8", errors="replace")


def detect_eol(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf and crlf > 0 else "\n"
