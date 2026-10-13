"""Markdown structural checks.

These run always, with no configuration, and catch Markdown that is *broken*
rather than merely stylistically imperfect.
"""

from __future__ import annotations

from cursor_edit_mcp.profiles import markdown_profile


def codes(text: str) -> list[str]:
    return [issue.code for issue in markdown_profile.check(text)]


# --- fences ---------------------------------------------------------------


def test_unclosed_fence_is_reported():
    assert "MD-UNCLOSED-FENCE" in codes("# T\n\n```python\nx = 1\n")


def test_closed_fence_is_fine():
    assert codes("# T\n\n```python\nx = 1\n```\n") == []


def test_tilde_fence_closes_on_tilde():
    assert codes("~~~\nx\n~~~\n") == []


def test_backtick_fence_does_not_close_on_tildes():
    assert "MD-UNCLOSED-FENCE" in codes("```\nx\n~~~\n")


def test_longer_fence_closes_a_longer_fence():
    assert codes("````\n```\n````\n") == []


def test_heading_inside_fence_is_not_a_heading():
    """The reason fences are tracked at all."""
    from cursor_edit_mcp.profiles.markdown_blocks import outline

    headings = [h["title"] for h in outline(["## Real\n", "```\n", "## Fake\n", "```\n"])]
    assert headings == ["Real"]


# --- tables ---------------------------------------------------------------


def test_broken_table_delimiter_is_reported():
    assert "MD-BAD-TABLE-DELIMITER" in codes("| a | b |\n| --- |\n| 1 | 2 |\n")


def test_valid_table_passes():
    assert codes("| a | b |\n| --- | --- |\n| 1 | 2 |\n") == []


def test_aligned_delimiter_is_valid():
    assert codes("| a | b |\n| :-- | --: |\n| 1 | 2 |\n") == []


def test_row_with_wrong_column_count_is_reported():
    assert "MD-TABLE-COLUMN-MISMATCH" in codes(
        "| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n"
    )


def test_consistent_table_with_many_rows_passes():
    assert codes("| a | b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n| 5 | 6 |\n") == []


# --- front matter ---------------------------------------------------------


def test_valid_front_matter_passes():
    assert codes("---\ntitle: x\n---\n\n# T\n") == []


def test_unterminated_front_matter_is_reported():
    assert "MD-UNTERMINATED-FRONT-MATTER" in codes("---\ntitle: x\n\n# T\n")


def test_no_front_matter_passes():
    assert codes("# T\n") == []


# --- lists ----------------------------------------------------------------


def test_consistent_list_passes():
    assert codes("- a\n- b\n- c\n") == []


def test_inconsistent_list_indentation_is_reported():
    assert "MD-LIST-INDENT-INCONSISTENT" in codes("- a\n  - b\n")


def test_ordered_and_unordered_lists_mixed_widths():
    codes_out = codes("1. a\n2. b\n")
    assert codes_out == []


def test_list_inside_fence_is_ignored():
    assert codes("```\n- a\n  - b\n```\n") == []


# --- profile integration --------------------------------------------------


def test_structural_issues_become_diagnostics():
    diagnostics = markdown_profile.check_as_diagnostics("```\nx\n")
    assert diagnostics
    assert diagnostics[0].is_syntax is True
    assert diagnostics[0].tool == "markdown"


def test_block_map_lists_headings_and_fences():
    blocks = markdown_profile.block_map("# A\n\n```\nx\n```\n\n## B\n")
    kinds = [(b.get("kind") or b.get("title")) for b in blocks]
    assert kinds == ["A", "code", "B"]
