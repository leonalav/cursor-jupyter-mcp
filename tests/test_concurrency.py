"""Regression tests for concurrency invariants of the hash guard.

The guard's value rests on one claim: if ``base_hash`` matches, the content the
edit was planned against is still the content on disk. Any code path that reads
the file more than once per call breaks that claim, because the reads can return
different content. These tests pin the claim by changing the file underneath the
tool mid-call.
"""

from __future__ import annotations

from pathlib import Path

from cursor_edit_mcp.edit import document as document_module
from cursor_edit_mcp.tools.edit_apply import EditSpec, edit_apply
from cursor_edit_mcp.tools.file_read import file_read


def specs(*items) -> list[EditSpec]:
    return [EditSpec.model_validate(item) for item in items]


def test_file_read_hashes_the_bytes_it_returns(settings, make_file, monkeypatch):
    """Rewriting the file mid-read must not produce a hash for newer content."""
    make_file("race.py", "original\n")
    real_read = Path.read_bytes
    calls = {"n": 0}

    def racing_read(self: Path) -> bytes:
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate a concurrent write landing between two reads.
            original_read = real_read(self)
            Path(self).write_bytes(b"rewritten\n")
            return original_read
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", racing_read)

    result = file_read("race.py", settings=settings)

    assert result["content"] == "original\n"
    assert calls["n"] == 1, "file_read must read the file exactly once"


def test_edit_apply_reads_the_file_exactly_once(settings, make_file, monkeypatch):
    """Parse, hash, and change-detection must all come from a single read.

    Two reads leave a window where the document and the compared hash describe
    different file contents, which is exactly what the guard must rule out.
    """
    make_file("race.py", "line-a\nline-b\n")
    target = settings.workspace_root / "race.py"
    digest = file_read("race.py", settings=settings)["hash"]

    real_read = Path.read_bytes
    calls = {"n": 0}

    def counting_read(self: Path) -> bytes:
        if self == target:
            calls["n"] += 1
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read)

    edit_apply(
        "race.py",
        specs(
            {
                "op": "replace",
                "range": {"start_line": 2, "end_line": 2},
                "new_text": "line-c",
            }
        ),
        base_hash=digest,
        settings=settings,
    )

    assert calls["n"] == 1, f"edit_apply read the file {calls['n']} times"


def test_document_reads_bytes_once_per_call(settings, make_file, monkeypatch):
    """read_bytes is the single entry point; nothing should re-read behind it."""
    make_file("a.py", "x\n")
    target = settings.workspace_root / "a.py"

    real_read = Path.read_bytes
    calls = {"n": 0}

    def counting_read(self: Path) -> bytes:
        if self == target:
            calls["n"] += 1
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read)

    file_read("a.py", settings=settings)
    assert calls["n"] == 1


def test_parse_document_does_not_touch_the_filesystem(settings):
    """parse_document must decode only the bytes handed to it."""
    data = b"alpha\nbeta\n"
    parsed = document_module.parse_document(data, "virtual.py")
    assert parsed.lines == ["alpha", "beta"]
    assert parsed.text == "alpha\nbeta\n"
