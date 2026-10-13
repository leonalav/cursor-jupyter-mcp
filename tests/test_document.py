"""Document model: line addressing, EOL/BOM/final-newline preservation."""

from __future__ import annotations

import pytest

from cursor_edit_mcp.edit.document import TextDocument, detect_encoding, detect_eol
from cursor_edit_mcp.edit.hashing import hash_bytes
from cursor_edit_mcp.errors import EditMcpError, ErrorCode


def test_line_count_ignores_trailing_newline():
    assert TextDocument(text="a\nb\n").line_count == 2
    assert TextDocument(text="a\nb").line_count == 2
    assert TextDocument(text="").line_count == 0


def test_lines_are_one_based():
    doc = TextDocument(text="alpha\nbeta\ngamma\n")
    assert doc.line(1) == "alpha"
    assert doc.line(3) == "gamma"


def test_slice_text_is_inclusive():
    doc = TextDocument(text="a\nb\nc\nd\n")
    assert doc.slice_text(2, 3) == "b\nc"


def test_slice_text_out_of_bounds_reports_line_count():
    doc = TextDocument(text="a\nb\n")
    with pytest.raises(EditMcpError) as excinfo:
        doc.slice_text(1, 9)
    error = excinfo.value
    assert error.code == ErrorCode.RANGE_OUT_OF_BOUNDS
    assert error.details["line_count"] == 2
    assert "2 line(s)" in error.hint


def test_empty_file_has_no_lines():
    assert TextDocument(text="").lines == []


def test_detect_eol_prefers_dominant_style():
    assert detect_eol("a\nb\n") == "\n"
    assert detect_eol("a\r\nb\r\n") == "\r\n"
    assert detect_eol("a\rb\r") == "\r"
    assert detect_eol("no newlines") == "\n"


def test_detect_eol_keeps_crlf_when_lone_lf_is_rare():
    assert detect_eol("a\r\nb\r\nc\nd\r\ne\r\n") == "\r\n"


def test_render_preserves_final_newline_policy():
    with_newline = TextDocument(text="a\n", final_newline=True)
    without = TextDocument(text="a", final_newline=False)
    assert with_newline.render(["x", "y"]) == "x\ny\n"
    assert without.render(["x", "y"]) == "x\ny"


def test_render_of_no_lines_is_empty_string():
    assert TextDocument(text="a\n").render([]) == ""


def test_detect_encoding_handles_bom():
    assert detect_encoding(b"\xef\xbb\xbfprint(1)") == "utf-8-sig"


def test_detect_encoding_reads_python_cookie():
    assert detect_encoding(b"# -*- coding: latin-1 -*-\nx = 1\n") == "latin-1"


def test_detect_encoding_defaults_to_utf8():
    assert detect_encoding(b"plain text") == "utf-8"


def test_hash_is_content_addressed():
    assert hash_bytes(b"abc") == hash_bytes(b"abc")
    assert hash_bytes(b"abc") != hash_bytes(b"abd")
    assert hash_bytes(b"abc").startswith("sha256:")
