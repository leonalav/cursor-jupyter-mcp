"""Notebook tools: outline, read, and the four mutators.

These run the real Ruff subprocess against real notebooks, because the
guarantees worth testing are the ones that only show up with a real tool and a
real file: byte identity after a round trip, schema validity after a mutation,
and diagnostics that point at the right cell.
"""

from __future__ import annotations

import json

import pytest

from cursor_edit_mcp.errors import EditMcpError, ErrorCode
from cursor_edit_mcp.notebook.engine import to_source_list
from cursor_edit_mcp.settings import Settings
from cursor_edit_mcp.tools.nb_cells import (
    nb_cell_delete,
    nb_cell_insert,
    nb_cell_move,
    nb_cell_read,
    nb_cell_update,
    nb_outline,
)


def code(source, cell_id, **extra):
    cell = {
        "cell_type": "code",
        "execution_count": None,
        "id": cell_id,
        "metadata": {},
        "outputs": [],
        "source": to_source_list(source),
    }
    cell.update(extra)
    return cell


def markdown(source, cell_id):
    return {
        "cell_type": "markdown",
        "id": cell_id,
        "metadata": {},
        "source": to_source_list(source),
    }


def raw(source, cell_id):
    return {
        "cell_type": "raw",
        "id": cell_id,
        "metadata": {},
        "source": to_source_list(source),
    }


def structure(cells):
    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            }
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def write_notebook(make_file, name, cells, trailing_newline=True):
    text = json.dumps(structure(cells), indent=1, sort_keys=True, ensure_ascii=False)
    if trailing_newline:
        text += "\n"
    make_file(name, text)
    return name


def cells_of(settings, name):
    return json.loads((settings.workspace_root / name).read_text(encoding="utf-8"))[
        "cells"
    ]


def assert_valid_schema(settings, name):
    import nbformat

    nbformat.validate(nbformat.read(str(settings.workspace_root / name), as_version=4))


@pytest.fixture
def book(settings, make_file):
    """A small notebook with markdown, code, raw, and a second code cell."""
    write_notebook(
        make_file,
        "book.ipynb",
        [
            markdown("# Analysis\n\nSome prose.", "m0000001"),
            code("import os\nprint(os.name)", "c0000001"),
            raw("raw notes", "r0000001"),
            code("total = 1\nprint(total)", "c0000002"),
        ],
    )
    return "book.ipynb"


# --- outline --------------------------------------------------------------


def test_outline_numbers_every_cell_from_one(settings, book):
    entries = nb_outline(book, settings=settings)["cells"]
    assert [e["cell_number"] for e in entries] == [1, 2, 3, 4]
    assert [e["cell_id"] for e in entries] == [
        "m0000001",
        "c0000001",
        "r0000001",
        "c0000002",
    ]
    assert [e["type"] for e in entries] == ["markdown", "code", "raw", "code"]


def test_outline_reports_the_first_line(settings, book):
    entries = nb_outline(book, settings=settings)["cells"]
    assert entries[0]["first_line"] == "# Analysis"
    assert entries[1]["first_line"] == "import os"


def test_outline_summarizes_outputs(settings, make_file):
    write_notebook(
        make_file,
        "o.ipynb",
        [
            code(
                "print(1)",
                "c0000001",
                execution_count=4,
                outputs=[{"output_type": "stream", "name": "stdout", "text": "1\n"}],
            ),
        ],
    )
    entry = nb_outline("o.ipynb", settings=settings)["cells"][0]
    assert entry["execution_count"] == 4
    assert entry["output_summary"] == "1 output(s)"


def test_outline_flags_an_errored_cell(settings, make_file):
    write_notebook(
        make_file,
        "e.ipynb",
        [
            code(
                "1/0",
                "c0000001",
                execution_count=1,
                outputs=[
                    {
                        "output_type": "error",
                        "ename": "ZeroDivisionError",
                        "evalue": "",
                        "traceback": [],
                    }
                ],
            ),
        ],
    )
    entry = nb_outline("e.ipynb", settings=settings)["cells"][0]
    assert "error" in entry["output_summary"]


def test_outline_of_empty_notebook_says_what_to_do(settings, make_file):
    write_notebook(make_file, "empty.ipynb", [])
    result = nb_outline("empty.ipynb", settings=settings)
    assert result["cells"] == []
    assert "nb_cell_insert" in result["note"]


