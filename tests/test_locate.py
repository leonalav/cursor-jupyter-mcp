"""Anchor and addressing tests for ``edit_locate``.

The recurring theme: a locator that returns the *wrong* range is worse than one
that fails, because the agent cannot tell the difference. Every test here pins
a line number against ``PY_SOURCE``/``MD_SOURCE`` below, whose layout is written
out explicitly so the numbers can be checked by eye:

PY_SOURCE   1 import os        11 (blank)    20 class Outer:
           2 (blank)           12     def delete_user(...)  21     class Inner:
           3 (blank)           13         os.remove(uid)    22         def method(self):
           4 class UserService:14 (blank)                       23             pass
           5     \"\"\"Service...\"\"\"15 (blank)
           6 (blank)           16 def fetch_user_standalone(uid):
           7     @staticmethod  17     return uid
           8     def fetch_user(...)
           9         \"\"\"Fetch...\"\"\"
          10         return {"id": uid}
"""

from __future__ import annotations

import pytest

from cursor_edit_mcp.errors import EditMcpError, ErrorCode
from cursor_edit_mcp.tools.edit_apply import EditSpec, edit_apply
from cursor_edit_mcp.tools.edit_locate import edit_locate

PY_SOURCE = '''import os


class UserService:
    """Service layer."""

    @staticmethod
    def fetch_user(uid: str) -> dict:
        """Fetch one user."""
        return {"id": uid}

    def delete_user(self, uid: str) -> None:
        os.remove(uid)


def fetch_user_standalone(uid):
    return uid


class Outer:
    class Inner:
        def method(self):
            pass
'''

MD_SOURCE = """# Title

Intro text.

## Installation

Run this first.

### Linux

apt install.

### macOS

brew install.

## Usage

Use it like so.

```python
# a comment inside a fence
## Not a heading either
```

More text.
"""


@pytest.fixture
def py_file(settings, make_file):
    make_file("app.py", PY_SOURCE)
    return "app.py"


@pytest.fixture
def md_file(settings, make_file):
    make_file("README.md", MD_SOURCE)
    return "README.md"


def apply_edit(path, edit, *, base_hash, settings):
    return edit_apply(
        path, [EditSpec.model_validate(edit)], base_hash=base_hash, settings=settings
    )


# --- text anchors ---------------------------------------------------------


def test_unique_text_anchor_returns_range(settings, py_file):
    result = edit_locate(py_file, text="os.remove(uid)", settings=settings)
    assert (result["start_line"], result["end_line"]) == (13, 13)


def test_anchor_spanning_lines_covers_them_all(settings, py_file):
    result = edit_locate(py_file, text='return {"id": uid}', settings=settings)
    assert (result["start_line"], result["end_line"]) == (10, 10)


def test_ambiguous_anchor_rejected_with_candidates(settings, py_file):
    with pytest.raises(EditMcpError) as excinfo:
        edit_locate(py_file, text="uid", settings=settings)

    error = excinfo.value
    assert error.code == ErrorCode.ANCHOR_NOT_FOUND
    assert "ambiguous" in error.hint
    assert len(error.details["candidates"]) > 1


def test_occurrence_selects_nth_match(settings, py_file):
    first = edit_locate(py_file, text="uid", occurrence=0, settings=settings)
    second = edit_locate(py_file, text="uid", occurrence=1, settings=settings)
    assert first["start_line"] < second["start_line"]


def test_occurrence_out_of_range_rejected(settings, py_file):
    with pytest.raises(EditMcpError) as excinfo:
        edit_locate(py_file, text="uid", occurrence=99, settings=settings)
    assert excinfo.value.code == ErrorCode.ANCHOR_NOT_FOUND


def test_missing_anchor_suggests_similar_lines(settings, py_file):
    with pytest.raises(EditMcpError) as excinfo:
        edit_locate(py_file, text="def fetch_userr(", settings=settings)
    candidates = excinfo.value.details["candidates"]
    assert any("fetch_user" in c["text"] for c in candidates)


# --- regex anchors --------------------------------------------------------


def test_regex_anchor_returns_range(settings, py_file):
    result = edit_locate(py_file, regex=r"def delete_user\(", settings=settings)
    assert result["start_line"] == 12


def test_regex_reports_first_capture_group(settings, py_file):
    result = edit_locate(py_file, regex=r"def (delete_user)\(", settings=settings)
    assert result["match"] == "delete_user"


def test_regex_with_multiple_matches_needs_occurrence(settings, py_file):
    with pytest.raises(EditMcpError) as excinfo:
        edit_locate(py_file, regex=r"def (\w+)\(", settings=settings)
    assert excinfo.value.code == ErrorCode.ANCHOR_NOT_FOUND


def test_invalid_regex_reports_clearly(settings, py_file):
    with pytest.raises(EditMcpError) as excinfo:
        edit_locate(py_file, regex=r"def (unclosed", settings=settings)
    assert excinfo.value.code == ErrorCode.INVALID_EDIT


# --- symbols --------------------------------------------------------------


def test_class_symbol_range_includes_body(settings, py_file):
    result = edit_locate(py_file, by="symbol", name="UserService", settings=settings)
    assert (result["start_line"], result["end_line"]) == (4, 13)


def test_method_symbol_includes_decorator(settings, py_file):
    result = edit_locate(
        py_file, by="symbol", name="UserService.fetch_user", settings=settings
    )
    assert result["start_line"] == 7  # the @staticmethod line, not the def
    assert result["end_line"] == 10


