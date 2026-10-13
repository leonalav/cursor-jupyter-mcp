"""Notebook loading, validation, and serialization.

Three rules shape this module.

**Use the JSON parser, not regex.** A notebook is JSON with deeply nested
structure and string values containing braces and quotes. Anything regex-based
will eventually corrupt a notebook, and corruption here is unrecoverable: the
user loses unsaved work.

**Never round-trip through nbformat's object model.** ``nbformat.read`` will
happily drop fields it does not know about, including a user's custom
metadata. So this module parses with ``json.loads``, uses nbformat only for
*schema validation*, and serializes itself. Unknown fields, attachments, and
anything nbformat does not model survive untouched.

**A load-then-save of an untouched file must be byte-identical.** Notebooks live
in Git, and a serialization change rewrites every line in the repository's
history for no reason. This is tested, and it is the constraint that decides
the details below.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..errors import EditMcpError, ErrorCode

#: nbformat minor version this server writes.
NBFORMAT_MINOR = 5

#: Cell ids are 8 hex characters, per the nbformat 4.5 schema.
ID_LENGTH = 8

CELL_TYPES = ("code", "markdown", "raw")


@dataclass
class Notebook:
    """A parsed notebook, plus the file conventions needed to write it back."""

    path: Path
    data: dict
    eol: str = "\n"
    trailing_newline: bool = True
    #: Cell ids that had to be regenerated to make them unique.
    repaired_ids: tuple[str, ...] = ()
    #: Cell-level keys outside the nbformat schema, preserved but reported.
    unknown_fields: tuple[str, ...] = ()

    @property
    def cells(self) -> list[dict]:
        return self.data.setdefault("cells", [])

    @property
    def nbformat_minor(self) -> int:
        return int(self.data.get("nbformat_minor") or 4)

    def with_data(self, data: dict) -> Notebook:
        return Notebook(
            path=self.path,
            data=data,
            eol=self.eol,
            trailing_newline=self.trailing_newline,
            repaired_ids=self.repaired_ids,
            unknown_fields=self.unknown_fields,
        )


def load(path: Path, raw: bytes) -> Notebook:
    """Parse and schema-validate a notebook.

    Raises ``NOTEBOOK_INVALID`` when the file is not JSON or not a valid
    nbformat document. Refusing here is the point: every mutation below assumes
    a well-formed notebook to start from.
    """
    text = _decode(raw)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EditMcpError(
            ErrorCode.NOTEBOOK_INVALID,
            f"{path.name} is not valid JSON (line {exc.lineno}, column {exc.colno}).",
            hint="This file is not a readable notebook. Check that it is a "
            "nbformat 4.x document and has not been partially written.",
            details={"path": path.name, "reason": exc.msg, "line": exc.lineno},
        ) from exc

    if not isinstance(data, dict) or "cells" not in data:
        raise EditMcpError(
            ErrorCode.NOTEBOOK_INVALID,
            f"{path.name} is JSON but not a notebook: it has no 'cells' key.",
            hint="Confirm the path points to a .ipynb file.",
            details={"path": path.name},
        )

    unknown = validate_schema(data, path)

    return Notebook(
        path=path,
        data=data,
        eol=detect_eol(text),
        # Match the file rather than impose a convention: adding or dropping a
        # trailing newline would show up as a one-line diff in every notebook
        # the server ever touches.
        trailing_newline=text.endswith("\n"),
        unknown_fields=unknown,
    )


def validate_schema(data: dict, path: Path) -> tuple[str, ...]:
    """Validate against the nbformat JSON schema.

    nbformat's cell schema sets ``additionalProperties: false``, so a notebook
    carrying a vendor field is rejected outright. Real notebooks do carry them
    (nbstripout, papermill, VS Code, and Jupyter extensions all add keys), and
    Jupyter itself reads and rewrites them without complaint. Rejecting a file
    for having one would make the tool useless on ordinary notebooks.

    So structure is validated strictly and unknown fields are tolerated: the
    structural check runs against a copy with unrecognized cell keys removed,
    and those keys are preserved on write and returned here for reporting.

    Returns the unknown field names found.
    """
    import copy

    import nbformat

    unknown = unknown_cell_fields(data)

    to_check = data
    if unknown:
        to_check = copy.deepcopy(data)
        for cell in to_check.get("cells") or []:
            if not isinstance(cell, dict):
                continue
            allowed = _KNOWN_CELL_KEYS.get(str(cell.get("cell_type")))
            if allowed is None:
                continue
            for key in [k for k in cell if k not in allowed]:
                del cell[key]

    # Cell ids are a 4.5 feature, so a notebook that has them but declares 4.4
    # fails the schema on `id` alone. The presence of ids is better evidence of
    # the file's real version than the declared minor is, so validate it as
    # what it actually is; the write path raises it to 5.
    if _has_cell_ids(to_check) and int(to_check.get("nbformat_minor") or 0) < 5:
        to_check = copy.deepcopy(to_check)
        to_check["nbformat_minor"] = 5

    try:
        nbformat.validate(nbformat.from_dict(to_check))
    except Exception as exc:  # nbformat raises several schema error types
        raise EditMcpError(
            ErrorCode.NOTEBOOK_INVALID,
            f"{path.name} is not a valid nbformat 4.x notebook: {exc}",
            hint="Fix the notebook's structure, or check whether a newer "
            "nbformat version is required.",
            details={"path": path.name, "reason": str(exc)[:500]},
        ) from exc

    return unknown


def _has_cell_ids(data: dict) -> bool:
    return any(
        isinstance(cell, dict) and isinstance(cell.get("id"), str) and cell["id"]
        for cell in data.get("cells") or []
    )


#: Properties the nbformat 4 schema defines for each cell type.
_KNOWN_CELL_KEYS: dict[str, frozenset[str]] = {
    "code": frozenset(
        {"cell_type", "execution_count", "id", "metadata", "outputs", "source"}
    ),
    "markdown": frozenset({"cell_type", "id", "metadata", "source", "attachments"}),
    "raw": frozenset({"cell_type", "id", "metadata", "source", "attachments"}),
}


def unknown_cell_fields(data: dict) -> tuple[str, ...]:
    """Cell-level keys the nbformat schema does not define.

    Reported rather than silently kept, so a caller can see that something
    outside the schema is riding along in the file.
    """
    found: list[str] = []
    for cell in data.get("cells") or []:
        if not isinstance(cell, dict):
            continue
        allowed = _KNOWN_CELL_KEYS.get(str(cell.get("cell_type")))
        if allowed is None:
            continue
        for key in cell:
            if key not in allowed and key not in found:
                found.append(str(key))
    return tuple(found)


def to_bytes(notebook: Notebook) -> bytes:
    """Serialize the way Jupyter does.

    ``indent=1``, sorted keys, UTF-8 without ASCII escaping. Those three are
    Jupyter's own choices and are what keeps diffs to the cells that actually
    changed.
    """
    canonical = canonicalize(notebook.data)
    text = json.dumps(canonical, indent=1, sort_keys=True, ensure_ascii=False)

    eol = notebook.eol
    if eol != "\n":
        text = text.replace("\n", eol)
    if notebook.trailing_newline and not text.endswith(eol):
        text += eol

    return text.encode("utf-8")


def canonicalize(data: dict) -> dict:
    """Normalize ``source`` values to nbformat's list-of-lines form.

    A cell's source may legally be a plain string or a single-element list with
    embedded newlines. Both are normalized to a list of lines, each retaining
    its own terminator, which is what nbformat writes and what keeps a
    round-trip byte-identical.
    """
    for cell in data.get("cells") or []:
        if isinstance(cell, dict) and "source" in cell:
            cell["source"] = to_source_list(cell["source"])
    return data


def to_source_list(source) -> list[str]:
    """Normalize a cell's source to a list of lines."""
    if isinstance(source, list):
        return [line if isinstance(line, str) else str(line) for line in source]
    if isinstance(source, str):
        return source.splitlines(keepends=True)
    if source is None:
        return []
    return str(source).splitlines(keepends=True)


