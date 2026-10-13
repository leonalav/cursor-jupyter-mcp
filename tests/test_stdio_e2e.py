"""End-to-end over stdio.

This is the M0 spike's real deliverable: the server has to work through the MCP
protocol, launched as a subprocess, exactly as Cursor launches it. Unit tests
call the handlers directly and would not catch a transport or schema problem.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.asyncio

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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


@asynccontextmanager
async def session_for(workspace_root: str) -> AsyncIterator[ClientSession]:
    """Launch the server exactly as Cursor does, and yield a live session.

    Both context managers must be entered and exited in the same task, which is
    why this is a context manager rather than a manual ``__aenter__`` call.
    """
    env = dict(os.environ)
    env["WORKSPACE_ROOT"] = workspace_root
    env["PYTHONPATH"] = os.path.join(REPO_ROOT, "src")

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "cursor_edit_mcp.server"],
        env=env,
        cwd=REPO_ROOT,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def payload(result) -> dict:
    """The structured body of a successful tool result."""
    return result.structured_content


def error_body(result) -> dict:
    """The JSON error payload the model sees inside the message text."""
    return json.loads(result.content[0].text.split(": ", 1)[1])


async def test_server_starts_and_lists_tools(tmp_path):
    async with session_for(str(tmp_path)) as session:
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert {"file_read", "edit_locate", "edit_apply"} <= names

        annotations = {t.name: t.annotations for t in tools.tools}
        assert annotations["file_read"].read_only_hint is True
        assert annotations["edit_locate"].read_only_hint is True
        assert annotations["edit_apply"].read_only_hint is False


async def test_locate_symbol_then_edit_over_stdio(tmp_path):
    """The PRD's primary flow: locate by symbol, then replace that range."""
    target = tmp_path / "service.py"
    target.write_bytes(SVC_BEFORE)

    async with session_for(str(tmp_path)) as session:
        located = payload(
            await session.call_tool(
                "edit_locate",
                {"path": "service.py", "by": "symbol", "name": "UserService.fetch_user"},
            )
        )
        assert (located["start_line"], located["end_line"]) == (2, 4)

        applied = payload(
            await session.call_tool(
                "edit_apply",
                {
                    "path": "service.py",
                    "base_hash": located["hash"],
                    "edits": [
                        {
                            "op": "replace",
                            "range": {
                                "start_line": located["start_line"],
                                "end_line": located["end_line"],
                            },
                            # Sent flush-left; relative indentation is
                            # expected to place it inside the class body.
                            # The range includes the decorator, so the
                            # replacement has to carry it too.
                            "new_text": (
                                "@staticmethod\n"
                                'def fetch_user(uid):\n    return {"id": int(uid)}'
                            ),
                        }
                    ],
                },
            )
        )

        assert applied["written"] is True
        assert target.read_bytes() == SVC_AFTER


async def test_locate_heading_over_stdio(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_bytes(b"# T\n\n## Install\n\nrun it\n\n## Usage\n\nuse it\n")

    async with session_for(str(tmp_path)) as session:
        located = payload(
            await session.call_tool(
                "edit_locate",
                {"path": "README.md", "by": "heading", "text": "Install"},
            )
        )
        assert (located["start_line"], located["end_line"]) == (3, 5)


async def test_locate_unknown_symbol_returns_actionable_error(tmp_path):
    (tmp_path / "app.py").write_bytes(b"def fetch_user():\n    pass\n")

    async with session_for(str(tmp_path)) as session:
        result = await session.call_tool(
            "edit_locate", {"path": "app.py", "by": "symbol", "name": "fetch_usr"}
        )

        assert result.is_error is True
        body = error_body(result)
        assert body["code"] == "ANCHOR_NOT_FOUND"
        assert "fetch_user" in body["hint"]


async def test_locate_does_not_modify_the_file(tmp_path):
    target = tmp_path / "app.py"
    before = b"def alpha():\n    pass\n"
    target.write_bytes(before)

    async with session_for(str(tmp_path)) as session:
        await session.call_tool(
            "edit_locate", {"path": "app.py", "by": "symbol", "name": "alpha"}
        )
        assert target.read_bytes() == before


async def test_read_then_edit_over_stdio(tmp_path):
    # write_bytes, not write_text: text mode would translate newlines to CRLF on
    # Windows and make this test assert on the platform rather than the server.
    target = tmp_path / "app.py"
    target.write_bytes(b"one\ntwo\nthree\n")

    async with session_for(str(tmp_path)) as session:
        read = payload(await session.call_tool("file_read", {"path": "app.py"}))
        assert read["line_count"] == 3
        assert read["content"] == "one\ntwo\nthree\n"
        assert read["eol"] == "lf"

        applied = payload(
            await session.call_tool(
                "edit_apply",
                {
                    "path": "app.py",
                    "base_hash": read["hash"],
                    "edits": [
                        {
                            "op": "replace",
                            "range": {"start_line": 2, "end_line": 2},
                            "new_text": "TWO",
                        }
                    ],
                },
            )
        )

        assert applied["ok"] is True
        assert applied["written"] is True
        assert target.read_bytes() == b"one\nTWO\nthree\n"


async def test_crlf_file_round_trips_over_stdio(tmp_path):
    """A CRLF file must come back as CRLF, byte for byte."""
    target = tmp_path / "windows.txt"
    target.write_bytes(b"one\r\ntwo\r\nthree\r\n")

    async with session_for(str(tmp_path)) as session:
        read = payload(await session.call_tool("file_read", {"path": "windows.txt"}))
        assert read["eol"] == "crlf"

        await session.call_tool(
            "edit_apply",
            {
                "path": "windows.txt",
                "base_hash": read["hash"],
                "edits": [
                    {
                        "op": "replace",
                        "range": {"start_line": 2, "end_line": 2},
                        "new_text": "TWO",
                    }
                ],
            },
        )

        assert target.read_bytes() == b"one\r\nTWO\r\nthree\r\n"


async def test_stale_hash_surfaces_structured_error(tmp_path):
    target = tmp_path / "app.py"
    target.write_bytes(b"one\ntwo\n")

    async with session_for(str(tmp_path)) as session:
        read = payload(await session.call_tool("file_read", {"path": "app.py"}))
        target.write_bytes(b"changed\nby\nuser\n")

        result = await session.call_tool(
            "edit_apply",
            {
                "path": "app.py",
                "base_hash": read["hash"],
                "edits": [
                    {
                        "op": "replace",
                        "range": {"start_line": 1, "end_line": 1},
                        "new_text": "X",
                    }
                ],
            },
        )

        assert result.is_error is True
        body = error_body(result)
        assert body["code"] == "STALE_HASH"
        assert "hint" in body
        assert target.read_bytes() == b"changed\nby\nuser\n"


async def test_path_jail_enforced_over_stdio(tmp_path):
    async with session_for(str(tmp_path)) as session:
        result = await session.call_tool("file_read", {"path": "../../../etc/passwd"})
        assert result.is_error is True
        assert error_body(result)["code"] == "PATH_FORBIDDEN"


async def test_workspace_root_env_is_respected(tmp_path):
    """The jail must follow WORKSPACE_ROOT, not the process CWD."""
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "inside.py").write_text("ok\n", encoding="utf-8")
    (tmp_path / "outside.py").write_text("nope\n", encoding="utf-8")

    async with session_for(str(workspace)) as session:
        inside = payload(await session.call_tool("file_read", {"path": "inside.py"}))
        assert inside["ok"] is True

        result = await session.call_tool("file_read", {"path": "../outside.py"})
        assert result.is_error is True
        assert error_body(result)["code"] == "PATH_FORBIDDEN"
