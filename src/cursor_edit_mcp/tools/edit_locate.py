"""``edit_locate``: resolve an anchor to a line range.

Read-only. Its whole job is to let the agent name what it wants to change
instead of counting lines, and to hand back a range that is exact by the time
it reaches ``edit_apply``.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from ..edit.anchors import Span, resolve_regex_anchor, resolve_text_anchor
from ..edit.document import parse_document, read_bytes
from ..edit.hashing import hash_bytes
from ..errors import EditMcpError, ErrorCode
from ..logging_setup import get_logger
from ..profiles import markdown_blocks, python_symbols
from ..security.paths import resolve_path
from ..settings import Settings, get_settings

_LOGGER = get_logger(__name__)

BY_ANCHOR = "anchor"
BY_SYMBOL = "symbol"
BY_HEADING = "heading"
BY_MODES = (BY_ANCHOR, BY_SYMBOL, BY_HEADING)

LOCATE_DESCRIPTION = """\
Resolve an anchor to a line range in a file, without modifying it.

Use this instead of counting lines. It returns a `start_line`/`end_line` pair
(1-based, inclusive) you can pass straight to `edit_apply`, plus the file's
`hash` for the `base_hash` guard.

Three addressing modes, chosen with `by`:

- `anchor` (default) — find `text` verbatim, or a `regex`. If the text appears
  more than once the call fails as ambiguous and lists where it matched; pass
  `occurrence: 0` to take the first, 1 for the second, and so on.
- `symbol` — a Python function, method, or class by dotted name, such as
  `UserService.fetch_user`. The range includes decorators and the whole body.
- `heading` — a Markdown section, such as `## Installation`. The range runs to
  the line before the next heading of the same or higher level, so subsections
  are included; pass `include_subsections: false` to stop at the first subheading.

Examples

  {"path": "src/app.py", "by": "anchor", "text": "def fetch_user("}
  {"path": "src/app.py", "by": "anchor", "regex": r"class \\w+Service:", "occurrence": 1}
  {"path": "src/app.py", "by": "symbol", "name": "UserService.fetch_user"}
  {"path": "README.md", "by": "heading", "text": "Installation"}

When a lookup fails, the error lists the candidates that did match, so a near
miss is usually a one-line correction rather than another read."""


class LocateArgs(BaseModel):
    path: str
    by: Literal["anchor", "symbol", "heading"] = BY_ANCHOR
    text: str | None = Field(default=None, description="Anchor text or heading title.")
    regex: str | None = Field(default=None, description="Regular expression anchor.")
    name: str | None = Field(default=None, description="Dotted Python symbol name.")
    occurrence: int | None = Field(
        default=None,
        ge=0,
        description="0-based index of the match to use when several match.",
    )
    include_subsections: bool = Field(
        default=True, description="Heading mode: include nested subsections."
    )
    include_text: bool = Field(
        default=False, description="Return the resolved text as well as the range."
    )

    @model_validator(mode="after")
    def _check_selector(self) -> LocateArgs:
        if self.by == BY_SYMBOL and not self.name:
            raise ValueError("by='symbol' requires name, e.g. 'Class.method'.")
        if self.by == BY_ANCHOR:
            if self.regex is not None and self.text is not None:
                raise ValueError("Pass either text or regex, not both.")
            if self.regex is None and not self.text:
                raise ValueError("by='anchor' requires either text or regex.")
        if self.by == BY_HEADING and not self.text:
            raise ValueError("by='heading' requires text, the heading title.")
        if self.occurrence is not None and self.by != BY_ANCHOR:
            raise ValueError("occurrence only applies to by='anchor'.")
        return self


def _validate_settings(settings: Settings) -> None:
    del settings  # signature kept for symmetry with the other tools


def _argument_hint(
    by: str, text: str | None, regex: str | None, name: str | None, occurrence: int | None
) -> str:
    """Turn a selector mistake into the corrected call."""
    if by == BY_SYMBOL:
        return "Pass a dotted Python name, e.g. name: 'ClassName.method_name'."
    if by == BY_HEADING:
        return "Pass the heading title as text, e.g. text: 'Installation'."
    if text and regex:
        return "Pass either text or regex, not both."
    if occurrence is not None:
        return "occurrence only applies to by='anchor'."
    return "Pass text (verbatim) or regex to locate an anchor."


def _validation_reasons(exc: ValidationError) -> list[str]:
    return [
        " ".join(str(part["msg"]) for part in error.get("loc", ()))
        for error in exc.errors()
    ]


def edit_locate(
    path: str,
    by: str = BY_ANCHOR,
    text: str | None = None,
    regex: str | None = None,
    name: str | None = None,
    occurrence: int | None = None,
    include_subsections: bool = True,
    include_text: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Resolve one anchor to a line range."""
    try:
        args = LocateArgs(
            path=path,
            by=by,  # type: ignore[arg-type]
            text=text,
            regex=regex,
            name=name,
            occurrence=occurrence,
            include_subsections=include_subsections,
            include_text=include_text,
        )
    except ValidationError as exc:
        # Re-raise as a tool error so the model sees a hint instead of a
        # pydantic trace. The selector rules are usage guidance, not a crash.
        raise EditMcpError(
            ErrorCode.INVALID_EDIT,
            "Invalid edit_locate arguments.",
            hint=_argument_hint(by, text, regex, name, occurrence),
            details={"reasons": _validation_reasons(exc)},
        ) from exc

    settings = settings or get_settings()
    resolved = resolve_path(path, settings)

    data = read_bytes(resolved, path, settings.max_file_bytes)
    document = parse_document(data, path)
    lines = document.lines

    if args.by == BY_SYMBOL:
        assert args.name is not None  # guaranteed by LocateArgs
        span = python_symbols.resolve_symbol(document.text, name=args.name, path=path)
    elif args.by == BY_HEADING:
        assert args.text is not None  # guaranteed by LocateArgs
        span = markdown_blocks.resolve_heading(
            lines, text=args.text, path=path, include_subsections=args.include_subsections
        )
    else:
        span = _resolve_anchor(document, args, path)

    result: dict[str, Any] = {
        "ok": True,
        "path": path,
        "by": args.by,
        "start_line": span.start_line,
        "end_line": span.end_line,
        "match": span.match,
        "line_count": document.line_count,
        "hash": hash_bytes(data),
        "short_hash": hash_bytes(data)[len("sha256:") :][:12],
    }

    if args.include_text:
        body = "\n".join(lines[span.start_line - 1 : span.end_line])
        result["text"] = body[: settings.max_content_chars]

    _LOGGER.info(
        "edit_locate",
        extra={
            "context": {
                "path": resolved.as_posix(),
                "by": args.by,
                "start_line": span.start_line,
                "end_line": span.end_line,
            }
        },
    )
    return result


def _resolve_anchor(document, args: LocateArgs, path: str) -> Span:
    if args.regex is not None:
        return resolve_regex_anchor(
            document.text,
            pattern=args.regex,
            path=path,
            occurrence=args.occurrence,
            flags=re.MULTILINE,
        )
    assert args.text is not None  # guaranteed by LocateArgs
    return resolve_text_anchor(
        document.text, query=args.text, path=path, occurrence=args.occurrence
    )
