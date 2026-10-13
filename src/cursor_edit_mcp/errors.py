"""Structured error model.

Every failure the agent can act on carries a stable code, a message, and a hint
that tells it what to do next. Errors are raised as MCP ``ToolError`` so the
payload reaches the model verbatim instead of being masked as a crash.

The JSON body is the contract: ``{"code", "message", "hint", "details"}``.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError


class ErrorCode:
    """Stable error codes. These are part of the tool contract; do not rename."""

    STALE_HASH = "STALE_HASH"
    RANGE_OUT_OF_BOUNDS = "RANGE_OUT_OF_BOUNDS"
    EXPECTED_TEXT_MISMATCH = "EXPECTED_TEXT_MISMATCH"
    OVERLAPPING_EDITS = "OVERLAPPING_EDITS"
    ANCHOR_NOT_FOUND = "ANCHOR_NOT_FOUND"
    PATH_FORBIDDEN = "PATH_FORBIDDEN"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    NOT_A_FILE = "NOT_A_FILE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    TOO_MANY_EDITS = "TOO_MANY_EDITS"
    INVALID_EDIT = "INVALID_EDIT"
    DECODE_ERROR = "DECODE_ERROR"
    WRITE_FAILED = "WRITE_FAILED"
    FILE_EXISTS = "FILE_EXISTS"
    SYNTAX_ERROR_INTRODUCED = "SYNTAX_ERROR_INTRODUCED"
    LINT_REGRESSION = "LINT_REGRESSION"
    VALIDATION_UNAVAILABLE = "VALIDATION_UNAVAILABLE"
    NOTEBOOK_INVALID = "NOTEBOOK_INVALID"
    SCRIPT_INVALID = "SCRIPT_INVALID"
    CELL_NOT_FOUND = "CELL_NOT_FOUND"
    CELL_NOT_CODE = "CELL_NOT_CODE"
    CELL_TYPE_INVALID = "CELL_TYPE_INVALID"
    CELL_RANGE_INVALID = "CELL_RANGE_INVALID"
    KERNEL_NOT_RUNNING = "KERNEL_NOT_RUNNING"
    KERNEL_START_FAILED = "KERNEL_START_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class EditMcpError(ToolError):
    """An anticipated failure with an actionable payload.

    Subclassing ``ToolError`` matters: the SDK sends our message to the client
    and logs it without a traceback. Any other exception reaches the model as
    the generic string ``Error executing tool <name>``, which would discard the
    code and hint the agent needs to self-correct.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.hint = hint
        self.details = details or {}
        super().__init__(self._render())

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            body["hint"] = self.hint
        if self.details:
            body["details"] = self.details
        return body

    def _render(self) -> str:
        return json.dumps(self.payload(), ensure_ascii=False, separators=(",", ":"))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}({self.code}: {self.message})"


def stale_hash(expected: str, actual: str, path: str) -> EditMcpError:
    return EditMcpError(
        ErrorCode.STALE_HASH,
        f"{path} changed since it was read.",
        hint="Re-read the file and re-issue the edit with the current base_hash.",
        details={
            "path": path,
            "expected_base_hash": expected,
            "current_hash": actual,
        },
    )


def path_forbidden(path: str, workspace_root: str, rule: str) -> EditMcpError:
    return EditMcpError(
        ErrorCode.PATH_FORBIDDEN,
        f"{path} is outside the editable area ({rule}).",
        hint=(
            "Only edit files inside the workspace root. "
            "Adjust the server's WORKSPACE_ROOT if this path should be allowed."
        ),
        details={"path": path, "workspace_root": workspace_root, "rule": rule},
    )


def file_not_found(path: str) -> EditMcpError:
    return EditMcpError(
        ErrorCode.FILE_NOT_FOUND,
        f"{path} does not exist.",
        hint="Check the path, or use file_create to add a new file.",
        details={"path": path},
    )


def anchor_not_found(
    path: str,
    what: str,
    *,
    hint: str,
    candidates: list[dict[str, Any]] | None = None,
    occurrence: int | None = None,
) -> EditMcpError:
    """Nothing resolved, or nothing resolved unambiguously.

    Candidates are the near-misses that would have worked. Without them the
    agent's only recovery is to re-read the whole file and guess again, which is
    exactly the loop this server exists to break.
    """
    details: dict[str, Any] = {"path": path, "what": what}
    if occurrence is not None:
        details["occurrence"] = occurrence
    if candidates is not None:
        details["candidates"] = candidates

    if candidates:
        message = f"{what} did not match, but {len(candidates)} similar match(es) exist."
    else:
        message = f"{what} did not match."

    return EditMcpError(ErrorCode.ANCHOR_NOT_FOUND, message, hint=hint, details=details)