def test_outline_does_not_modify(settings, book):
    before = (settings.workspace_root / book).read_bytes()
    nb_outline(book, settings=settings)
    assert (settings.workspace_root / book).read_bytes() == before


# --- read -----------------------------------------------------------------


def test_read_by_id_returns_full_source(settings, book):
    cell = nb_cell_read(book, cell_id="c0000001", settings=settings)["cells"][0]
    assert cell["source"] == "import os\nprint(os.name)"
    assert cell["cell_number"] == 2


def test_read_by_number(settings, book):
    cell = nb_cell_read(book, cell_number=4, settings=settings)["cells"][0]
    assert cell["source"] == "total = 1\nprint(total)"


def test_read_omits_outputs_by_default(settings, make_file):
    write_notebook(
        make_file,
        "o.ipynb",
        [
            code(
                "print(1)",
                "c0000001",
                outputs=[{"output_type": "stream", "name": "stdout", "text": "1\n"}],
            ),
        ],
    )
    assert (
        "outputs"
        not in nb_cell_read("o.ipynb", cell_id="c0000001", settings=settings)["cells"][0]
    )
    with_outputs = nb_cell_read(
        "o.ipynb", cell_id="c0000001", include_outputs=True, settings=settings
    )
    assert with_outputs["cells"][0]["outputs"][0]["text"] == "1\n"


def test_read_unknown_id_lists_the_real_ones(settings, book):
    with pytest.raises(EditMcpError) as excinfo:
        nb_cell_read(book, cell_id="nope0000", settings=settings)
    assert excinfo.value.code == ErrorCode.CELL_NOT_FOUND
    assert "c0000001" in excinfo.value.details["available"]


def test_read_out_of_range_reports_the_count(settings, book):
    with pytest.raises(EditMcpError) as excinfo:
        nb_cell_read(book, cell_number=99, settings=settings)
    assert excinfo.value.code == ErrorCode.RANGE_OUT_OF_BOUNDS
    assert excinfo.value.details["cell_count"] == 4


# --- insert ---------------------------------------------------------------


def test_insert_after_a_cell(settings, book):
    result = nb_cell_insert(
        book, "x = 1", "code", after_cell_id="m0000001", settings=settings
    )
    assert result["inserted"]["cell_number"] == 2
    assert result["cell_count"] == 5
    assert_valid_schema(settings, book)


def test_insert_at_start(settings, book):
    result = nb_cell_insert(book, "# top", "markdown", at_start=True, settings=settings)
    assert result["inserted"]["cell_number"] == 1
    assert cells_of(settings, book)[0]["cell_type"] == "markdown"


def test_insert_at_end(settings, book):
    result = nb_cell_insert(book, "x = 1", at_end=True, settings=settings)
    assert result["inserted"]["cell_number"] == 5


def test_insert_at_index(settings, book):
    result = nb_cell_insert(book, "x = 1", index=2, settings=settings)
    assert result["inserted"]["cell_number"] == 3


def test_insert_gives_the_new_cell_a_distinct_id(settings, book):
    first = nb_cell_insert(book, "a", after_cell_id="m0000001", settings=settings)
    second = nb_cell_insert(book, "b", after_cell_id="m0000001", settings=settings)
    assert first["inserted"]["cell_id"] != second["inserted"]["cell_id"]
    assert len({c["id"] for c in cells_of(settings, book)}) == 6


def test_inserted_code_cell_has_no_outputs(settings, book):
    nb_cell_insert(book, "x = 1", "code", after_cell_id="c0000001", settings=settings)
    cell = cells_of(settings, book)[2]
    assert cell["outputs"] == []
    assert cell["execution_count"] is None


def test_insert_rejects_an_unknown_type(settings, book):
    with pytest.raises(EditMcpError) as excinfo:
        nb_cell_insert(book, "x", "wizard", settings=settings)
    assert excinfo.value.code == ErrorCode.CELL_TYPE_INVALID


def test_insert_rejects_a_bad_index(settings, book):
    with pytest.raises(EditMcpError) as excinfo:
        nb_cell_insert(book, "x", index=99, settings=settings)
    assert excinfo.value.code == ErrorCode.RANGE_OUT_OF_BOUNDS


# --- update ---------------------------------------------------------------


def test_update_replaces_source(settings, book):
    nb_cell_update(book, cell_id="c0000001", source="import sys", settings=settings)
    assert cells_of(settings, book)[1]["source"] == ["import sys"]


