"""The text document model.

A file is held as a list of lines plus the file's own line-ending style, so
that writing it back reproduces the original bytes for every line the edit did
not touch. That property is what makes edits produce clean diffs on CRLF
repositories, and it is covered directly by the round-trip tests.

Line numbers are 1-based and inclusive, matching what an editor shows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

from ..errors import EditMcpError, ErrorCode, file_not_found

LF = "\n"
CRLF = "\r\n"
CR = "\r"

_ENCODING_COOKIE = re.compile(rb"^[ \t\f]*#.*?coding[:=][ \t]*([-_.a-zA-Z0-9]+)")


@dataclass(frozen=True)
class TextDocument:
    """A decoded text file with its line-ending style preserved."""

    text: str
    eol: str = LF
    final_newline: bool = True
    encoding: str = "utf-8"
    had_bom: bool = False

    @property
    def lines(self) -> list[str]:
        """Lines without terminators. A trailing newline does not add a line.

        The invariant this protects: ``len(lines)`` is the line number the agent
        sees in an editor, and ``render(lines)`` reproduces the file exactly.
        """
        if self.text == "":
            return []
        parts = self.text.split(self.eol)
        if parts and parts[-1] == "" and self.text.endswith(self.eol):
            parts.pop()
        return parts

    @property
    def line_count(self) -> int:
        return len(self.lines)

    def line(self, number: int) -> str:
        """Return one line, 1-based, without its terminator."""
        return self.lines[number - 1]

    def slice_text(self, start_line: int, end_line: int | None = None) -> str:
        """Return the text of an inclusive 1-based line range."""
        lines = self.lines
        end = end_line if end_line is not None else len(lines)
        if start_line < 1 or end > len(lines) or start_line > end:
            raise EditMcpError(
                ErrorCode.RANGE_OUT_OF_BOUNDS,
                f"Lines {start_line}-{end} are outside the file.",
                hint=f"The file has {len(lines)} line(s).",
                details={
                    "start_line": start_line,
                    "end_line": end,
                    "line_count": len(lines),
                },
            )
        return self.eol.join(lines[start_line - 1 : end])

    def render(self, lines: list[str]) -> str:
        """Rebuild file text from ``lines`` using this file's conventions."""
        if not lines:
            return ""
        return self.eol.join(lines) + (self.eol if self.final_newline else "")

    def with_text(self, text: str) -> TextDocument:
        return replace(self, text=text)


def detect_eol(text: str) -> str:
    """Return the dominant line ending, preferring the most explicit form.

    Mixed-ending files resolve to whichever style appears most, so a CRLF file
    that happens to contain a lone LF keeps CRLF for anything newly written.
    """
    crlf = text.count(CRLF)
    lf = text.count(LF) - crlf
    cr = text.count(CR) - crlf
    if crlf >= lf and crlf >= cr and crlf > 0:
        return CRLF
    if cr > lf and cr > 0:
        return CR
    return LF


def to_lf(text: str) -> str:
    """Normalize every line ending to ``\\n`` for an external tool.

    External validators must be fed LF. Feeding a CRLF file's own text to Ruff
    makes it emit a blank line between every source line, because the carriage
    return is carried into its line model. Normalizing here, once, at the tool
    boundary, keeps that class of bug out of every runner.
    """
    return text.replace(CRLF, LF).replace(CR, LF)


def to_eol(text: str, eol: str) -> str:
    """Restore a document's line-ending style after an external tool ran."""
    if eol == LF:
        return text
    return text.replace(LF, eol)


def detect_encoding(data: bytes) -> str:
    """Decode using the BOM, else a Python encoding cookie, else UTF-8."""
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    for raw_line in data[:1024].split(b"\n", 2)[:2]:
        match = _ENCODING_COOKIE.match(raw_line)
        if match:
            return match.group(1).decode("ascii", "ignore")
    return "utf-8"


def parse_document(data: bytes, raw_path: str) -> TextDocument:
    """Decode bytes that have already been read from disk.

    Kept separate from :func:`read_document` so that callers which must also
    hash the file can hash *the same bytes* they parse. Reading the file twice
    would leave a window in which the content being edited and the content
    being compared are different reads, which is exactly what the hash guard
    exists to prevent.
    """
    encoding = detect_encoding(data)
    try:
        text = data.decode(encoding)
    except (UnicodeDecodeError, LookupError) as exc:
        raise EditMcpError(
            ErrorCode.DECODE_ERROR,
            f"{raw_path} could not be decoded as {encoding}.",
            hint="Re-save the file as UTF-8, then retry.",
            details={"path": raw_path, "encoding": encoding, "reason": str(exc)},
        ) from exc

    return TextDocument(
        text=text,
        eol=detect_eol(text),
        final_newline=text.endswith((LF, CR)),
        encoding=encoding,
        had_bom=encoding == "utf-8-sig",
    )


def read_bytes(path: Path, raw_path: str, max_bytes: int) -> bytes:
    """Read a file's bytes once, enforcing the size limit first."""
    if not path.exists():
        raise file_not_found(raw_path)
    if not path.is_file():
        raise EditMcpError(
            ErrorCode.NOT_A_FILE,
            f"{raw_path} is a directory, not a file.",
            hint="Pass a file path.",
            details={"path": raw_path},
        )

    size = path.stat().st_size
    if size > max_bytes:
        raise EditMcpError(
            ErrorCode.FILE_TOO_LARGE,
            f"{raw_path} is {size} bytes, over the {max_bytes} byte limit.",
            hint="Edit a smaller file, or raise MAX_FILE_BYTES for the server.",
            details={"path": raw_path, "size_bytes": size, "max_bytes": max_bytes},
        )

    return path.read_bytes()


def read_document(path: Path, raw_path: str, max_bytes: int) -> TextDocument:
    """Read and decode a file in one shot."""
    return parse_document(read_bytes(path, raw_path, max_bytes), raw_path)


def write_bytes(document: TextDocument, lines: list[str]) -> bytes:
    """Render ``lines`` and encode them with the document's own encoding."""
    return document.render(lines).encode(document.encoding)
