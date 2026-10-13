"""MCP server bootstrap.

Tools are registered on a single ``MCPServer`` instance over stdio. Tool
annotations are set explicitly because Cursor uses them to decide when to ask
for approval: ``file_read`` is read-only, while ``edit_apply`` modifies the
workspace and is marked accordingly.
"""

from __future__ import annotations

import sys
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from .logging_setup import configure_logging, get_logger
from .settings import get_settings
from .tools.config import (
    APPLY_DESCRIPTION,
    DETECT_DESCRIPTION,
    DOCTOR_DESCRIPTION,
)
from .tools.config import (
    config_apply_fix as _config_apply_fix,
)
from .tools.config import (
    config_detect as _config_detect,
)
from .tools.config import (
    config_doctor as _config_doctor,
)
from .tools.edit_apply import APPLY_DESCRIPTION, EditSpec
from .tools.edit_apply import edit_apply as _edit_apply
from .tools.edit_locate import LOCATE_DESCRIPTION
from .tools.edit_locate import edit_locate as _edit_locate
from .tools.execute import (
    EXECUTE_ALL_DESCRIPTION,
    EXECUTE_CELL_DESCRIPTION,
    EXECUTE_RANGE_DESCRIPTION,
    KERNEL_CONTROL_DESCRIPTION,
)
from .tools.execute import (
    execute_all_cells as _execute_all_cells,
)
from .tools.execute import (
    execute_cell as _execute_cell,
)
from .tools.execute import (
    execute_cells_in_range as _execute_cells_in_range,
)
from .tools.execute import (
    kernel_control as _kernel_control,
)
from .tools.file_create import CREATE_DESCRIPTION
from .tools.file_create import file_create as _file_create
from .tools.file_read import READ_DESCRIPTION
from .tools.file_read import file_read as _file_read
from .tools.kernel_discover import KERNEL_DISCOVER_DESCRIPTION
from .tools.kernel_discover import kernel_discover as _kernel_discover
from .tools.nb_cells import (
    DELETE_DESCRIPTION,
    INSERT_DESCRIPTION,
    MOVE_DESCRIPTION,
    NB_READ_DESCRIPTION,
    OUTLINE_DESCRIPTION,
    UPDATE_DESCRIPTION,
)
from .tools.nb_cells import nb_cell_delete as _nb_cell_delete
from .tools.nb_cells import nb_cell_insert as _nb_cell_insert
from .tools.nb_cells import nb_cell_move as _nb_cell_move
from .tools.nb_cells import nb_cell_read as _nb_cell_read
from .tools.nb_cells import nb_cell_update as _nb_cell_update
from .tools.nb_cells import nb_outline as _nb_outline
from .tools.type_check import TYPE_CHECK_DESCRIPTION
from .tools.type_check import type_check as _type_check
from .tools.validate_file import VALIDATE_DESCRIPTION
from .tools.validate_file import validate_file as _validate_file

SERVER_NAME = "cursor-edit-mcp"
VERSION = "0.1.0"

INSTRUCTIONS = """\
Precise, validated editing for workspace files.

Find the range with edit_locate (symbol for Python, heading for Markdown) rather \
than counting lines. Read with file_read, then pass its `hash` back as \
`base_hash` on edit_apply. Line numbers are 1-based and inclusive, and every \
edit in a batch refers to that one snapshot.

Edits are checked before they are written. Only problems the edit introduced are \
reported; existing lint debt does not block you. Under the default \
`validate: "warn"` the edit is applied and new problems are returned; under \
`"strict"` it is refused instead, with the rejected diff attached. Fix what it \
names and retry — do not fall back to `"off"` to force a change through.

If you get STALE_HASH, re-read rather than retrying blindly."""

_LOGGER = get_logger(__name__)


