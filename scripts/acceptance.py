"""Manual M0 acceptance check.

Launches the server through the installed ``cursor-edit-mcp`` console script,
the same way Cursor launches it, and walks the flows an agent actually performs.

Run with:  uv run python scripts/acceptance.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SVC_BEFORE = (
    b"class UserService:\n"
    b"    @staticmethod\n"
    b"    def fetch_user(uid):\n"
    b'        return {"id": uid}\n'
)
SVC_AFTER = (
    b"class UserService:\n"
    b"    @staticmethod\n"
    b"    def fetch_user(uid):\n"
    b'        return {"id": int(uid)}\n'
)
CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    CHECKS.append((name, passed, detail))
    mark = "PASS" if passed else "FAIL"
    print(f"[{mark}] {name}" + (f"  -- {detail}" if detail else ""))


def body(result) -> dict:
    if result.structured_content:
        return result.structured_content
    return json.loads(result.content[0].text)


def error_of(result) -> dict:
    return json.loads(result.content[0].text.split(": ", 1)[1])


async def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="cursor-edit-acceptance-"))
    target = workspace / "service.py"
    target.write_bytes(b"import os\n\n\ndef fetch_user(uid):\n    return os.get(uid)\n")
    outside = workspace.parent / "outside.py"
    outside.write_bytes(b"secret = 1\n")

    env = dict(os.environ)
    env["WORKSPACE_ROOT"] = str(workspace)
    params = StdioServerParameters(command="cursor-edit-mcp", env=env)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            check(
                "server initializes",
                init.server_info.name == "cursor-edit-mcp",
                f"{init.server_info.name} v{init.server_info.version}",
            )

            tools = {t.name: t for t in (await session.list_tools()).tools}
            check(
                "all eleven tools exposed",
                {
                    "file_read",
                    "edit_locate",
                    "edit_apply",
                    "file_create",
                    "validate_file",
                    "nb_outline",
                    "nb_cell_read",
                    "nb_cell_insert",
                    "nb_cell_update",
                    "nb_cell_delete",
                    "nb_cell_move",
                }
                <= set(tools),
                ", ".join(sorted(tools)),
            )
            check(
                "only mutators declare themselves as writers",
                all(
                    tools[name].annotations.read_only_hint is False
                    for name in (
                        "edit_apply",
                        "file_create",
                        "nb_cell_insert",
                        "nb_cell_update",
                        "nb_cell_delete",
                        "nb_cell_move",
                    )
                )
                and all(
                    tools[name].annotations.read_only_hint is True
                    for name in (
                        "file_read",
                        "edit_locate",
                        "validate_file",
                        "nb_outline",
                        "nb_cell_read",
                    )
                ),
            )

            # --- M2: validation ------------------------------------------
            checked = workspace / "checked.py"
            checked.write_bytes(b"def alpha():\n    return 1\n")

            clean = body(
                await session.call_tool(
                    "edit_apply",
                    {
                        "path": "checked.py",
                        "base_hash": body(
                            await session.call_tool("file_read", {"path": "checked.py"})
                        )["hash"],
                        "edits": [
                            {
                                "op": "replace",
                                "range": {"start_line": 2, "end_line": 2},
                                "new_text": "    return 2",
                            }
                        ],
                        "validate": "strict",
                    },
                )
            )
            check(
                "strict edit with no new problems is written",
                clean["written"] is True and clean["diagnostics"]["new_count"] == 0,
                f"stages={[s['stage'] + ':' + s['status'] for s in clean['stages']]}",
            )

            strict_fail = await session.call_tool(
                "edit_apply",
                {
                    "path": "checked.py",
                    "base_hash": body(
                        await session.call_tool("file_read", {"path": "checked.py"})
                    )["hash"],
                    "edits": [
                        {
                            "op": "replace",
                            "range": {"start_line": 2, "end_line": 2},
                            "new_text": "    unused = 1\n    return 2",
                        }
                    ],
                    "validate": "strict",
                    "format": "none",
                },
            )
            rejected = error_of(strict_fail)
            check(
                "strict mode rejects a new lint error",
                strict_fail.is_error and rejected["code"] == "LINT_REGRESSION",
                rejected["code"],
            )
            check(
                "rejection names the offending rule",
                any(
                    d["code"] == "F841" for d in rejected["details"]["diagnostics"]["new"]
                ),
                ", ".join(d["code"] for d in rejected["details"]["diagnostics"]["new"]),
            )
            check(
                "rejected strict edit left the file alone",
                checked.read_bytes() == b"def alpha():\n    return 2\n",
                repr(checked.read_bytes()),
            )

            debt = workspace / "debt.py"
            debt.write_bytes(b"import sys\n\n\ndef alpha():\n    return 1\n")
            debt_ok = body(
                await session.call_tool(
                    "edit_apply",
                    {
                        "path": "debt.py",
                        "base_hash": body(
                            await session.call_tool("file_read", {"path": "debt.py"})
                        )["hash"],
                        "edits": [
                            {
                                "op": "replace",
                                "range": {"start_line": 5, "end_line": 5},
                                "new_text": "    return 2",
                            }
                        ],
                        "validate": "strict",
                    },
                )
            )
            check(
                "pre-existing lint debt does not block an edit",
                debt_ok["written"] is True and debt_ok["diagnostics"]["new_count"] == 0,
                f"preexisting={debt_ok['diagnostics']['preexisting_count']}",
            )

            syntax_fail = await session.call_tool(
                "file_create",
                {"path": "broken.py", "new_text": "def broken(:\n"},
            )
            check(
                "file_create refuses to write unparseable Python",
                syntax_fail.is_error
                and error_of(syntax_fail)["code"] == "SYNTAX_ERROR_INTRODUCED"
                and not (workspace / "broken.py").exists(),
            )

            created = body(
                await session.call_tool(
                    "file_create",
                    {"path": "fresh.py", "new_text": "def fresh():\n    return 1\n"},
                )
            )
            check(
                "file_create writes a valid file",
                created["created"] is True
                and (workspace / "fresh.py").read_bytes()
                == b"def fresh():\n    return 1\n",
            )

            dupe = await session.call_tool(
                "file_create", {"path": "fresh.py", "new_text": "x = 1\n"}
            )
            check(
                "file_create refuses to clobber",
                dupe.is_error and error_of(dupe)["code"] == "FILE_EXISTS",
            )

            verdict = body(await session.call_tool("validate_file", {"path": "debt.py"}))
            check(
                "validate_file reports problems and the tooling it used",
                verdict["ok"] is False
                and verdict["diagnostics"]["new_count"] > 0
                and "ruff" in verdict["tooling"],
                f"{verdict['diagnostics']['new_count']} problem(s)",
            )

            checked_before = checked.read_bytes()
            await session.call_tool("validate_file", {"path": "checked.py"})
            check(
                "validate_file does not modify the file",
                checked.read_bytes() == checked_before,
            )

            scoped = workspace / "scoped.py"
            scoped.write_bytes(
                b"import sys\nimport os\n\n\ndef alpha():\n"
                b"    y   =   2\n    return y\n\n\ndef beta():\n"
                b"    z   =   3\n    return z\n"
            )
            body(
                await session.call_tool(
                    "edit_apply",
                    {
                        "path": "scoped.py",
                        "base_hash": body(
                            await session.call_tool("file_read", {"path": "scoped.py"})
                        )["hash"],
                        "edits": [
                            {
                                "op": "replace",
                                "range": {"start_line": 6, "end_line": 6},
                                "new_text": "    y   =   22",
                            }
                        ],
                        "validate": "warn",
                        "format": "range",
                    },
                )
            )
            scoped_after = scoped.read_bytes()
            check(
                "format touched only the edited lines",
                b"    y = 22\n" in scoped_after
                and b"    z   =   3\n" in scoped_after
                and scoped_after.startswith(b"import sys\nimport os\n"),
                repr(scoped_after.decode()),
            )

            # --- M1: locate, then edit by the range it returned ----------
            cls = workspace / "svc.py"
            cls.write_bytes(SVC_BEFORE)
            sym = body(
                await session.call_tool(
                    "edit_locate",
                    {"path": "svc.py", "by": "symbol", "name": "UserService.fetch_user"},
                )
            )
            check(
                "symbol lookup includes the decorator",
                (sym["start_line"], sym["end_line"]) == (2, 4),
                f"{sym['start_line']}-{sym['end_line']} match={sym['match']}",
            )

            reindented = body(
                await session.call_tool(
                    "edit_apply",
                    {
                        "path": "svc.py",
                        "base_hash": sym["hash"],
                        "edits": [
                            {
                                "op": "replace",
                                "range": {
                                    "start_line": sym["start_line"],
                                    "end_line": sym["end_line"],
                                },
                                # Sent flush-left; must land inside the class.
                                "new_text": (
                                    "@staticmethod\n"
                                    "def fetch_user(uid):\n"
                                    '    return {"id": int(uid)}'
                                ),
                            }
                        ],
                    },
                )
            )
            check(
                "locate -> apply re-indents into the class body",
                reindented["written"] is True and cls.read_bytes() == SVC_AFTER,
                repr(cls.read_bytes().decode()),
            )

            anchor = body(
                await session.call_tool(
                    "edit_locate",
                    {"path": "svc.py", "by": "anchor", "text": "return {"},
                )
            )
            check(
                "text anchor resolves to a line",
                anchor["start_line"] == 4,
                f"line {anchor['start_line']}",
            )

            md = workspace / "README.md"
            md.write_bytes(
                b"# Title\n\n## Install\n\nrun it\n"
                b"\n### Detail\n\nmore\n\n## Usage\n\ngo\n"
            )
            heading = body(
                await session.call_tool(
                    "edit_locate",
                    {"path": "README.md", "by": "heading", "text": "Install"},
                )
            )
            own = body(
                await session.call_tool(
                    "edit_locate",
                    {
                        "path": "README.md",
                        "by": "heading",
                        "text": "Install",
                        "include_subsections": False,
                    },
                )
            )
            check(
                "heading section spans its subsections",
                (heading["start_line"], heading["end_line"]) == (3, 9)
                and (own["start_line"], own["end_line"]) == (3, 5),
                f"with={heading['end_line']} without={own['end_line']}",
            )

            fenced = workspace / "FENCED.md"
            fenced.write_bytes(b"## Real\n\n```sh\n## Not real\n```\n")
            fenced_result = await session.call_tool(
                "edit_locate", {"path": "FENCED.md", "by": "heading", "text": "Not real"}
            )
            check(
                "heading inside a code fence is not found",
                fenced_result.is_error
                and error_of(fenced_result)["code"] == "ANCHOR_NOT_FOUND",
            )

            ambiguous = await session.call_tool(
                "edit_locate", {"path": "svc.py", "by": "anchor", "text": "uid"}
            )
            ambiguous_body = error_of(ambiguous)
            check(
                "ambiguous anchor reports candidates",
                ambiguous.is_error
                and "ambiguous" in ambiguous_body["hint"]
                and len(ambiguous_body["details"]["candidates"]) > 1,
            )

            typo = await session.call_tool(
                "edit_locate", {"path": "svc.py", "by": "symbol", "name": "fetch_usr"}
            )
            check(
                "misspelled symbol suggests the real name",
                typo.is_error and "fetch_user" in error_of(typo)["hint"],
                error_of(typo)["hint"][:60],
            )

            # --- happy path -------------------------------------------------
            read = body(await session.call_tool("file_read", {"path": "service.py"}))
            check(
                "read returns hash and line count",
                read["line_count"] == 5 and read["hash"].startswith("sha256:"),
                f"{read['line_count']} lines, {read['short_hash']}",
            )

            applied = body(
                await session.call_tool(
                    "edit_apply",
                    {
                        "path": "service.py",
                        "base_hash": read["hash"],
                        "edits": [
                            {
                                "op": "replace",
                                "range": {"start_line": 4, "end_line": 5},
                                "expect": "def fetch_user(",
                                "new_text": "def fetch_user(uid):\n"
                                "    return os.get(uid)",
                            },
                            {
                                "op": "insert",
                                "after_line": 0,
                                "new_text": "from pathlib import Path",
                            },
                        ],
                    },
                )
            )
            on_disk = target.read_text(encoding="utf-8")
            check(
                "batch edits apply against one snapshot",
                applied["written"]
                and on_disk.startswith("from pathlib import Path")
                and "def fetch_user(uid):" in on_disk,
            )
            check(
                "diff returned", "@@" in applied["diff"], f"{len(applied['diff'])} chars"
            )

            # --- guards -----------------------------------------------------
            stale = await session.call_tool(
                "edit_apply",
                {
                    "path": "service.py",
                    "base_hash": read["hash"],
                    "edits": [
                        {"op": "delete", "range": {"start_line": 1, "end_line": 1}}
                    ],
                },
            )
            check(
                "stale hash rejected",
                stale.is_error and error_of(stale)["code"] == "STALE_HASH",
            )

            current = body(await session.call_tool("file_read", {"path": "service.py"}))
            overlap = await session.call_tool(
                "edit_apply",
                {
                    "path": "service.py",
                    "base_hash": current["hash"],
                    "edits": [
                        {
                            "op": "replace",
                            "range": {"start_line": 1, "end_line": 2},
                            "new_text": "x",
                        },
                        {
                            "op": "replace",
                            "range": {"start_line": 2, "end_line": 3},
                            "new_text": "y",
                        },
                    ],
                },
            )
            check(
                "overlapping edits rejected",
                overlap.is_error and error_of(overlap)["code"] == "OVERLAPPING_EDITS",
            )

            mism = await session.call_tool(
                "edit_apply",
                {
                    "path": "service.py",
                    "base_hash": current["hash"],
                    "edits": [
                        {
                            "op": "replace",
                            "range": {"start_line": 1, "end_line": 1},
                            "expect": "def not_here",
                            "new_text": "x",
                        }
                    ],
                },
            )
            payload = error_of(mism)
            check(
                "expect mismatch returns current text",
                payload["code"] == "EXPECTED_TEXT_MISMATCH"
                and "current_text" in payload["details"],
            )

            oob = await session.call_tool(
                "edit_apply",
                {
                    "path": "service.py",
                    "base_hash": current["hash"],
                    "edits": [
                        {
                            "op": "replace",
                            "range": {"start_line": 1, "end_line": 99},
                            "new_text": "x",
                        }
                    ],
                },
            )
            check(
                "out of bounds reports line count",
                oob.is_error
                and error_of(oob)["details"]["line_count"] == current["line_count"],
            )

            # --- safety -----------------------------------------------------
            jail = await session.call_tool("file_read", {"path": str(outside)})
            check(
                "path outside workspace rejected",
                jail.is_error and error_of(jail)["code"] == "PATH_FORBIDDEN",
            )

            secret = workspace / ".env"
            secret.write_bytes(b"TOKEN=abc\n")
            senv = body(await session.call_tool("file_read", {"path": ".env"}))
            blocked = await session.call_tool(
                "edit_apply",
                {
                    "path": ".env",
                    "base_hash": senv["hash"],
                    "edits": [
                        {"op": "delete", "range": {"start_line": 1, "end_line": 1}}
                    ],
                },
            )
            check(
                "secret file readable but not writable",
                blocked.is_error and error_of(blocked)["code"] == "PATH_FORBIDDEN",
            )
            check("secret file untouched", secret.read_bytes() == b"TOKEN=abc\n")

            # --- dry run ----------------------------------------------------
            snap = body(await session.call_tool("file_read", {"path": "service.py"}))
            before = target.read_bytes()
            dry = body(
                await session.call_tool(
                    "edit_apply",
                    {
                        "path": "service.py",
                        "base_hash": snap["hash"],
                        "dry_run": True,
                        "edits": [
                            {
                                "op": "replace",
                                "range": {"start_line": 1, "end_line": 1},
                                "new_text": "ZZZ",
                            }
                        ],
                    },
                )
            )
            check(
                "dry run previews without writing",
                dry["written"] is False
                and "+ZZZ" in dry["diff"]
                and target.read_bytes() == before,
            )

            # --- M3: notebooks -------------------------------------------
            book = workspace / "analysis.ipynb"
            notebook = {
                "cells": [
                    {
                        "cell_type": "markdown",
                        "id": "md000001",
                        "metadata": {},
                        "source": ["# Analysis\n"],
                    },
                    {
                        "cell_type": "code",
                        "execution_count": 2,
                        "id": "cd000001",
                        "metadata": {"tags": ["keep"]},
                        "outputs": [
                            {"output_type": "stream", "name": "stdout", "text": "hi\n"}
                        ],
                        "source": ["import os\n", "print(os.name)"],
                    },
                    {
                        "cell_type": "raw",
                        "id": "rw000001",
                        "metadata": {},
                        "source": ["notes"],
                    },
                    {
                        "cell_type": "code",
                        "execution_count": None,
                        "id": "cd000002",
                        "metadata": {},
                        "outputs": [],
                        "source": ["%matplotlib inline\n", "total = 1\n"],
                    },
                ],
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
            serialized = json.dumps(
                notebook, indent=1, sort_keys=True, ensure_ascii=False
            )
            book.write_bytes((serialized + "\n").encode("utf-8"))
            original_bytes = book.read_bytes()

            outline = body(await session.call_tool("nb_outline", {"path": book.name}))
            check(
                "outline numbers every cell from 1",
                [c["cell_number"] for c in outline["cells"]] == [1, 2, 3, 4]
                and [c["type"] for c in outline["cells"]]
                == ["markdown", "code", "raw", "code"],
                str([(c["cell_number"], c["type"]) for c in outline["cells"]]),
            )
            check(
                "outline reports an executed cell's output",
                outline["cells"][1]["execution_count"] == 2
                and "1 output" in outline["cells"][1]["output_summary"],
            )
            check(
                "outline leaves the notebook byte-identical",
                book.read_bytes() == original_bytes,
            )

            read = body(
                await session.call_tool(
                    "nb_cell_read",
                    {"path": book.name, "cell_id": "cd000001", "include_outputs": True},
                )
            )
            check(
                "cell read returns source and outputs",
                read["cells"][0]["source"] == "import os\nprint(os.name)"
                and read["cells"][0]["outputs"][0]["text"] == "hi\n",
            )

            inserted = body(
                await session.call_tool(
                    "nb_cell_insert",
                    {
                        "path": book.name,
                        "source": "## Cleaning",
                        "cell_type": "markdown",
                        "after_cell_id": "md000001",
                    },
                )
            )
            new_id = inserted["inserted"]["cell_id"]
            check(
                "insert places the cell after its anchor",
                inserted["inserted"]["cell_number"] == 2 and inserted["cell_count"] == 5,
            )
            check(
                "inserted cell id is new and unique",
                new_id
                not in {c["cell_id"] for c in inserted["cells"] if c["cell_id"] != new_id}
                and len(new_id) == 8,
                new_id,
            )

            moved = body(
                await session.call_tool(
                    "nb_cell_move",
                    {"path": book.name, "cell_id": new_id, "destination": 0},
                )
            )
            check(
                "move relocates the cell and keeps the rest",
                moved["moved"]["cell_number"] == 1 and len(moved["cells"]) == 5,
            )

            updated = body(
                await session.call_tool(
                    "nb_cell_update",
                    {
                        "path": book.name,
                        "cell_id": "cd000001",
                        "source": "import sys\nprint(sys.version)",
                    },
                )
            )
            after_update = json.loads(book.read_text(encoding="utf-8"))
            first_code = next(c for c in after_update["cells"] if c["id"] == "cd000001")
            check(
                "update clears outputs that no longer match the source",
                first_code["outputs"] == []
                and first_code["execution_count"] is None
                and updated["outputs_cleared"] is True,
            )
            check(
                "a cell with magics is edited without a syntax error",
                updated["ok"] is True
                and not [
                    d
                    for d in updated["diagnostics"]["new"]
                    if d["code"] == "invalid-syntax"
                ],
                str(updated["diagnostics"]["new_count"]) + " new diagnostic(s)",
            )

            ranged = body(
                await session.call_tool(
                    "nb_cell_update",
                    {
                        "path": book.name,
                        "cell_id": "cd000002",
                        "range": {"start_line": 2, "end_line": 2},
                        "source": "total = 42",
                        "expect": "total = 1",
                    },
                )
            )
            after_range = json.loads(book.read_text(encoding="utf-8"))
            magics_cell = next(c for c in after_range["cells"] if c["id"] == "cd000002")
            # Line 2 was the cell's last line, so it carries no terminator.
            expected_source = ["%matplotlib inline\n", "total = 42"]
            check(
                "ranged edit is cell-relative and honours expect",
                magics_cell["source"] == expected_source,
                str(magics_cell["source"]),
            )
            del ranged

            bad_expect = await session.call_tool(
                "nb_cell_update",
                {
                    "path": book.name,
                    "cell_id": "cd000002",
                    "range": {"start_line": 2, "end_line": 2},
                    "source": "x",
                    "expect": "not in the cell",
                },
            )
            check(
                "ranged edit refuses a stale expect",
                bad_expect.is_error
                and error_of(bad_expect)["code"] == "EXPECTED_TEXT_MISMATCH"
                and "current_text" in error_of(bad_expect)["details"],
            )

            strict_nb = await session.call_tool(
                "nb_cell_update",
                {
                    "path": book.name,
                    "cell_id": "cd000002",
                    "source": "import socket",
                    "validate": "strict",
                },
            )
            on_disk = json.loads(book.read_text(encoding="utf-8"))["cells"]
            check(
                "strict refuses a new lint error and does not write",
                strict_nb.is_error
                and error_of(strict_nb)["code"] == "LINT_REGRESSION"
                and any(
                    c["id"] == "cd000002" and c["source"] == expected_source
                    for c in on_disk
                ),
                error_of(strict_nb)["code"] if strict_nb.is_error else "not rejected",
            )

            deleted = body(
                await session.call_tool(
                    "nb_cell_delete", {"path": book.name, "cell_id": "rw000001"}
                )
            )
            check(
                "delete removes only the named cell",
                deleted["deleted_count"] == 1
                and "rw000001" not in {c["cell_id"] for c in deleted["cells"]}
                and len(deleted["cells"]) == 4,
            )

            import nbformat as _nbf  # noqa: PLC0415

            _nbf.validate(_nbf.read(str(book), as_version=4))
            check("mutated notebook still validates against the nbformat schema", True)

            preserved = json.loads(book.read_text(encoding="utf-8"))
            check(
                "cell metadata and output-free structure preserved",
                any(
                    c["id"] == "cd000001" and c["metadata"].get("tags") == ["keep"]
                    for c in preserved["cells"]
                ),
            )

            not_a_notebook = workspace / "plain.py"
            not_a_notebook.write_text("x = 1\n", encoding="utf-8")
            wrong_kind = await session.call_tool("nb_outline", {"path": "plain.py"})
            check(
                "notebook tools refuse a non-notebook path",
                wrong_kind.is_error
                and error_of(wrong_kind)["code"] == "NOTEBOOK_INVALID",
            )

            empty_book = workspace / "empty.ipynb"
            empty_book.write_bytes(
                (
                    json.dumps(
                        {"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5},
                        indent=1,
                    )
                    + "\n"
                ).encode("utf-8")
            )
            empty_outline = body(
                await session.call_tool("nb_outline", {"path": "empty.ipynb"})
            )
            check(
                "empty notebook explains how to add a cell",
                empty_outline["cells"] == []
                and "nb_cell_insert" in empty_outline["note"],
            )

            # --- M4: execution -------------------------------------------
            exec_book = workspace / "exec.ipynb"
            exec_book.write_bytes(
                (json.dumps(
                    {
                        "cells": [
                            {
                                "cell_type": "code",
                                "execution_count": None,
                                "id": "ec000001",
                                "metadata": {},
                                "outputs": [],
                                "source": ["x = 41\n", "y = x + 1\n", "print(y)"],
                            },
                            {
                                "cell_type": "code",
                                "execution_count": None,
                                "id": "ec000002",
                                "metadata": {},
                                "outputs": [],
                                "source": ["print(f'x is {x}')"],
                            },
                            {
                                "cell_type": "code",
                                "execution_count": None,
                                "id": "ec000003",
                                "metadata": {},
                                "outputs": [],
                                "source": ["1/0"],
                            },
                        ],
                        "metadata": {
                            "kernelspec": {
                                "display_name": "Python 3",
                                "language": "python",
                                "name": "python3",
                            }
                        },
                        "nbformat": 4,
                        "nbformat_minor": 5,
                    },
                    indent=1,
                    sort_keys=True,
                    ensure_ascii=False,
                ) + "\n").encode("utf-8")
            )

            cell1 = body(
                await session.call_tool(
                    "execute_cell", {"path": "exec.ipynb", "cell_id": "ec000001"}
                )
            )
            check(
                "execute_cell runs code and reports stdout",
                cell1["ok"] is True
                and cell1["result"]["status"] == "ok"
                and any(
                    o.get("output_type") == "stream"
                    and o.get("text") == "42\n"
                    for o in cell1["result"]["output"]
                ),
                "ok=" + str(cell1["ok"]) + " status=" + str(cell1["result"].get("status")),
            )

            cell2 = body(
                await session.call_tool(
                    "execute_cell", {"path": "exec.ipynb", "cell_id": "ec000002"}
                )
            )
            check(
                "kernel state persists across cells",
                cell2["ok"] is True
                and any(
                    o.get("text") == "x is 41\n" for o in cell2["result"]["output"]
                ),
                str([o.get("text") for o in cell2["result"]["output"]]),
            )

            cell3 = body(
                await session.call_tool(
                    "execute_cell", {"path": "exec.ipynb", "cell_id": "ec000003"}
                )
            )
            check(
                "execute_cell reports a Python error",
                cell3["ok"] is False
                and cell3["result"]["error"]["ename"] == "ZeroDivisionError"
                and cell3["result"]["error"]["traceback"],
                str(cell3["result"].get("error", {}).get("ename")),
            )

            all_run = body(
                await session.call_tool(
                    "execute_all_cells", {"path": "exec.ipynb", "stop_on_error": True}
                )
            )
            check(
                "execute_all_cells runs every cell and stops on error",
                [r["status"] for r in all_run["results"]] == ["ok", "ok", "error"]
                and len(all_run["results"]) == 3,
                str([r["status"] for r in all_run["results"]]),
            )

            kernel_status = body(
                await session.call_tool(
                    "kernel_control", {"path": "exec.ipynb", "action": "status"}
                )
            )
            check(
                "kernel_control status reports a live kernel",
                kernel_status["kernel"]["alive"] is True
                and kernel_status["kernel"]["ready"] is True
                and kernel_status["kernel"]["name"] == "python3",
            )

            restart = body(
                await session.call_tool(
                    "kernel_control", {"path": "exec.ipynb", "action": "restart"}
                )
            )
            check(
                "kernel_control restart increments the restart count",
                restart["kernel"]["restarts"] >= 1,
                "restarts=" + str(restart["kernel"]["restarts"]),
            )

            after_restart = await session.call_tool(
                "execute_cell", {"path": "exec.ipynb", "cell_id": "ec000002"}
            )
            if after_restart.is_error:
                name_error_seen = "NameError" in str(
                    error_of(after_restart).get("message", "")
                )
            else:
                name_error_seen = "NameError" in after_restart.content[0].text
            check(
                "restart wipes kernel state",
                name_error_seen,
                after_restart.content[0].text[:120]
                if not after_restart.is_error
                else str(error_of(after_restart).get("message", ""))[:120],
            )

            await session.call_tool(
                "kernel_control", {"path": "exec.ipynb", "action": "shutdown"}
            )

    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
