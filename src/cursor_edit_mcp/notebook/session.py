"""Shared plumbing for the notebook tools.

Every notebook mutation follows the same shape: load, check the hash, mutate in
memory, lint before and after, refuse under ``strict``, write atomically, and
re-validate the schema of what was written. Writing that once means none of the
six tools can forget one of those steps.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..edit.atomic import atomic_write
from ..edit.document import read_bytes
from ..edit.hashing import hash_bytes, short_hash
from ..errors import EditMcpError, ErrorCode, stale_hash
from ..logging_setup import get_logger
from ..runners.base import Resolver
from ..security.paths import assert_mutable, resolve_path
from ..settings import Settings
from ..validate.diagnostics import BaselineDiff
from . import cells as cell_ops
from . import engine, linting
from .engine import Notebook

_LOGGER = get_logger(__name__)

VALIDATE_OFF = "off"
VALIDATE_WARN = "warn"
VALIDATE_STRICT = "strict"


class NotebookSession:
    """A loaded notebook plus the guard state for one tool call."""

    def __init__(
        self,
        path: str,
        settings: Settings,
        base_hash: str | None = None,
    ) -> None:
        self.settings = settings
        self.resolved: Path = resolve_path(path, settings)
        cell_ops.notebook_path(self.resolved)
        assert_mutable(self.resolved, path, settings)

        data = read_bytes(self.resolved, path, settings.max_file_bytes)
        self.notebook: Notebook = engine.load(self.resolved, data)
        self.current_hash = hash_bytes(data)
        self.before_diagnostics: tuple[linting.CellDiagnostic, ...] = ()

        if base_hash and base_hash != self.current_hash:
            raise stale_hash(path, self.current_hash, self.resolved.name)

        self.before_diagnostics = self._lint()

    def _lint(self) -> tuple[linting.CellDiagnostic, ...]:
        return linting.lint(
            self.notebook.data,
            path=self.resolved,
            resolver=Resolver(self.settings.workspace_root),
            timeout=self.settings.tool_timeout_s,
        )

    @property
    def data(self) -> dict:
        return self.notebook.data

    def outline(self) -> list[cell_ops.OutlineEntry]:
        return cell_ops.outline(self.notebook)

    def resolve(
        self,
        *,
        cell_id: str | None = None,
        cell_number: int | None = None,
    ) -> cell_ops.CellRef:
        return cell_ops.resolve(self.notebook, cell_id=cell_id, cell_number=cell_number)

    def insert(
        self,
        *,
        source: str,
        cell_type: str,
        after_cell_id: str | None = None,
        index: int | None = None,
        at_start: bool = False,
        at_end: bool = False,
    ) -> cell_ops.CellRef:
        return cell_ops.insert(
            self.notebook,
            source=source,
            cell_type=cell_type,
            after_cell_id=after_cell_id,
            index=index,
            at_start=at_start,
            at_end=at_end,
        )

    def update(
        self,
        ref: cell_ops.CellRef,
        *,
        source: str | None = None,
        keep_outputs: bool = False,
        cell_type: str | None = None,
    ) -> None:
        cell_ops.update(
            self.notebook,
            ref,
            source=source,
            keep_outputs=keep_outputs,
            cell_type=cell_type,
        )

    def delete(self, refs: list[cell_ops.CellRef]) -> int:
        return cell_ops.delete(self.notebook, refs)

    def move(self, ref: cell_ops.CellRef, destination: int) -> cell_ops.CellRef:
        return cell_ops.move(self.notebook, ref, destination)

    def summary(self, **extra: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "ok": True,
            "path": self.resolved.name,
            "hash": self.current_hash,
            "cell_count": len(self.notebook.cells),
        }
        body.update(extra)
        return body

    def finish(
        self,
        *,
        validate: str = VALIDATE_WARN,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate, write, and describe the result.

        ``validate`` has the same meaning as on ``edit_apply``: ``strict``
        refuses to write when the mutation introduced a new problem.
        """
        repaired = cell_ops.finalize(self.notebook)

        after = () if validate == VALIDATE_OFF else self._lint()
        baseline: BaselineDiff = (
            linting.diff_cells(self.before_diagnostics, after)
            if validate != VALIDATE_OFF
            else linting.diff_cells((), ())
        )

        if validate == VALIDATE_STRICT and baseline.has_new_errors:
            introduced = {d.fingerprint for d in baseline.new}
            blocking = [
                d.as_dict() for d in after if d.as_diagnostic().fingerprint in introduced
            ]
            raise EditMcpError(
                ErrorCode.LINT_REGRESSION,
                f"{self.resolved.name}: this change introduces "
                f"{len(baseline.new_errors)} new problem(s).",
                hint="Fix the reported cells and retry. Existing problems are "
                "listed under preexisting_count and do not block the write.",
                details={
                    "path": self.resolved.name,
                    "diagnostics": baseline.as_dict(),
                    "new_cells": blocking,
                },
            )

        payload = engine.to_bytes(self.notebook)
        atomic_write(self.resolved, payload, mode=None)
        new_hash = hash_bytes(payload)

        _LOGGER.info(
            "notebook.write",
            extra={
                "context": {
                    "path": self.resolved.as_posix(),
                    "old_hash": short_hash(self.current_hash),
                    "new_hash": short_hash(new_hash),
                    "validate": validate,
                }
            },
        )

        body: dict[str, Any] = {
            "ok": True,
            "path": self.resolved.name,
            "hash": self.current_hash,
            "new_hash": new_hash,
            "cell_count": len(self.notebook.cells),
            "diagnostics": baseline.as_dict(),
            "cells": [
                entry.as_dict()
                for entry in cell_ops.outline(self.notebook.with_data(self.data))
            ],
        }
        if repaired:
            body["repaired_cell_ids"] = list(repaired)
        if extra:
            body.update(extra)
        return body
