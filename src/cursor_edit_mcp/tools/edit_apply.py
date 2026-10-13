"""``edit_apply`` — apply a batch of range edits atomically.

The contract the agent can rely on:

* all line numbers refer to the file as it was when ``file_read`` returned;
* the whole batch is checked before anything is written, so a rejected batch
  leaves the file untouched;
* the file is replaced atomically, so a reader never sees a partial write;
* line endings, final newline, encoding, and BOM survive untouched.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from ..edit import indents
from ..edit.atomic import atomic_write, current_mode
from ..edit.batch import Edit, apply_batch, validate_batch
from ..edit.diffs import unified_diff
from ..edit.document import (
    TextDocument,
    parse_document,
    read_bytes,
    to_eol,
    to_lf,
    write_bytes,
)
from ..edit.hashing import hash_bytes, normalize_hash, short_hash
from ..edit.indents import INDENT_MODE_RELATIVE, INDENT_MODES
from ..errors import EditMcpError, ErrorCode, stale_hash
from ..logging_setup import get_logger
from ..security.paths import assert_mutable, resolve_path
from ..settings import Settings, get_settings
from ..validate.pipeline import ValidationRequest, ValidationResult
from ..validate.pipeline import validate as run_validation

APPLY_DESCRIPTION = """\
Apply a batch of line-range edits to one text file, atomically.

Read the file first and pass its `hash` as `base_hash`; the edit is rejected \
with STALE_HASH if the file changed since. Every line number in `edits` refers \
to that same snapshot, so edits must not overlap. Each edit may carry `expect` \
to assert what is currently at its target. Nothing is written unless the whole \
batch applies.

Use `edit_locate` to find the range instead of counting lines yourself.

`indent_mode` controls what happens to `new_text`:

- `"relative"` (default) — strip the common margin from `new_text` and re-apply \
the indentation of the surrounding block. Write code flush-left and it lands at \
the right depth, which avoids `IndentationError` from a miscounted margin.
- `"absolute"` — write `new_text` exactly as sent.

## Validation

Before writing, the edit goes through a syntax check, a range-contained format, \
and a lint pass. Only the problems **your edit introduced** are reported: \
pre-existing lint debt is counted, not blamed on you, and a warning that moved \
down a line because of your insert is not new.

`validate` chooses how strictly to react:

- `"warn"` (default) — write the edit, return any new problems.
- `"strict"` — refuse to write, returning `SYNTAX_ERROR_INTRODUCED` or \
`LINT_REGRESSION` with the diff that was rejected.
- `"off"` — skip the pipeline entirely, including format and autofix.

`format` controls formatting (`"range"` by default: only the lines you edited, \
plus two lines of margin, may change; `"full"` reformats the whole file, which \
usually is not what you want). `autofix: "safe"` applies Ruff's safe fixes to \
your edited lines and the import block.