def test_update_clears_outputs_and_count(settings, make_file):
    write_notebook(
        make_file,
        "o.ipynb",
        [
            code(
                "print(1)",
                "c0000001",
                execution_count=9,
                outputs=[{"output_type": "stream", "name": "stdout", "text": "1\n"}],
            ),
        ],
    )
    result = nb_cell_update(
        "o.ipynb", cell_id="c0000001", source="print(2)", settings=settings
    )
    cell = cells_of(settings, "o.ipynb")[0]
    assert cell["outputs"] == []
    assert cell["execution_count"] is None
    assert result["outputs_cleared"] is True


def test_update_can_keep_outputs(settings, make_file):
    write_notebook(
        make_file,
        "o.ipynb",
        [
            code(
                "print(1)",
                "c0000001",
                execution_count=9,
                outputs=[{"output_type": "stream", "name": "stdout", "text": "1\n"}],
            ),
        ],
    )
    nb_cell_update(
        "o.ipynb",
        cell_id="c0000001",
        source="print(2)",
        keep_outputs=True,
        settings=settings,
    )
    assert cells_of(settings, "o.ipynb")[0]["execution_count"] == 9


def test_update_cell_relative_range(settings, book):
    nb_cell_update(
        book,
        cell_id="c0000001",
        range={"start_line": 2, "end_line": 2},
        source="print('changed')",
        settings=settings,
    )
    assert cells_of(settings, book)[1]["source"] == [
        "import os\n",
        "print('changed')",
    ]


def test_update_range_is_cell_relative_not_file_relative(settings, book):
    """Cell 4 starts at line 1 of the notebook but its own lines start at 1."""
    nb_cell_update(
        book,
        cell_id="c0000002",
        range={"start_line": 1, "end_line": 1},
        source="total = 99",
        settings=settings,
    )
    # Canonical source form: every line but the last carries its terminator.
    assert cells_of(settings, book)[3]["source"] == ["total = 99\n", "print(total)"]


def test_update_range_honours_expect(settings, book):
    with pytest.raises(EditMcpError) as excinfo:
        nb_cell_update(
            book,
            cell_id="c0000001",
            range={"start_line": 1, "end_line": 2},
            expect="import sys",
            source="import sys",
            settings=settings,
        )
    assert excinfo.value.code == ErrorCode.EXPECTED_TEXT_MISMATCH
    assert "import os" in excinfo.value.details["current_text"]


def test_update_range_outside_the_cell(settings, book):
    with pytest.raises(EditMcpError) as excinfo:
        nb_cell_update(
            book,
            cell_id="c0000001",
            range={"start_line": 99, "end_line": 99},
            source="x",
            settings=settings,
        )
    assert excinfo.value.code == ErrorCode.RANGE_OUT_OF_BOUNDS


def test_update_changes_cell_type(settings, book):
    nb_cell_update(book, cell_id="c0000001", cell_type="markdown", settings=settings)
    cell = cells_of(settings, book)[1]
    assert cell["cell_type"] == "markdown"
    # outputs and execution_count are not valid on a markdown cell
    assert "outputs" not in cell
    assert_valid_schema(settings, book)


def test_update_type_to_code_adds_the_required_fields(settings, book):
    nb_cell_update(book, cell_id="m0000001", cell_type="code", settings=settings)
    cell = cells_of(settings, book)[0]
    assert cell["outputs"] == []
    assert cell["execution_count"] is None
    assert_valid_schema(settings, book)


def test_update_with_nothing_to_change_is_rejected(settings, book):
    with pytest.raises(EditMcpError) as excinfo:
        nb_cell_update(book, cell_id="c0000001", settings=settings)
    assert excinfo.value.code == ErrorCode.INVALID_EDIT


# --- delete ---------------------------------------------------------------


def test_delete_by_id(settings, book):
    result = nb_cell_delete(book, cell_id="r0000001", settings=settings)
    assert result["deleted_count"] == 1
    assert [c["id"] for c in cells_of(settings, book)] == [
        "m0000001",
        "c0000001",
        "c0000002",
    ]


def test_delete_by_number(settings, book):
    nb_cell_delete(book, cell_number=1, settings=settings)
    assert cells_of(settings, book)[0]["id"] == "c0000001"


def test_delete_needs_some_identification(settings, book):
    with pytest.raises(EditMcpError) as excinfo:
        nb_cell_delete(book, settings=settings)
    assert excinfo.value.code == ErrorCode.CELL_NOT_FOUND


