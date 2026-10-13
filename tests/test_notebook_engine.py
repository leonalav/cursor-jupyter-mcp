"""Notebook engine: serialization, integrity, and linting.

The governing rule throughout: a notebook is a document the user may have open
in Jupyter with unsaved work. Everything here exists to avoid corrupting it.
"""

from __future__ import annotations

import json

import pytest

from cursor_edit_mcp.errors import EditMcpError, ErrorCode
from cursor_edit_mcp.notebook import engine, linting
from cursor_edit_mcp.runners.base import Resolver


def make_notebook(cells=None, **metadata):
    return {
        "cells": cells if cells is not None else [],
        "metadata": metadata,
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def code(source, cell_id="c0000001", **extra):
    cell = {
        "cell_type": "code",
        "execution_count": None,
        "id": cell_id,
        "metadata": {},
        "outputs": [],
        "source": engine.to_source_list(source),
    }
    cell.update(extra)
    return cell


def markdown(source, cell_id="m0000001"):
    return {
        "cell_type": "markdown",
        "id": cell_id,
        "metadata": {},
        "source": engine.to_source_list(source),
    }


def raw(source, cell_id="r0000001"):
    return {
        "cell_type": "raw",
        "id": cell_id,
        "metadata": {},
        "source": engine.to_source_list(source),
    }


def serialize(data, trailing_newline=True):
    text = json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False)
    return (text + "\n").encode("utf-8") if trailing_newline else text.encode("utf-8")


def load_bytes(data, name="n.ipynb", trailing_newline=True, tmp_path=None):
    raw_bytes = serialize(data, trailing_newline)
    path = tmp_path / name
    path.write_bytes(raw_bytes)
    return engine.load(path, raw_bytes)


# --- byte identity --------------------------------------------------------


def test_round_trip_is_byte_identical(tmp_path):
    data = make_notebook(
        [
            code("import os\nprint(os)", execution_count=2),
            markdown("# Title\ntext"),
        ],
        kernelspec={
            "display_name": "Python 3",
            "language": "python",
            "name": "python3",
        },
    )
    loaded = load_bytes(data, tmp_path=tmp_path)
    assert engine.to_bytes(loaded) == serialize(data)


def test_round_trip_without_trailing_newline(tmp_path):
    """The file's own convention wins over a convention we impose."""
    data = make_notebook([code("x = 1")])
    loaded = load_bytes(data, trailing_newline=False, tmp_path=tmp_path)
    assert engine.to_bytes(loaded) == serialize(data, trailing_newline=False)


def test_round_trip_preserves_unicode(tmp_path):
    data = make_notebook([markdown("# 日本語 — café\n")])
    loaded = load_bytes(data, tmp_path=tmp_path)
    assert engine.to_bytes(loaded) == serialize(data)


# --- preservation ---------------------------------------------------------


def test_unknown_cell_fields_survive_and_are_reported(tmp_path):
    cell = code("x = 1")
    cell["vendor_field"] = "keep me"
    data = make_notebook([cell])
    loaded = load_bytes(data, tmp_path=tmp_path)

    out = json.loads(engine.to_bytes(loaded).decode("utf-8"))
    assert out["cells"][0]["vendor_field"] == "keep me"
    assert "vendor_field" in loaded.unknown_fields


def test_attachments_survive(tmp_path):
    cell = markdown("# img")
    cell["attachments"] = {"i.png": {"image/png": "AAAA"}}
    loaded = load_bytes(make_notebook([cell]), tmp_path=tmp_path)
    out = json.loads(engine.to_bytes(loaded).decode("utf-8"))
    assert out["cells"][0]["attachments"] == {"i.png": {"image/png": "AAAA"}}


def test_outputs_survive(tmp_path):
    cell = code(
        "print(1)",
        execution_count=7,
        outputs=[{"output_type": "stream", "name": "stdout", "text": "1\n"}],
    )
    loaded = load_bytes(make_notebook([cell]), tmp_path=tmp_path)
    out = json.loads(engine.to_bytes(loaded).decode("utf-8"))
    assert out["cells"][0]["execution_count"] == 7
    assert out["cells"][0]["outputs"][0]["text"] == "1\n"


def test_metadata_survives(tmp_path):
    cell = code("x = 1")
    cell["metadata"] = {"tags": ["hide-input"], "custom": {"a": 1}}
    loaded = load_bytes(make_notebook([cell]), tmp_path=tmp_path)
    out = json.loads(engine.to_bytes(loaded).decode("utf-8"))
    assert out["cells"][0]["metadata"]["tags"] == ["hide-input"]
    assert out["cells"][0]["metadata"]["custom"] == {"a": 1}


