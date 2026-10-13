"""``file_create``: create a file with the same validation and path rules.

A new file is the one edit with no "before" state, so its baseline is empty:
every diagnostic it produces is new by definition. That makes ``strict`` mean
exactly what a user expects here — do not create a file that does not parse.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..edit.atomic import atomic_write
from ..edit.document import detect_eol, to_eol, to_lf
from ..edit.hashing import hash_bytes, short_hash
from ..errors import EditMcpError, ErrorCode
from ..logging_setup import get_logger
from ..security.paths import assert_mutable, resolve_path
from ..settings import Settings, get_settings
from ..validate.pipeline import (
    AUTOFIX_NONE,
    FORMAT_RANGE,
    VALIDATE_STRICT,
    ValidationRequest,
    ValidationResult,
)
from ..validate.pipeline import validate as run_validation

_LOGGER = get_logger(__name__)

CREATE_DESCRIPTION = """\
Create a new text file, validated before it is written.

Fails with FILE_EXISTS if the path is already taken, unless `overwrite` is set.

The same validation pipeline as `edit_apply` runs first. Because a new file has
no previous version, there is no baseline to diff against, so every diagnostic
it produces counts as new: with `validate: "strict"` (the default) a file that
does not parse is not created.

Parent directories are created as needed. `new_text` may end with a trailing
newline, which is preserved as written."""


def file_create(
    path: str,
    new_text: str,
    overwrite: bool = False,
    validate: str = VALIDATE_STRICT,
    format: str = FORMAT_RANGE,
    autofix: str = AUTOFIX_NONE,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Create ``path`` with ``new_text``, writing only if validation allows."""
    settings = settings or get_settings()
    resolved = resolve_path(path, settings)
    assert_mutable(resolved, path, settings)

    if resolved.exists():
        if not overwrite:
            raise EditMcpError(
                ErrorCode.FILE_EXISTS,
                f"{path} already exists.",
                hint=(
                    "Edit it with edit_apply instead, or pass overwrite: true "
                    "to replace it."
                ),
                details={"path": path},
            )
        if not resolved.is_file():
            raise EditMcpError(
                ErrorCode.NOT_A_FILE,
                f"{path} exists and is not a regular file.",
                hint="Choose a different path.",
                details={"path": path},
            )

    # Match the surrounding repository's conventions so a new file does not
    # arrive with the wrong line endings.
    eol = _dominant_eol_for_new_file(resolved, settings.workspace_root)
    body = to_eol(to_lf(new_text), eol)
    if new_text and not new_text.endswith(("\n", "\r")):
        body += eol

    subject = to_lf(body)
    line_count = len(subject.split("\n"))

    validation = run_validation(
        ValidationRequest(
            path=resolved,
            workspace_root=settings.workspace_root,
            # Empty baseline: every diagnostic in a brand-new file is new.
            before="",
            after=subject,
            spans=[(1, line_count)],
            validate=validate,
            format_mode=format,
            autofix=autofix,
            timeout=settings.tool_timeout_s,
        )
    )

    if not validation.ok:
        raise _rejection(path, validation, subject)

    final = to_eol(validation.text, eol)
    data = final.encode(_encoding_for(resolved))
    atomic_write(resolved, data)

    _LOGGER.info(
        "file_create",
        extra={
            "context": {
                "path": resolved.as_posix(),
                "bytes": len(data),
                "validate": validate,
            }
        },
    )

    return {
        "ok": True,
        "path": path,
        "created": True,
        "overwritten": overwrite,
        "bytes": len(data),
        "line_count": _count_lines(final, eol),
        "hash": hash_bytes(data),
        "short_hash": short_hash(hash_bytes(data)),
        "formatted": validation.formatted,
        "diagnostics": validation.diff.as_dict(),
        "stages": validation.stage_dicts(),
    }


def _rejection(path: str, validation: ValidationResult, text: str) -> EditMcpError:
    syntax = [d for stage in validation.stages for d in stage.diagnostics if d.is_syntax]
    code = ErrorCode.SYNTAX_ERROR_INTRODUCED if syntax else ErrorCode.LINT_REGRESSION
    message = (
        "The content is not valid Python." if syntax else "The content has lint errors."
    )
    return EditMcpError(
        code,
        f"{path} was not created: {message}",
        hint=(
            'Fix the reported problems and retry, or pass validate: "warn" to '
            "create it anyway."
        ),
        details={
            "path": path,
            "diagnostics": validation.diff.as_dict(),
            "stages": validation.stage_dicts(),
        },
    )


def _dominant_eol_for_new_file(path: Path, workspace_root: Path) -> str:
    """Guess a new file's line endings from its neighbours."""
    try:
        siblings = [p for p in path.parent.iterdir() if p.is_file()][:50]
    except OSError:
        return "\n"

    counts = {"\r\n": 0, "\n": 0}
    for sibling in siblings:
        if sibling.suffix not in (".py", ".pyi", ".md", ".markdown"):
            continue
        try:
            head = sibling.read_bytes()[:8192]
        except OSError:
            continue
        detected = detect_eol(head.decode("utf-8", errors="replace"))
        counts[detected] = counts.get(detected, 0) + 1

    return "\r\n" if counts["\r\n"] > counts["\n"] else "\n"


def _encoding_for(path: Path) -> str:
    """Use UTF-8 unless the directory's Python neighbours say otherwise."""
    try:
        for sibling in path.parent.iterdir():
            if sibling.suffix in (".py", ".pyi") and sibling.is_file():
                head = sibling.read_bytes()[:200].decode("utf-8", errors="replace")
                for line in head.split("\n")[:2]:
                    if "coding:" in line:
                        return line.split("coding:")[1].strip().strip("\"'")
                break
    except OSError:  # pragma: no cover - unreadable directory
        pass
    return "utf-8"


def _count_lines(text: str, eol: str) -> int:
    body = text[: -len(eol)] if text.endswith(eol) else text
    return len(body.split(eol)) if body else 0