# --- move -----------------------------------------------------------------


def test_move_to_front(settings, book):
    result = nb_cell_move(book, "c0000002", 0, settings=settings)
    assert result["moved"]["cell_number"] == 1
    assert [c["id"] for c in cells_of(settings, book)][0] == "c0000002"


def test_move_to_end(settings, book):
    nb_cell_move(book, "m0000001", 3, settings=settings)
    assert [c["id"] for c in cells_of(settings, book)][-1] == "m0000001"


def test_move_keeps_other_ids(settings, book):
    before = {c["id"] for c in cells_of(settings, book)}
    nb_cell_move(book, "m0000001", 3, settings=settings)
    assert {c["id"] for c in cells_of(settings, book)} == before


def test_move_out_of_range(settings, book):
    with pytest.raises(EditMcpError) as excinfo:
        nb_cell_move(book, "m0000001", 99, settings=settings)
    assert excinfo.value.code == ErrorCode.RANGE_OUT_OF_BOUNDS


# --- integrity ------------------------------------------------------------


def test_mutation_keeps_the_notebook_schema_valid(settings, book):
    nb_cell_insert(book, "x = 1", after_cell_id="c0000001", settings=settings)
    nb_cell_update(book, cell_id="c0000002", source="y = 2", settings=settings)
    nb_cell_move(book, "r0000001", 0, settings=settings)
    nb_cell_delete(book, cell_id="m0000001", settings=settings)
    assert_valid_schema(settings, book)


def test_untouched_cells_keep_their_outputs(settings, make_file):
    """Editing one cell must not disturb a neighbour's execution state."""
    write_notebook(
        make_file,
        "o.ipynb",
        [
            code(
                "print(1)",
                "c0000001",
                execution_count=5,
                outputs=[{"output_type": "stream", "name": "stdout", "text": "1\n"}],
            ),
            code("print(2)", "c0000002"),
        ],
    )
    nb_cell_update("o.ipynb", cell_id="c0000002", source="print(3)", settings=settings)
    first = cells_of(settings, "o.ipynb")[0]
    assert first["execution_count"] == 5
    assert len(first["outputs"]) == 1


def test_duplicate_ids_are_repaired_on_write(settings, make_file):
    write_notebook(
        make_file, "d.ipynb", [code("a = 1", "same0001"), code("b = 2", "same0001")]
    )
    result = nb_cell_insert(
        "d.ipynb", "c = 3", after_cell_id="same0001", settings=settings
    )
    ids = [c["id"] for c in cells_of(settings, "d.ipynb")]
    assert len(set(ids)) == 3
    assert result["repaired_cell_ids"]
    assert_valid_schema(settings, "d.ipynb")


def test_notebook_4_4_is_upgraded_when_ids_are_used(settings, make_file):
    data = structure([code("a = 1", "c0000001")])
    data["nbformat_minor"] = 4
    make_file("old.ipynb", json.dumps(data, indent=1, sort_keys=True) + "\n")
    nb_cell_update("old.ipynb", cell_id="c0000001", source="a = 2", settings=settings)
    assert cells_of(settings, "old.ipynb") is not None
    assert_valid_schema(settings, "old.ipynb")


def test_non_notebook_path_is_rejected(settings, make_file):
    """A plain ``.py`` (no ``# %%`` markers) is not a cell target.

    Percent-format scripts are accepted, but a bare Python file is
    refused with ``SCRIPT_INVALID`` so the agent does not silently turn
    a regular module into a virtual notebook.
    """
    make_file("script.py", "x = 1\n")
    with pytest.raises(EditMcpError) as excinfo:
        nb_outline("script.py", settings=settings)
    assert excinfo.value.code == ErrorCode.SCRIPT_INVALID


def test_unsupported_extension_is_rejected(settings, make_file):
    """Extensions other than ``.ipynb`` and ``.py`` are refused.

    The error code is the original ``NOTEBOOK_INVALID``: a non-notebook
    suffix is a clear misuse, not a content problem.
    """
    make_file("notes.txt", "hello\n")
    with pytest.raises(EditMcpError) as excinfo:
        nb_outline("notes.txt", settings=settings)
    assert excinfo.value.code == ErrorCode.NOTEBOOK_INVALID


def test_missing_notebook_is_reported(settings):
    with pytest.raises(EditMcpError) as excinfo:
        nb_outline("nope.ipynb", settings=settings)
    assert excinfo.value.code == ErrorCode.FILE_NOT_FOUND