def test_notebook_metadata_survives(tmp_path):
    data = make_notebook([code("x=1")], custom_vendor={"deep": [1, 2]})
    loaded = load_bytes(data, tmp_path=tmp_path)
    out = json.loads(engine.to_bytes(loaded).decode("utf-8"))
    assert out["metadata"]["custom_vendor"] == {"deep": [1, 2]}


# --- source canonicalization ---------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a\nb\n", ["a\n", "b\n"]),
        ("a\nb", ["a\n", "b"]),
        (["a\n", "b"], ["a\n", "b"]),
        (["a\nb\n"], ["a\nb\n"]),
        ("", []),
        (None, []),
    ],
)
def test_source_normalization(value, expected):
    assert engine.to_source_list(value) == expected


def test_source_text_round_trips():
    for text in ("a\nb\n", "a\nb", "single"):
        assert engine.source_text(engine.to_source_list(text)) == text


# --- integrity ------------------------------------------------------------


def test_bom_is_tolerated(tmp_path):
    data = serialize(make_notebook([code("x=1")]))
    path = tmp_path / "n.ipynb"
    path.write_bytes(b"\xef\xbb\xbf" + data)
    loaded = engine.load(path, b"\xef\xbb\xbf" + data)
    assert len(loaded.cells) == 1


def test_invalid_json_is_rejected(tmp_path):
    path = tmp_path / "n.ipynb"
    path.write_bytes(b"{not json")
    with pytest.raises(EditMcpError) as excinfo:
        engine.load(path, b"{not json")
    assert excinfo.value.code == ErrorCode.NOTEBOOK_INVALID


def test_json_without_cells_is_rejected(tmp_path):
    raw_bytes = b'{"metadata": {}, "nbformat": 4}'
    path = tmp_path / "n.ipynb"
    path.write_bytes(raw_bytes)
    with pytest.raises(EditMcpError) as excinfo:
        engine.load(path, raw_bytes)
    assert excinfo.value.code == ErrorCode.NOTEBOOK_INVALID


def test_structurally_invalid_notebook_is_rejected(tmp_path):
    """A bad kernelspec is a real error, unlike an unknown field."""
    data = make_notebook([code("x=1")], kernelspec={"name": "python3"})
    with pytest.raises(EditMcpError) as excinfo:
        load_bytes(data, tmp_path=tmp_path)
    assert excinfo.value.code == ErrorCode.NOTEBOOK_INVALID


# --- cell ids -------------------------------------------------------------


def test_repair_makes_ids_unique():
    cells = [{"id": "aaaaaaaa"}, {"id": "aaaaaaaa"}, {"id": "bbbbbbbb"}]
    assigned = engine.repair_cell_ids(cells)
    ids = [c["id"] for c in cells]
    assert len(set(ids)) == 3
    assert len(assigned) == 1


def test_repair_generates_missing_ids():
    cells = [{"cell_type": "code"}, {"id": ""}, {"id": "good1234"}]
    assigned = engine.repair_cell_ids(cells)
    # The two cells that had no usable id are reported, so the caller can say
    # which cells gained new identities.
    assert len(assigned) == 2
    assert all(c["id"] for c in cells)
    assert len({c["id"] for c in cells}) == 3


def test_repair_leaves_good_ids_alone():
    cells = [{"id": "aaaaaaaa"}, {"id": "bbbbbbbb"}]
    assert engine.repair_cell_ids(cells) == ()
    assert [c["id"] for c in cells] == ["aaaaaaaa", "bbbbbbbb"]


def test_generated_ids_are_eight_chars():
    assert len(engine.generate_cell_id(set())) == 8


def test_generated_id_avoids_collisions():
    taken = {engine.generate_cell_id(set())}
    assert engine.generate_cell_id(taken) not in taken


def test_raising_minor_version_when_ids_are_used():
    data = make_notebook([code("x=1")])
    data["nbformat_minor"] = 4
    assert engine.ensure_notebook_minor(data) is True
    assert data["nbformat_minor"] == 5


# --- magics ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected_first"),
    [
        ("%matplotlib inline\nx=1", "#matplotlib inline"),
        ("%%time\ny=2", "#%time"),
        ("!pip install pandas\n", "#pip install pandas"),
        ("%load_ext autoreload\n", "#load_ext autoreload"),
    ],
)
def test_magics_are_neutralized(source, expected_first):
    out, changed = linting.neutralize_magics(source)
    assert changed is True
    assert out.split("\n")[0].strip().startswith(expected_first.strip())