def test_bare_leaf_name_resolves(settings, py_file):
    result = edit_locate(
        py_file, by="symbol", name="fetch_user_standalone", settings=settings
    )
    assert result["start_line"] == 16


def test_nested_symbol_resolves(settings, py_file):
    result = edit_locate(
        py_file, by="symbol", name="Outer.Inner.method", settings=settings
    )
    assert (result["start_line"], result["end_line"]) == (22, 23)


def test_unknown_symbol_suggests_close_match(settings, py_file):
    with pytest.raises(EditMcpError) as excinfo:
        edit_locate(py_file, by="symbol", name="UserService.fetch_usr", settings=settings)
    assert excinfo.value.code == ErrorCode.ANCHOR_NOT_FOUND
    assert "fetch_user" in excinfo.value.hint


def test_symbol_mode_requires_name(settings, py_file):
    with pytest.raises(EditMcpError):
        edit_locate(py_file, by="symbol", settings=settings)


def test_symbol_in_syntax_error_file_reports_decode(settings, make_file):
    make_file("broken.py", "def oops(:\n")
    with pytest.raises(EditMcpError) as excinfo:
        edit_locate("broken.py", by="symbol", name="oops", settings=settings)
    assert excinfo.value.code == ErrorCode.DECODE_ERROR


# --- headings -------------------------------------------------------------


def test_heading_section_includes_subsections_by_default(settings, md_file):
    result = edit_locate(md_file, by="heading", text="Installation", settings=settings)
    assert (result["start_line"], result["end_line"]) == (5, 15)


def test_heading_section_can_exclude_subsections(settings, md_file):
    result = edit_locate(
        md_file,
        by="heading",
        text="Installation",
        include_subsections=False,
        settings=settings,
    )
    assert (result["start_line"], result["end_line"]) == (5, 7)


def test_heading_accepted_with_hashes(settings, md_file):
    result = edit_locate(md_file, by="heading", text="## Installation", settings=settings)
    assert result["start_line"] == 5


def test_heading_inside_code_fence_is_not_a_heading(settings, md_file):
    """A `#` inside a fence is code, not a heading."""
    with pytest.raises(EditMcpError) as excinfo:
        edit_locate(md_file, by="heading", text="Not a heading either", settings=settings)
    assert excinfo.value.code == ErrorCode.ANCHOR_NOT_FOUND


def test_heading_suggests_close_title(settings, md_file):
    with pytest.raises(EditMcpError) as excinfo:
        edit_locate(md_file, by="heading", text="Installaton", settings=settings)
    assert "Installation" in excinfo.value.hint


# --- shared behaviour -----------------------------------------------------


def test_locate_returns_hash_for_base_hash(settings, py_file):
    located = edit_locate(py_file, text="def fetch_user_standalone", settings=settings)
    assert located["hash"].startswith("sha256:")
    assert located["line_count"] == len(PY_SOURCE.rstrip("\n").split("\n"))


def test_replacing_a_decorated_symbol_drops_the_decorator_unless_reincluded(
    settings, py_file
):
    """The symbol range includes decorators, so new_text must carry them.

    This is the sharp edge of including decorators in the range: replacing a
    decorated function with text that omits the decorator silently removes it.
    The behaviour is intended; this test exists so the choice stays deliberate.
    """
    located = edit_locate(
        py_file, by="symbol", name="UserService.fetch_user", settings=settings
    )
    apply_edit(
        py_file,
        {
            "op": "replace",
            "range": {
                "start_line": located["start_line"],
                "end_line": located["end_line"],
            },
            "new_text": "def fetch_user(self, uid):\n    return uid",
        },
        base_hash=located["hash"],
        settings=settings,
    )
    body = (settings.workspace_root / py_file).read_text(encoding="utf-8")
    assert "@staticmethod" not in body

    # Re-including it restores the original shape.
    located = edit_locate(
        py_file, by="symbol", name="UserService.fetch_user", settings=settings
    )
    apply_edit(
        py_file,
        {
            "op": "replace",
            "range": {
                "start_line": located["start_line"],
                "end_line": located["end_line"],
            },
            "new_text": "@staticmethod\ndef fetch_user(self, uid):\n    return uid",
        },
        base_hash=located["hash"],
        settings=settings,
    )
    assert "@staticmethod" in (settings.workspace_root / py_file).read_text(
        encoding="utf-8"
    )


def test_locate_includes_text_on_request(settings, py_file):
    result = edit_locate(
        py_file,
        by="symbol",
        name="UserService.delete_user",
        include_text=True,
        settings=settings,
    )
    # The method's own margin is part of the text, so this is an exact slice.
    assert (
        result["text"]
        == "    def delete_user(self, uid: str) -> None:\n        os.remove(uid)"
    )


def test_locate_does_not_modify_the_file(settings, py_file):
    before = (settings.workspace_root / py_file).read_bytes()
    edit_locate(py_file, text="os.remove(uid)", settings=settings)
    assert (settings.workspace_root / py_file).read_bytes() == before


def test_locate_respects_the_path_jail(settings):
    with pytest.raises(EditMcpError) as excinfo:
        edit_locate("../../../etc/passwd", text="root", settings=settings)
    assert excinfo.value.code == ErrorCode.PATH_FORBIDDEN


def test_anchor_and_text_together_is_rejected(settings, py_file):
    with pytest.raises(EditMcpError):
        edit_locate(py_file, text="x", regex="y", settings=settings)
