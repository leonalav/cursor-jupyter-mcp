"""The four invariants recorded in design/DECISIONS.md.

These are the claims that, if they stop being true, mean a recorded decision
needs revisiting rather than a bug needing a patch. They are deliberately
asserted at the level of the whole tool call rather than of a helper.
"""

from __future__ import annotations

import ast
from pathlib import Path

from cursor_edit_mcp.tools.edit_apply import EditSpec, edit_apply
from cursor_edit_mcp.tools.file_read import file_read


def specs(*items) -> list[EditSpec]:
    return [EditSpec.model_validate(item) for item in items]


def test_one_read_per_edit_apply(settings, make_file, monkeypatch):
    """Invariant 1: the target is read exactly once per call."""
    make_file("a.py", "one\ntwo\nthree\n")
    target = settings.workspace_root / "a.py"
    digest = file_read("a.py", settings=settings)["hash"]

    real_read = Path.read_bytes
    reads = {"n": 0}

    def counting(self: Path) -> bytes:
        if self == target:
            reads["n"] += 1
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    edit_apply(
        "a.py",
        specs(
            {
                "op": "replace",
                "range": {"start_line": 2, "end_line": 2},
                "new_text": "TWO",
            }
        ),
        base_hash=digest,
        settings=settings,
    )

    assert reads["n"] == 1, f"expected a single read, got {reads['n']}"


def test_rejected_batch_is_byte_identical(settings, make_file):
    """Invariant 2: a guard failing late leaves nothing behind, not even the
    valid edits that preceded it in the same batch.

    Each case pairs a valid first edit with a second edit that must be rejected.
    If validation ran after any write, the first edit would already be on disk.
    """
    target = make_file("a.py", "one\ntwo\nthree\nfour\n")
    original = target.read_bytes()
    digest = file_read("a.py", settings=settings)["hash"]

    valid_first = {
        "op": "replace",
        "range": {"start_line": 1, "end_line": 1},
        "new_text": "ONE",
    }

    rejected_second = [
        # Past the end of the file.
        {"op": "replace", "range": {"start_line": 99, "end_line": 99}, "new_text": "x"},
        # Guard that cannot pass.
        {
            "op": "replace",
            "range": {"start_line": 2, "end_line": 2},
            "expect": "not present",
            "new_text": "x",
        },
        # Overlaps the first edit.
        {"op": "replace", "range": {"start_line": 1, "end_line": 2}, "new_text": "x"},
        # Insert anchored on a line the first edit rewrites.
        {"op": "insert", "after_line": 1, "new_text": "x"},
        # Malformed: delete carrying replacement text.
        {"op": "delete", "range": {"start_line": 2, "end_line": 2}, "new_text": "x"},
    ]

    for bad in rejected_second:
        try:
            edit_apply(
                "a.py",
                specs(valid_first, bad),
                base_hash=digest,
                settings=settings,
            )
        except Exception:
            pass
        else:  # pragma: no cover - a batch that should have failed succeeded
            raise AssertionError(f"batch unexpectedly applied: {bad}")

        assert target.read_bytes() == original, f"partial write after {bad['op']}"


def test_disjoint_edits_in_one_batch_both_apply(settings, make_file):
    """The counterpart to the invariant above: valid batches do apply together."""
    target = make_file("a.py", "one\ntwo\nthree\nfour\n")
    digest = file_read("a.py", settings=settings)["hash"]

    result = edit_apply(
        "a.py",
        specs(
            {
                "op": "replace",
                "range": {"start_line": 1, "end_line": 1},
                "new_text": "ONE",
            },
            {"op": "replace", "range": {"start_line": 3, "end_line": 4}, "new_text": "x"},
        ),
        base_hash=digest,
        settings=settings,
    )

    assert result["written"] is True
    assert target.read_text(encoding="utf-8") == "ONE\ntwo\nx\n"


def test_crlf_round_trip_is_byte_identical(settings, make_file):
    """Invariant 3: a CRLF file survives a read and a no-op edit unchanged."""
    target = make_file("crlf.txt", "one\r\ntwo\r\nthree\r\n")
    before = target.read_bytes()
    digest = file_read("crlf.txt", settings=settings)["hash"]

    result = edit_apply(
        "crlf.txt",
        specs(
            {
                "op": "replace",
                "range": {"start_line": 2, "end_line": 2},
                "new_text": "two",
            }
        ),
        base_hash=digest,
        settings=settings,
    )

    assert result["changed"] is False
    assert target.read_bytes() == before == b"one\r\ntwo\r\nthree\r\n"


def test_path_forbidden_names_the_blocking_rule(settings, make_file):
    """Invariant 4: a jail rejection says which rule fired."""
    from cursor_edit_mcp.errors import EditMcpError

    for name, expected_rule in (
        (".env", "protected file pattern"),
        (".git/config", "protected directory"),
    ):
        target = settings.workspace_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x\n")
        digest = file_read(name, settings=settings)["hash"]

        try:
            edit_apply(
                name,
                specs({"op": "delete", "range": {"start_line": 1, "end_line": 1}}),
                base_hash=digest,
                settings=settings,
            )
        except EditMcpError as error:
            assert error.code == "PATH_FORBIDDEN"
            assert expected_rule in error.details["rule"], error.details
            assert error.hint, "a jail rejection must say what to do"
        else:  # pragma: no cover
            raise AssertionError(f"{name} should not be writable")

    # And a path outside the root names the workspace rule.
    try:
        file_read("../../../etc/passwd", settings=settings)
    except EditMcpError as error:
        assert error.details["rule"] == "outside workspace root"
        assert error.details["workspace_root"] == settings.workspace_root.as_posix()
    else:  # pragma: no cover
        raise AssertionError("path outside the workspace should be rejected")


def test_server_source_declares_no_shell_execution(settings):
    """The PRD forbids shelling out with interpolated strings.

    M0 runs no subprocesses at all; M2 will add runners, which must spawn with
    argument arrays. This asserts the property holds now, so the milestone that
    introduces subprocesses has to confront it deliberately.
    """
    import cursor_edit_mcp

    root = Path(cursor_edit_mcp.__file__).parent
    sources = list(root.rglob("*.py"))
    assert sources, "expected to find package sources"

    offenders = []
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # Bare os.system / os.popen are never acceptable: they hand the
            # argument to a shell.
            name = getattr(func, "id", None)
            if name in {"system", "popen"}:
                offenders.append(f"{source.name}: {name}()")
                continue
            attr = getattr(func, "attr", None)
            if attr == "run" and _has_shell_true(node):
                offenders.append(f"{source.name}: subprocess.run(shell=True)")
    assert not offenders, offenders


def _has_shell_true(node: ast.Call) -> bool:
    for keyword in node.keywords:
        if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant):
            return bool(keyword.value.value)
    return False