def test_magic_neutralization_preserves_line_count():
    source = "%time\na = 1\n%%capture\nb = 2"
    out, _ = linting.neutralize_magics(source)
    assert len(out.split("\n")) == len(source.split("\n"))


def test_magic_neutralization_preserves_length():
    """Column alignment has to hold or diagnostics point at the wrong token."""
    for source in ("%matplotlib inline\nx=1", "!pip install pandas\ny=2"):
        out, _ = linting.neutralize_magics(source)
        assert len(out.split("\n")[0]) == len(source.split("\n")[0])


def test_shebang_is_not_created_from_a_bang_magic():
    """`#!pip` reads as a misplaced shebang and produces a false EXE005."""
    out, _ = linting.neutralize_magics("!pip install x\n")
    assert not out.startswith("#!")


def test_ordinary_code_is_untouched():
    source = "a = 1\n# a comment\n"
    out, changed = linting.neutralize_magics(source)
    assert changed is False
    assert out == source


def test_percent_inside_a_string_is_not_a_magic():
    out, changed = linting.neutralize_magics('s = "%not a magic"\n')
    assert changed is False


# --- notebook linting -----------------------------------------------------


@pytest.fixture
def resolver(settings):
    return Resolver(settings.workspace_root)


def test_diagnostics_map_to_the_right_cell(settings, resolver):
    """Markdown and raw cells must keep their place in the numbering."""
    data = make_notebook(
        [
            markdown("# Title"),
            code("import os\nx = 1\n"),
            raw("some raw text"),
            code("import sys\n"),
        ]
    )
    diags = linting.lint(
        data, path=settings.workspace_root / "n.ipynb", resolver=resolver
    )
    by_cell = {d.cell_number: d for d in diags if d.code == "F401"}
    assert set(by_cell) == {2, 4}
    assert by_cell[2].cell_id == "c0000001"
    assert by_cell[4].cell_id == "c0000001"  # same default id, different cell number


def test_diagnostics_carry_cell_relative_lines(settings, resolver):
    data = make_notebook([code("a = 1\nimport os\n")])
    diags = linting.lint(
        data, path=settings.workspace_root / "n.ipynb", resolver=resolver
    )
    f401 = next(d for d in diags if d.code == "F401")
    assert f401.line == 2
    assert f401.source_line == "import os"


def test_magics_do_not_produce_syntax_errors(settings, resolver):
    data = make_notebook(
        [
            code("%matplotlib inline\nimport os\n"),
            code("!pip install x\n%%time\nimport sys\n"),
        ]
    )
    diags = linting.lint(
        data, path=settings.workspace_root / "n.ipynb", resolver=resolver
    )
    assert not [d for d in diags if d.is_syntax]


def test_markdown_cells_are_never_linted(settings, resolver):
    data = make_notebook([markdown("# bad `fence\nimport os\n")])
    assert (
        linting.lint(data, path=settings.workspace_root / "n.ipynb", resolver=resolver)
        == ()
    )


def test_raw_cells_are_never_linted(settings, resolver):
    data = make_notebook([raw("import os\n")])
    assert (
        linting.lint(data, path=settings.workspace_root / "n.ipynb", resolver=resolver)
        == ()
    )


def test_no_code_cells_yields_nothing(settings, resolver):
    data = make_notebook([markdown("# only prose")])
    assert (
        linting.lint(data, path=settings.workspace_root / "n.ipynb", resolver=resolver)
        == ()
    )


def test_cell_diagnostic_fingerprint_ignores_cell_number():
    """Inserting a cell must not make the whole notebook look newly broken."""
    first = linting.CellDiagnostic(
        cell_id="abc",
        cell_number=1,
        line=1,
        column=1,
        code="F401",
        message="m",
        severity="error",
        source_line="import os",
    )
    later = linting.CellDiagnostic(
        cell_id="abc",
        cell_number=9,
        line=1,
        column=1,
        code="F401",
        message="m",
        severity="error",
        source_line="import os",
    )
    result = linting.diff_cells((first,), (later,))
    assert result.new == ()


def test_cell_diagnostic_fingerprint_tracks_content():
    before = linting.CellDiagnostic(
        cell_id="abc",
        cell_number=1,
        line=1,
        column=1,
        code="F401",
        message="m",
        severity="error",
        source_line="import os",
    )
    after = linting.CellDiagnostic(
        cell_id="abc",
        cell_number=1,
        line=1,
        column=1,
        code="F401",
        message="m",
        severity="error",
        source_line="import sys",
    )
    assert len(linting.diff_cells((before,), (after,)).new) == 1
