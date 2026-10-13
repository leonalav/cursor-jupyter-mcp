"""``validate_file``: run the validation pipeline without editing.

Lets the agent check its work before committing to an edit, and lets a user ask
"would this file pass?" without changing anything.
"""

from __future__ import annotations

from typing import Any

from ..edit.document import parse_document, read_bytes, to_lf
from ..logging_setup import get_logger
from ..security.paths import resolve_path
from ..settings import Settings, get_settings
from ..validate.pipeline import (
    AUTOFIX_NONE,
    FORMAT_NONE,
    VALIDATE_WARN,
    ValidationRequest,
    validate,
)

_LOGGER = get_logger(__name__)

VALIDATE_DESCRIPTION = """\
Check a file, or a block of proposed content, without modifying anything.

Use this before committing to an edit when you are unsure whether the change is
clean, and to confirm a file's state on its own.

`path` is the file being validated. Passing `content` validates that text *as if*
it were the file's contents, which is how you check a replacement before
applying it.

Every problem is reported, not only the new ones: there is no "before" version
to diff against when you are validating content that does not exist yet. To
find out which problems *your edit* introduced, apply the edit with
`validate: "warn"` and read `diagnostics` in the response.

Reports the effective tooling alongside the results, so a clean result from a
skipped stage is never mistaken for a clean file. A stage that could not run
appears in `stages` with `status: "skipped"` and the reason."""


def validate_file(
    path: str,
    content: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Validate a file or proposed content, read-only."""
    settings = settings or get_settings()
    resolved = resolve_path(path, settings)

    if content is not None:
        subject = to_lf(content)
        origin = "proposed"
        line_count = len(subject.split("\n"))
    else:
        data = read_bytes(resolved, path, settings.max_file_bytes)
        document = parse_document(data, path)
        subject = to_lf(document.text)
        origin = "file"
        line_count = document.line_count

    result = validate(
        ValidationRequest(
            path=resolved,
            workspace_root=settings.workspace_root,
            # Empty baseline on purpose: with nothing to compare against, every
            # diagnostic is reported. Passing the content as both before and
            # after would make each finding cancel itself out.
            before="",
            after=subject,
            # Everything is "touched" for content that has no previous version,
            # so a formatter is free to normalize all of it.
            spans=[(1, line_count)],
            validate=VALIDATE_WARN,
            format_mode=FORMAT_NONE,
            autofix=AUTOFIX_NONE,
            timeout=settings.tool_timeout_s,
        )
    )

    from ..config.resolution import resolve_all  # local import: avoids a cycle

    config = resolve_all(resolved, settings.workspace_root)
    diagnostics = result.diff.new

    return {
        "ok": not diagnostics,
        "path": path,
        "validated": origin,
        "line_count": line_count,
        "diagnostics": {
            "new": [d.as_dict() for d in diagnostics[:50]],
            "new_count": len(diagnostics),
            "new_error_count": len(result.blocking),
            "truncated": len(diagnostics) > 50,
        },
        "stages": result.stage_dicts(),
        "tooling": config.as_dict(),
    }