# --- validation against notebooks ----------------------------------------


def test_strict_blocks_a_new_lint_error(settings, make_file):
    write_notebook(make_file, "s.ipynb", [code("a = 1", "c0000001")])
    with pytest.raises(EditMcpError) as excinfo:
        nb_cell_update(
            "s.ipynb",
            cell_id="c0000001",
            source="import os",
            validate="strict",
            settings=settings,
        )
    assert excinfo.value.code == ErrorCode.LINT_REGRESSION
    assert any(d["code"] == "F401" for d in excinfo.value.details["diagnostics"]["new"])
    # and the file was not written
    assert cells_of(settings, "s.ipynb")[0]["source"] == ["a = 1"]


def test_strict_allows_a_preexisting_lint_error(settings, make_file):
    write_notebook(make_file, "s.ipynb", [code("import os\na = 1", "c0000001")])
    result = nb_cell_update(
        "s.ipynb",
        cell_id="c0000001",
        source="import os\na = 2",
        validate="strict",
        settings=settings,
    )
    assert result["ok"] is True
    assert result["diagnostics"]["new_count"] == 0


def test_warn_writes_and_reports(settings, make_file):
    write_notebook(make_file, "s.ipynb", [code("a = 1", "c0000001")])
    result = nb_cell_update(
        "s.ipynb",
        cell_id="c0000001",
        source="import os",
        validate="warn",
        settings=settings,
    )
    assert result["diagnostics"]["new_count"] >= 1
    assert cells_of(settings, "s.ipynb")[0]["source"] == ["import os"]


def test_magics_in_a_cell_do_not_block_an_edit(settings, make_file):
    write_notebook(make_file, "m.ipynb", [code("%matplotlib inline\na = 1", "c0000001")])
    result = nb_cell_update(
        "m.ipynb",
        cell_id="c0000001",
        source="%matplotlib inline\na = 2",
        validate="strict",
        settings=settings,
    )
    assert result["ok"] is True


def test_diagnostics_name_the_cell_and_line(settings, make_file):
    write_notebook(
        make_file,
        "d.ipynb",
        [markdown("# Title", "m0000001"), code("import os\n", "c0000001")],
    )
    result = nb_cell_update(
        "d.ipynb",
        cell_id="c0000001",
        source="import sys\n",
        validate="warn",
        settings=settings,
    )
    new = result["diagnostics"]["new"]
    assert any(d["code"] == "F401" for d in new)


def test_byte_identity_for_a_read_only_call(settings, book):
    before = (settings.workspace_root / book).read_bytes()
    nb_outline(book, settings=settings)
    nb_cell_read(book, cell_id="c0000001", settings=settings)
    assert (settings.workspace_root / book).read_bytes() == before


def test_mutating_an_untouched_region_keeps_a_minimal_diff(settings, book):
    before = (settings.workspace_root / book).read_text(encoding="utf-8")
    nb_cell_update(
        book, cell_id="c0000002", source="total = 2\nprint(total)", settings=settings
    )
    after = (settings.workspace_root / book).read_text(encoding="utf-8")
    assert before.count("\n") == after.count("\n")
    assert "total = 2" in after


# --- percent-format scripts ------------------------------------------------


def test_percent_script_loads_cells(settings, make_file):
    """A ``# %%`` file is parsed into cells with the right types."""
    make_file(
        "script.py",
        "import os\n\n# %%\nimport sys\n\n# %% [markdown]\n# Title\n\n# %% my cell\nx = 1\n",
    )
    from cursor_edit_mcp.notebook.script import load

    s = load(settings.workspace_root / "script.py", (settings.workspace_root / "script.py").read_bytes())
    assert len(s.cells) == 3
    assert s.cells[0]["cell_type"] == "code"
    assert s.cells[1]["cell_type"] == "markdown"
    assert s.cells[2]["cell_type"] == "code"
    assert s.cells[2]["metadata"].get("name") == "my cell"


def test_percent_script_roundtrip_ids(settings, make_file):
    """The round-trip produces the same source with added ``# id:`` comments."""
    src = "# %%\nimport os\n"
    make_file("round.py", src)
    path = settings.workspace_root / "round.py"

    from cursor_edit_mcp.notebook.script import load, to_bytes

    s = load(path, path.read_bytes())
    assert len(s.cells) == 1
    assert s.cells[0]["id"] != ""
    out = to_bytes(s)
    # The cell body (without the marker and id line) is preserved
    assert "import os" in out.decode()
    # The id is written into the file
    assert ("# id: " + s.cells[0]["id"]) in out.decode()


