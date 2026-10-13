"""Shared plumbing for the percent-format cell tools.

A percent-format script is read, mutated, and rewritten the same way a
notebook is, with three differences:

**No outputs, no execution_count.** ``cell_type`` is the only kind field
that has a real effect; ``outputs`` and ``execution_count`` are not part
of the cell dict at all, because percent scripts have nowhere to store
them and persisting them would invent state the file never had.

**Linting is per-cell, with magics neutralised.** The same Ruff
``--no-cache --stdin-filename`` path runs on the script's code cells
joined into a virtual notebook, so a magic inside a code cell does not
break the lint of the cell that follows it.

**Atomic write goes through the standard writer.** A ``.py`` file is text
and the same ``atomic_write`` used for notebooks is the right tool: the
user does not want a half-written percent file any more than they want a
half-written notebook.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..edit.atomic import atomic_write
from ..edit.document import read_bytes
from ..edit.hashing import hash_bytes
from ..errors import EditMcpError, ErrorCode
from ..logging_setup import get_logger
from ..runners.base import Resolver
from ..security.paths import assert_mutable, resolve_path
from ..settings import Settings
from . import script as script_engine
from . import script_linting
from .script import Script, finalize, source_text, to_bytes
from .script_ops import (
    CellRef,
    OutlineEntry,
    outline as _outline,
    resolve as _resolve,
    insert as _insert,
    update as _update,
    delete as _delete,
    move as _move,
)

_LOGGER = get_logger(__name__)


@dataclass
class ScriptSession:
    """A loaded script plus the guard state for one tool call."""

    def __init__(
        self,
        path: str,
        settings: Settings,
        base_hash: str | None = None,
    ) -> None:
        self.settings = settings
        self.resolved: Path = resolve_path(path, settings)
        assert_mutable(self.resolved, path, settings)

        data = read_bytes(self.resolved, path, settings.max_file_bytes)
        self.script: Script = script_engine.load(self.resolved, data)
        self.current_hash = hash_bytes(data)
        self.before_diagnostics: tuple[script_linting.CellDiagnostic, ...] = ()

        if base_hash and base_hash != self.current_hash:
            raise EditMcpError(
                ErrorCode.STALE_HASH,
                f"{path} changed since it was read.",
                hint="Re-read the file and re-issue the edit with the current base_hash.",
                details={
                    "path": path,
                    "expected_base_hash": base_hash,
                    "current_hash": self.current_hash,
                },
            )

        self.before_diagnostics = self._lint()

    def _lint(self) -> tuple[script_linting.CellDiagnostic, ...]:
        return script_linting.lint(
            self.script,
            path=self.resolved,
            resolver=Resolver(self.settings.workspace_root),
            timeout=self.settings.tool_timeout_s,
        )

    @property
    def data(self) -> dict:
        # Compatibility shim: callers that need the cells list read
        # ``script.cells`` directly. ``data`` is kept so a session can be
        # used wherever a notebook session is expected, as long as the
        # caller only asks for ``cells``.
        return {"cells": self.script.cells}

    def outline(self) -> list[OutlineEntry]:
        return _outline(self.script)

    def resolve(
        self,
        *,
        cell_id: str | None = None,
        cell_number: int | None = None,
    ) -> CellRef:
        return _resolve(self.script, cell_id=cell_id, cell_number=cell_number)

    def insert(
        self,
        *,
        source: str,
        cell_type: str,
        after_cell_id: str | None = None,
        index: int | None = None,
        at_start: bool = False,
        at_end: bool = False,
    ) -> CellRef:
        return _insert(
            self.script,
            source=source,
            cell_type=cell_type,
            after_cell_id=after_cell_id,
            index=index,
            at_start=at_start,
            at_end=at_end,
        )

    def update(
        self,
        ref: CellRef,
        *,
        source: str | None = None,
        cell_type: str | None = None,
    ) -> None:
        _update(self.script, ref, source=source, cell_type=cell_type)

    def delete(self, refs: list[CellRef]) -> int:
        return _delete(self.script, refs)

    def move(self, ref: CellRef, destination: int) -> CellRef:
        return _move(self.script, ref, destination)

    def summary(self, **extra: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "ok": True,
            "path": self.resolved.name,
            "hash": self.current_hash,
            "cell_count": len(self.script.cells),
        }
        body.update(extra)
        return body

    def finish(
        self,
        *,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Finalize, write, and describe the result.

        Percent scripts have no validation pipeline of their own: the file
        is text, and the lint stage has already run. Lint regressions are
        reported (in ``before_diagnostics``) but never block the write,
        because the cell-level edits are scoped to the cell the user named
        and refusing on a pre-existing problem in another cell would be
        surprising.
        """
        repaired = finalize(self.script)
        after = self._lint()

        payload = to_bytes(self.script)
        atomic_write(self.resolved, payload, mode=None)
        new_hash = hash_bytes(payload)

        _LOGGER.info(
            "script.write",
            extra={
                "context": {
                    "path": self.resolved.as_posix(),
                    "old_hash": self.current_hash[:12],
                    "new_hash": new_hash[:12],
                }
            },
        )

        body: dict[str, Any] = {
            "ok": True,
            "path": self.resolved.name,
            "hash": self.current_hash,
            "new_hash": new_hash,
            "cell_count": len(self.script.cells),
            "diagnostics": {
                "new_count": len(after) - len(self.before_diagnostics),
                "preexisting_count": len(self.before_diagnostics),
                "lint": [d.as_dict() for d in after],
            },
            "cells": [entry.as_dict() for entry in self.outline()],
        }
        if repaired:
            body["repaired_cell_ids"] = list(repaired)
        if self.script.unknown_types:
            body["unknown_cell_types"] = list(self.script.unknown_types)
        if extra:
            body.update(extra)
        return body