def build_server() -> MCPServer:
    """Create the server with its tools registered."""
    server = MCPServer(
        name=SERVER_NAME,
        version=VERSION,
        instructions=INSTRUCTIONS,
    )

    @server.tool(
        name="file_read",
        description=READ_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Read file",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    def _file_read_tool(
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> dict[str, Any]:
        return _file_read(path, start_line=start_line, end_line=end_line)

    @server.tool(
        name="edit_locate",
        description=LOCATE_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Locate anchor",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    def _edit_locate_tool(
        path: str,
        by: str = "anchor",
        text: str | None = None,
        regex: str | None = None,
        name: str | None = None,
        occurrence: int | None = None,
        include_subsections: bool = True,
        include_text: bool = False,
    ) -> dict[str, Any]:
        return _edit_locate(
            path,
            by=by,
            text=text,
            regex=regex,
            name=name,
            occurrence=occurrence,
            include_subsections=include_subsections,
            include_text=include_text,
        )

    @server.tool(
        name="edit_apply",
        description=APPLY_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Apply file edits",
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    def _edit_apply_tool(
        path: str,
        edits: list[EditSpec],
        base_hash: str,
        dry_run: bool = False,
        indent_mode: str = "relative",
        validate: str = "warn",
        format: str = "range",
        autofix: str = "none",
        require_all_stages: bool = False,
    ) -> dict[str, Any]:
        return _edit_apply(
            path,
            edits,
            base_hash=base_hash,
            dry_run=dry_run,
            indent_mode=indent_mode,
            validate=validate,
            format=format,
            autofix=autofix,
            require_all_stages=require_all_stages,
        )

    @server.tool(
        name="file_create",
        description=CREATE_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Create file",
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    def _file_create_tool(
        path: str,
        new_text: str,
        overwrite: bool = False,
        validate: str = "strict",
        format: str = "range",
        autofix: str = "none",
    ) -> dict[str, Any]:
        return _file_create(
            path,
            new_text,
            overwrite=overwrite,
            validate=validate,
            format=format,
            autofix=autofix,
        )

    @server.tool(
        name="kernel_discover",
        description=KERNEL_DISCOVER_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Discover Jupyter kernels",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    def _kernel_discover_tool() -> dict[str, Any]:
        return _kernel_discover()

    @server.tool(
        name="validate_file",
        description=VALIDATE_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Validate file",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    def _validate_file_tool(path: str, content: str | None = None) -> dict[str, Any]:
        return _validate_file(path, content=content)

    @server.tool(
        name="type_check",
        description=TYPE_CHECK_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Type check Python",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    def _type_check_tool(
        path: str,
        tool: str = "auto",
        level: str = "error",
        content: str | None = None,
        timeout_s: float = 30.0,
    ) -> dict[str, Any]:
        return _type_check(
            path,
            tool=tool,
            level=level,
            content=content,
            timeout_s=timeout_s,
        )

    @server.tool(
        name="config_detect",
        description=DETECT_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Detect effective tooling",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    def _config_detect_tool(path: str | None = None) -> dict[str, Any]:
        return _config_detect(path=path)

    @server.tool(
        name="config_doctor",
        description=DOCTOR_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Diagnose config conflicts",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    def _config_doctor_tool(path: str | None = None) -> dict[str, Any]:
        return _config_doctor(path=path)

    @server.tool(
        name="config_apply_fix",
        description=APPLY_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Apply config doctor patch",
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    def _config_apply_fix_tool(
        finding_id: str,
        confirmation_token: str,
    ) -> dict[str, Any]:
        return _config_apply_fix(
            finding_id=finding_id,
            confirmation_token=confirmation_token,
        )

    @server.tool(
        name="nb_outline",
        description=OUTLINE_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Notebook outline",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    def _nb_outline_tool(path: str) -> dict[str, Any]:
        return _nb_outline(path)

    @server.tool(
        name="nb_cell_read",
        description=NB_READ_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Read notebook cell",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    def _nb_cell_read_tool(
        path: str,
        cell_id: str | None = None,
        cell_number: int | None = None,
        include_outputs: bool = False,
    ) -> dict[str, Any]:
        return _nb_cell_read(
            path,
            cell_id=cell_id,
            cell_number=cell_number,
            include_outputs=include_outputs,
        )

    @server.tool(
        name="nb_cell_insert",
        description=INSERT_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Insert notebook cell",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    def _nb_cell_insert_tool(
        path: str,
        source: str,
        cell_type: str = "code",
        after_cell_id: str | None = None,
        index: int | None = None,
        at_start: bool = False,
        at_end: bool = False,
        validate: str = "warn",
    ) -> dict[str, Any]:
        return _nb_cell_insert(
            path,
            source,
            cell_type=cell_type,
            after_cell_id=after_cell_id,
            index=index,
            at_start=at_start,
            at_end=at_end,
            validate=validate,
        )

    @server.tool(
        name="nb_cell_update",
        description=UPDATE_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Update notebook cell",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    def _nb_cell_update_tool(
        path: str,
        cell_id: str | None = None,
        cell_number: int | None = None,
        source: str | None = None,
        range: dict[str, int] | None = None,
        expect: str | None = None,
        cell_type: str | None = None,
        keep_outputs: bool = False,
        validate: str = "warn",
    ) -> dict[str, Any]:
        return _nb_cell_update(
            path,
            cell_id=cell_id,
            cell_number=cell_number,
            source=source,
            range=range,
            expect=expect,
            cell_type=cell_type,
            keep_outputs=keep_outputs,
            validate=validate,
        )

    @server.tool(
        name="nb_cell_delete",
        description=DELETE_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Delete notebook cell",
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    def _nb_cell_delete_tool(
        path: str,
        cell_id: str | None = None,
        cell_number: int | None = None,
        validate: str = "warn",
    ) -> dict[str, Any]:
        return _nb_cell_delete(
            path,
            cell_id=cell_id,
            cell_number=cell_number,
            validate=validate,
        )

    @server.tool(
        name="nb_cell_move",
        description=MOVE_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Move notebook cell",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    def _nb_cell_move_tool(
        path: str, cell_id: str, destination: int, validate: str = "warn"
    ) -> dict[str, Any]:
        return _nb_cell_move(path, cell_id, destination, validate=validate)

    @server.tool(
        name="execute_cell",
        description=EXECUTE_CELL_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Execute notebook cell",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    def _execute_cell_tool(
        path: str,
        cell_id: str | None = None,
        cell_number: int | None = None,
        timeout_s: float = 30.0,
        persist: bool = True,
        images: str = "discard",
        connection_file: str | None = None,
    ) -> dict[str, Any]:
        return _execute_cell(
            path,
            cell_id=cell_id,
            cell_number=cell_number,
            timeout_s=timeout_s,
            persist=persist,
            images=images,
            connection_file=connection_file,
        )

    @server.tool(
        name="execute_all_cells",
        description=EXECUTE_ALL_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Execute all notebook cells",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    def _execute_all_tool(
        path: str,
        stop_on_error: bool = True,
        timeout_s: float = 30.0,
        persist: bool = True,
        restart_kernel: bool = False,
        images: str = "discard",
        connection_file: str | None = None,
    ) -> dict[str, Any]:
        return _execute_all_cells(
            path,
            stop_on_error=stop_on_error,
            timeout_s=timeout_s,
            persist=persist,
            restart_kernel=restart_kernel,
            images=images,
            connection_file=connection_file,
        )

    @server.tool(
        name="execute_cells_in_range",
        description=EXECUTE_RANGE_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Execute notebook cell range",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    def _execute_range_tool(
        path: str,
        cell_start: int,
        cell_end: int,
        stop_on_error: bool = True,
        timeout_s: float = 30.0,
        persist: bool = True,
        restart_kernel: bool = False,
        images: str = "discard",
        connection_file: str | None = None,
    ) -> dict[str, Any]:
        return _execute_cells_in_range(
            path,
            cell_start=cell_start,
            cell_end=cell_end,
            stop_on_error=stop_on_error,
            timeout_s=timeout_s,
            persist=persist,
            restart_kernel=restart_kernel,
            images=images,
            connection_file=connection_file,
        )

    @server.tool(
        name="kernel_control",
        description=KERNEL_CONTROL_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Notebook kernel control",
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    def _kernel_control_tool(
        path: str,
        action: str = "status",
        connection_file: str | None = None,
    ) -> dict[str, Any]:
        return _kernel_control(path, action=action, connection_file=connection_file)

    return server


def main() -> None:
    """Entry point for the ``cursor-edit-mcp`` console script."""
    configure_logging()
    settings = get_settings()
    _LOGGER.info(
        "server.start",
        extra={
            "context": {
                "version": VERSION,
                "workspace_root": settings.workspace_root.as_posix(),
            }
        },
    )
    build_server().run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