def source_text(source) -> str:
    """Join a cell's source back into text."""
    if isinstance(source, list):
        return "".join(part for part in source if isinstance(part, str))
    return source if isinstance(source, str) else ""


def set_source(cell: dict, text: str) -> None:
    """Write a cell's source in canonical form."""
    cell["source"] = to_source_list(text)


# --- cell identity --------------------------------------------------------


def generate_cell_id(existing: set[str]) -> str:
    """A fresh 8-character id that does not collide with ``existing``."""
    while True:
        candidate = uuid.uuid4().hex[:ID_LENGTH]
        if candidate not in existing:
            return candidate


def repair_cell_ids(cells: list[dict]) -> tuple[str, ...]:
    """Make every cell id unique, regenerating only what must change.

    nbformat 4.5 requires unique ids. A notebook produced by copying and pasting
    cells can violate that, and it must be repaired on write rather than
    refused: a duplicate is a symptom, not the user's intent.

    Returns the ids that were assigned, so the caller can tell the user their
    notebook was touched and which cells now have new identities.
    """
    seen: set[str] = set()
    assigned: list[str] = []

    for cell in cells:
        if not isinstance(cell, dict):
            continue
        cell_id = cell.get("id")
        if not isinstance(cell_id, str) or not cell_id or cell_id in seen:
            new_id = generate_cell_id(seen)
            cell["id"] = new_id
            seen.add(new_id)
            assigned.append(new_id)
        else:
            seen.add(cell_id)

    return tuple(assigned)


def ensure_notebook_minor(data: dict) -> bool:
    """Raise ``nbformat_minor`` to 5 when ids are in use.

    Cell ids are a 4.5 feature. Writing them into a 4.4 notebook produces a
    file that older Jupyter refuses to open.
    """
    if int(data.get("nbformat_minor") or 0) < NBFORMAT_MINOR:
        data["nbformat_minor"] = NBFORMAT_MINOR
        return True
    return False


# --- helpers --------------------------------------------------------------


def _decode(raw: bytes) -> str:
    # Notebooks are JSON, and the JSON spec requires UTF-8. Jupyter writes a BOM
    # when the file had one, so tolerate it rather than failing the read.
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return raw.decode("utf-8", errors="replace")


def detect_eol(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf and crlf > 0 else "\n"


def new_cell(cell_type: str, source: str, *, cell_id: str | None = None) -> dict:
    """A minimal, schema-valid cell."""
    base: dict = {
        "cell_type": cell_type,
        "id": cell_id or generate_cell_id(set()),
        "metadata": {},
        "source": to_source_list(source),
    }
    if cell_type == "code":
        base["outputs"] = []
        base["execution_count"] = None
    return base
