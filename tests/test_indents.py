"""Indentation tests for ``indent_mode: relative``.

Relative indentation removes the common margin from ``new_text`` and re-applies
the target block's margin. The agent therefore writes internally-consistent
code from any starting column.
"""

from __future__ import annotations

import pytest

from cursor_edit_mcp.edit import indents
from cursor_edit_mcp.edit.indents import (
    common_margin,
    detect_style,
    reindent_to,
    strip_margin,
)
from cursor_edit_mcp.errors import EditMcpError
from cursor_edit_mcp.tools.edit_apply import EditSpec, edit_apply
from cursor_edit_mcp.tools.edit_locate import edit_locate

SVC = """class UserService:
    def fetch_user(self, uid):
        return {"id": uid}

    def delete_user(self, uid):
        pass
"""


def specs(*items) -> list[EditSpec]:
    return [EditSpec.model_validate(item) for item in items]


@pytest.fixture
def svc(settings, make_file):
    make_file("svc.py", SVC)
    return "svc.py"


# --- margin primitives ----------------------------------------------------


def test_common_margin_is_shared_prefix():
    assert common_margin("    a\n    b") == "    "


def test_common_margin_ignores_blank_lines():
    assert common_margin("    a\n\n    b") == "    "


def test_common_margin_of_flush_text_is_empty():
    assert common_margin("a\nb") == ""


def test_strip_margin_preserves_relative_depth():
    assert strip_margin("    a\n        b") == "a\n    b"


def test_detect_style_finds_two_space_project():
    lines = ["def f():", "  return 1"]
    assert detect_style(lines).width == 2


def test_detect_style_defaults_to_four():
    assert detect_style(["x = 1"]).width == 4


def test_reindent_applies_margin_preserving_relative():
    result = reindent_to("a\n  b", margin="    ")
    assert result == "    a\n      b"


def test_reindent_is_a_noop_for_blank_text():
    assert reindent_to("   \n  ", margin="  ") == "   \n  "


def test_detects_mixed_indentation():
    """Indented lines using both tabs and spaces, which Python rejects."""
    assert indents.detects_mixed_indentation("  a\n\tb") is True
    assert indents.detects_mixed_indentation("  a\n  b") is False
    assert indents.detects_mixed_indentation("\ta\n    b") is True


def test_uses_tabs_detects_tab_indented_file():
    assert indents.uses_tabs(["\tx = 1"]) is True
    assert indents.uses_tabs(["    x = 1"]) is False


# --- relative indent via edit_apply --------------------------------------


def test_relative_indent_lifts_flush_code_into_a_class(settings, svc):
    located = edit_locate(
        svc, by="symbol", name="UserService.fetch_user", settings=settings
    )
    edit_apply(
        svc,
        specs(
            {
                "op": "replace",
                "range": {
                    "start_line": located["start_line"],
                    "end_line": located["end_line"],
                },
                "new_text": 'def fetch_user(self, uid):\n    return {"id": uid}',
            }
        ),
        base_hash=located["hash"],
        settings=settings,
    )
    assert (settings.workspace_root / svc).read_text(encoding="utf-8") == SVC


def test_relative_indent_normalizes_a_constant_indent(settings, svc):
    """The agent writes from column 4; the result is unchanged."""
    located = edit_locate(
        svc, by="symbol", name="UserService.fetch_user", settings=settings
    )
    edit_apply(
        svc,
        specs(
            {
                "op": "replace",
                "range": {
                    "start_line": located["start_line"],
                    "end_line": located["end_line"],
                },
                "new_text": '    def fetch_user(self, uid):\n        return {"id": uid}',
            }
        ),
        base_hash=located["hash"],
        settings=settings,
    )
    assert (settings.workspace_root / svc).read_text(encoding="utf-8") == SVC


def test_absolute_indent_writes_verbatim(settings, svc):
    located = edit_locate(
        svc, by="symbol", name="UserService.delete_user", settings=settings
    )
    edit_apply(
        svc,
        specs(
            {
                "op": "replace",
                "range": {
                    "start_line": located["start_line"],
                    "end_line": located["end_line"],
                },
                "new_text": "def delete_user(self, uid):\npass",
            }
        ),
        base_hash=located["hash"],
        indent_mode="absolute",
        # This body is deliberately flush-left, which is what
        # indent_mode: absolute means. The result is invalid Python inside a
        # class; format is disabled so the formatter does not touch it and the
        # test isolates the indentation behaviour alone.
        validate="off",
        format="none",
        settings=settings,
    )
    assert (
        "def delete_user(self, uid):\npass" in (settings.workspace_root / svc).read_text()
    )


def test_relative_indent_at_top_level_uses_zero_margin(settings, make_file):
    make_file("top.py", "def alpha():\n    pass\n")
    located = edit_locate("top.py", by="symbol", name="alpha", settings=settings)
    edit_apply(
        "top.py",
        specs(
            {
                "op": "replace",
                "range": {
                    "start_line": located["start_line"],
                    "end_line": located["end_line"],
                },
                "new_text": "def alpha():\n    if True:\n        return 1",
            }
        ),
        base_hash=located["hash"],
        settings=settings,
    )
    assert (settings.workspace_root / "top.py").read_text(encoding="utf-8") == (
        "def alpha():\n    if True:\n        return 1\n"
    )


def test_relative_indent_of_insert_follows_neighbour(settings, svc):
    located = edit_locate(
        svc, by="symbol", name="UserService.delete_user", settings=settings
    )
    edit_apply(
        svc,
        specs(
            {
                "op": "insert",
                "after_line": located["start_line"] - 1,
                "new_text": "def helper(self):\n    return None",
            }
        ),
        base_hash=located["hash"],
        settings=settings,
    )
    body = (settings.workspace_root / svc).read_text(encoding="utf-8")
    assert "\n    def helper(self):\n        return None\n" in body


def test_unknown_indent_mode_rejected(settings, svc):
    located = edit_locate(
        svc, by="symbol", name="UserService.delete_user", settings=settings
    )
    with pytest.raises(EditMcpError) as excinfo:
        edit_apply(
            svc,
            specs(
                {
                    "op": "replace",
                    "range": {
                        "start_line": located["start_line"],
                        "end_line": located["end_line"],
                    },
                    "new_text": "x",
                }
            ),
            base_hash=located["hash"],
            indent_mode="magic",
            settings=settings,
        )
    assert excinfo.value.code == "INVALID_EDIT"


def test_relative_indent_of_markdown_is_identity(settings, make_file):
    """Markdown has no indentation semantics; the edit must not gain margin."""
    body = "# Title\n\ntext\n"
    make_file("README.md", body)
    read = __import__(
        "cursor_edit_mcp.tools.file_read", fromlist=["file_read"]
    ).file_read("README.md", settings=settings)
    edit_apply(
        "README.md",
        specs(
            {
                "op": "replace",
                "range": {"start_line": 1, "end_line": 1},
                "new_text": "# New Title",
            }
        ),
        base_hash=read["hash"],
        settings=settings,
    )
    assert (settings.workspace_root / "README.md").read_text(encoding="utf-8") == (
        "# New Title\n\ntext\n"
    )