The response carries a `diagnostics` block and a `stages` block. Read `stages`: \
a stage that could not run is `"skipped"` with a reason, so a clean result is \
never confused with an unvalidated one. If `strict` rejects the edit, fix the \
named problem and retry — do not switch to `"off"` to force it through."""

_LOGGER = get_logger(__name__)


def _render_lines(document: TextDocument, text: str) -> list[str]:
    """Re-derive lines after the formatter changed the text.

    The formatter works on whole text and may adjust the final newline, so the
    split has to be redone rather than patched in place. ``render`` treats a
    trailing empty element as a real blank line, so the artifact produced by
    splitting text that ends with a terminator has to go.
    """
    lines = text.split(document.eol)
    if document.final_newline and lines and lines[-1] == "":
        lines.pop()
    return lines


def _rejection(
    path: str, validation: ValidationResult, rejected_diff: str, truncated: bool
) -> EditMcpError:
    """Build the error for a strict-mode rejection.

    The rejected diff is included so the agent can correct its edit without
    another read.
    """
    syntax = [d for stage in validation.stages for d in stage.diagnostics if d.is_syntax]
    if syntax:
        code = ErrorCode.SYNTAX_ERROR_INTRODUCED
        message = "The edit would leave the file unparseable."
        hint = (
            "Fix the syntax error in new_text and retry. The diff below shows "
            "what was rejected."
        )
    else:
        code = ErrorCode.LINT_REGRESSION
        message = f"The edit introduces {len(validation.blocking)} new problem(s)."
        hint = (
            "Fix the reported diagnostics and retry. Existing problems that "
            "were already in the file do not block the edit."
        )

    return EditMcpError(
        code,
        f"{path}: {message}",
        hint=hint,
        details={
            "path": path,
            "diagnostics": validation.diff.as_dict(),
            "stages": validation.stage_dicts(),
            "rejected_diff": rejected_diff,
            "diff_truncated": truncated,
        },
    )


def _target_margin_for(document: TextDocument, edit: Edit) -> str:
    """The indentation the incoming text should adopt.

    A replacement inherits the margin of the first line it replaces. An insert
    inherits the margin of the following line, since that is the block the new
    code joins; inserting at the very end inherits the last line's margin.
    """
    lines = document.lines
    if edit.is_insert:
        after = int(edit.after_line or 0)
        following = after + 1
        if 1 <= following <= len(lines):
            return indents.margin_of(lines[following - 1])
        if lines:
            return indents.margin_of(lines[-1])
        return ""

    start = int(edit.start_line or 1)
    if 1 <= start <= len(lines):
        return indents.margin_of(lines[start - 1])
    return ""


def _apply_relative_indent(
    document: TextDocument, edits: list[Edit]
) -> tuple[list[Edit], int]:
    """Re-indent each edit's ``new_text``.

    ``Edit`` is frozen, so this returns a new list and the count that changed;
    the caller uses the returned list.
    """
    lines = document.lines
    style = indents.detect_style(lines)
    tabbed = indents.uses_tabs(lines)

    out: list[Edit] = []
    changed = 0
    for edit in edits:
        if not edit.new_text or edit.op == "delete":
            out.append(edit)
            continue

        margin = _target_margin_for(document, edit)
        result = indents.reindent_to(
            edit.new_text, margin=margin, style=style, path_is_tabbed=tabbed
        )
        if result != edit.new_text:
            changed += 1
            out.append(replace(edit, new_text=result))
        else:
            out.append(edit)

    return out, changed


class LineRange(BaseModel):
    """An inclusive 1-based line range."""

    start_line: int = Field(description="First line to affect, 1-based inclusive.")
    end_line: int = Field(description="Last line to affect, 1-based inclusive.")


class EditSpec(BaseModel):
    """One edit. Which fields are required depends on `op`."""

    op: Literal["replace", "insert", "delete"] = Field(
        description="'replace', 'insert', or 'delete'."
    )
    range: LineRange | None = Field(
        default=None, description="Required for 'replace' and 'delete'."
    )
    after_line: int | None = Field(
        default=None,
        description="Required for 'insert'. 0 means the start of the file.",
    )
    new_text: str | None = Field(
        default=None, description="Replacement or inserted text."
    )
    expect: str | None = Field(
        default=None,
        description="Text that must currently appear in the target range.",
    )


def edit_apply(
    path: str,
    edits: Annotated[list[EditSpec], Field(description="Edits to apply together.")],
    base_hash: str,
    dry_run: bool = False,
    indent_mode: str = "relative",
    validate: str = "warn",
    format: str = "range",
    autofix: str = "none",
    require_all_stages: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Apply ``edits`` to ``path`` if ``base_hash`` still matches the file."""
    settings = settings or get_settings()
    resolved = resolve_path(path, settings)
    assert_mutable(resolved, path, settings)

    # One read, two derivations. Parsing and hashing must see the same bytes,
    # otherwise the guard could pass against content the edit was not based on.
    original_bytes = read_bytes(resolved, path, settings.max_file_bytes)
    document = parse_document(original_bytes, path)
    current_hash = hash_bytes(original_bytes)

    if normalize_hash(base_hash) != current_hash:
        raise stale_hash(normalize_hash(base_hash), current_hash, path)

    if len(edits) > settings.max_edits:
        raise EditMcpError(
            ErrorCode.TOO_MANY_EDITS,
            f"{len(edits)} edits exceed the limit of {settings.max_edits}.",
            hint="Split the work into smaller batches.",
            details={"edit_count": len(edits), "max_edits": settings.max_edits},
        )

    parsed = [
        Edit(
            op=spec.op,
            start_line=spec.range.start_line if spec.range else None,
            end_line=spec.range.end_line if spec.range else None,
            after_line=spec.after_line,
            new_text=spec.new_text,
            expect=spec.expect,
            index=index,
        )
        for index, spec in enumerate(edits)
    ]

    validate_batch(document, parsed)

    if indent_mode not in INDENT_MODES:
        raise EditMcpError(
            ErrorCode.INVALID_EDIT,
            f"Unknown indent_mode {indent_mode!r}.",
            hint="Use 'relative' (default, re-indent to the surrounding block) "
            "or 'absolute' (keep new_text exactly as sent).",
            details={"indent_mode": indent_mode, "valid": list(INDENT_MODES)},
        )

    reindented = 0
    if indent_mode == INDENT_MODE_RELATIVE:
        parsed, reindented = _apply_relative_indent(document, parsed)

    new_lines, touched = apply_batch(document, parsed)
    new_text = document.render(new_lines)

    # Validation runs against LF text: external tools mis-parse CRLF, so the
    # file's own endings are normalized for the call and restored after.
    lf_before, lf_after = to_lf(document.text), to_lf(new_text)

    validation = run_validation(
        ValidationRequest(
            path=resolved,
            workspace_root=settings.workspace_root,
            before=lf_before,
            after=lf_after,
            spans=touched,
            validate=validate,
            format_mode=format,
            autofix=autofix,
            require_all_stages=require_all_stages,
            timeout=settings.tool_timeout_s,
        )
    )

    if validation.text != lf_after:
        new_text = to_eol(validation.text, document.eol)
        new_lines = _render_lines(document, new_text)

    if not validation.ok:
        rejected_diff, truncated = unified_diff(
            document.text, new_text, path, max_lines=40
        )
        raise _rejection(path, validation, rejected_diff, truncated)

    new_bytes = write_bytes(document, new_lines)
    new_hash = hash_bytes(new_bytes)

    changed = new_bytes != original_bytes
    diff, diff_truncated = unified_diff(document.text, new_text, path)

    if not changed:
        _LOGGER.info("edit_apply.noop", extra={"context": {"path": resolved.as_posix()}})
        return {
            "ok": True,
            "path": path,
            "hash": current_hash,
            "changed": False,
            "written": False,
            "dry_run": dry_run,
            "line_count": {"before": document.line_count, "after": len(new_lines)},
            "diff": "",
            "diff_truncated": False,
            **validation.as_dict(),
        }

    if not dry_run:
        atomic_write(resolved, new_bytes, mode=current_mode(resolved))

    _LOGGER.info(
        "edit_apply",
        extra={
            "context": {
                "path": resolved.as_posix(),
                "edits": len(parsed),
                "old_hash": short_hash(current_hash),
                "new_hash": short_hash(new_hash),
                "dry_run": dry_run,
                "validate": validate,
                "new_diagnostics": len(validation.diff.new),
            }
        },
    )

    return {
        "ok": True,
        "path": path,
        "hash": current_hash,
        "new_hash": new_hash,
        "changed": True,
        "written": not dry_run,
        "dry_run": dry_run,
        "line_count": {"before": document.line_count, "after": len(new_lines)},
        "diff": diff,
        "diff_truncated": diff_truncated,
        **validation.as_dict(),
    }