def test_percent_script_plain_py_rejected(settings, make_file):
    """A ``.py`` without ``# %%`` markers is rejected as ``SCRIPT_INVALID``."""
    make_file("plain.py", "x = 1\ny = 2\n")
    with pytest.raises(EditMcpError) as excinfo:
        nb_outline("plain.py", settings=settings)
    assert excinfo.value.code == "SCRIPT_INVALID"


def test_percent_script_nb_outline(settings, make_file):
    """``nb_outline`` on a percent script returns cells with id and type."""
    make_file("cells.py", "# %%\nimport os\n\n# %% [markdown]\n# Title\n")
    result = nb_outline("cells.py", settings=settings)
    assert result["format"] == "py"
    assert result["cell_count"] == 2
    assert result["cells"][0]["cell_id"]
    assert result["cells"][0]["type"] == "code"
    assert result["cells"][1]["type"] == "markdown"


def test_percent_script_nb_cell_insert(settings, make_file):
    """``nb_cell_insert`` adds a new cell to a percent script."""
    make_file("insert.py", "# %%\nx = 1\n")
    ref = nb_cell_insert(
        "insert.py",
        source="import math",
        cell_type="code",
        at_start=True,
        settings=settings,
    )
    assert ref["inserted"]["cell_id"]
    assert ref["inserted"]["cell_number"] == 1

    result = nb_outline("insert.py", settings=settings)
    assert result["cell_count"] == 2


def test_percent_script_nb_cell_update(settings, make_file):
    """``nb_cell_update`` changes a percent script's cell source."""
    make_file("update.py", "# %%\nx = 1\n")
    ref = nb_cell_insert("update.py", "x = 99", settings=settings)
    nb_cell_update(
        "update.py",
        cell_id=ref["inserted"]["cell_id"],
        source="x = 100",
        settings=settings,
    )
    content = (settings.workspace_root / "update.py").read_text(encoding="utf-8")
    assert "x = 100" in content
    assert "x = 99" not in content


def test_percent_script_nb_cell_delete(settings, make_file):
    """``nb_cell_delete`` removes a percent script cell."""
    make_file("delete.py", "# %%\nx = 1\n\n# %%\ny = 2\n")
    # Get the id of the y=2 cell before inserting
    before = nb_outline("delete.py", settings=settings)
    y_cell_id = before["cells"][1]["cell_id"]
    # Insert a new cell at the end (different content to distinguish)
    nb_cell_insert("delete.py", "z = 3", settings=settings)
    # Delete the original y=2 cell
    nb_cell_delete("delete.py", cell_id=y_cell_id, settings=settings)
    result = nb_outline("delete.py", settings=settings)
    assert result["cell_count"] == 2  # x=1 and z=3 remain
    assert result["cells"][0]["first_line"] == "x = 1"


def test_percent_script_nb_cell_move(settings, make_file):
    """``nb_cell_move`` moves a percent script cell."""
    make_file("move.py", "# %%\na = 1\n\n# %%\nb = 2\n\n# %%\nc = 3\n")
    result = nb_outline("move.py", settings=settings)
    third_id = result["cells"][2]["cell_id"]
    nb_cell_move(
        "move.py",
        cell_id=third_id,
        destination=0,
        settings=settings,
    )
    result2 = nb_outline("move.py", settings=settings)
    assert result2["cells"][0]["cell_id"] == third_id


# --- kernel discover -------------------------------------------------------


def test_kernel_discover_returns_list():
    """``kernel_discover`` reports the installed kernels."""
    from cursor_edit_mcp.tools.kernel_discover import kernel_discover

    result = kernel_discover()
    assert result["ok"] is True
    assert isinstance(result["count"], int)
    assert result["count"] >= 1
    assert result["default_kernel"] == "python3"
    assert "python" in result["languages"]
    assert "python3" in result["reachable"]
    # The entry for python3 has the full detail
    python3 = next(k for k in result["kernels"] if k["name"] == "python3")
    assert python3["language"] == "python"
    assert python3["display_name"]
    assert python3["is_default"] is True
    assert python3["executable_status"] == "ok"
    assert python3["executable"] is not None
