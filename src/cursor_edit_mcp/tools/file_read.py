"""``file_read`` — read a file or a line range.

The returned ``hash`` is the guard for every later ``edit_apply`` call, so it is
always computed over the whole file even when only a range is requested.
"""

from __future__ import annotations

from typing import Any

from ..edit.document import parse_document, read_bytes
from ..edit.hashing import hash_bytes, short_hash
from ..logging_setup import get_logger
from ..security.paths import resolve_path
from ..settings import Settings, get_settings

READ_DESCRIPTION = """\
Read a text file, or a 1-based inclusive line range of it.

Returns the content plus a SHA-256 `hash` of the WHOLE file. Pass that hash \
back as `base_hash` on the next edit_apply so the edit is rejected if the \
file changed in between. Read before every edit; do not guess line numbers."""

_LOGGER = get_logger(__name__)


def file_read(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Read ``path``, optionally limited to an inclusive 1-based line range."""
    settings = settings or get_settings()
    resolved = resolve_path(path, settings)
    # One read: the hash must describe the same bytes the content came from.
    data = read_bytes(resolved, path, settings.max_file_bytes)
    document = parse_document(data, path)
    digest = hash_bytes(data)

    line_count = document.line_count
    if start_line is None and end_line is None:
        content = document.text
        first, last = (1, line_count) if line_count else (0, 0)
        partial = False
    else:
        first = start_line if start_line is not None else 1
        last = end_line if end_line is not None else line_count
        content = document.slice_text(first, last)
        partial = True

    truncated = len(content) > settings.max_content_chars
    if truncated:
        content = content[: settings.max_content_chars]

    _LOGGER.info(
        "file_read",
        extra={
            "context": {
                "path": resolved.as_posix(),
                "lines": [first, last],
                "line_count": line_count,
                "truncated": truncated,
            }
        },
    )

    return {
        "ok": True,
        "path": path,
        "content": content,
        "hash": digest,
        "short_hash": short_hash(digest),
        "line_count": line_count,
        "start_line": first,
        "end_line": last,
        "partial": partial,
        "truncated": truncated,
        "eol": "crlf" if document.eol == "\r\n" else "lf",
        "final_newline": document.final_newline,
        "encoding": document.encoding,
    }
