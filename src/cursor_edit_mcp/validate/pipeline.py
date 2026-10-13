"""The validation pipeline (FR-8, §3.5).

Stages run in order and each can be skipped, either by mode or by missing
tooling:

1. **syntax** — Ruff's parser for Python, structural checks for Markdown;
2. **format** — range-contained, so an edit never reformats untouched code;
3. **lint** — run before and after, and report only what is new.

Lint runs on *both* versions of the file. That is what makes it possible to edit
a file that already has lint debt: the pre-existing problems are not reported as
new, so they do not block the write, and the agent is not sent chasing warnings
it did not cause.

The output of the format stage is fed into the lint stage, so diagnostics are
measured against the text that would actually be written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config.resolution import FileConfig, resolve_all
from ..errors import EditMcpError, ErrorCode
from ..profiles import markdown_profile, python_symbols
from ..runners import markdownlint, prettier, ruff
from ..runners.base import Resolver
from .diagnostics import (
    BaselineDiff,
    Diagnostic,
    StageResult,
    diff,
    stage_ok,
    stage_skipped,
)

VALIDATE_OFF = "off"
VALIDATE_WARN = "warn"
VALIDATE_STRICT = "strict"
VALIDATE_MODES = (VALIDATE_OFF, VALIDATE_WARN, VALIDATE_STRICT)

FORMAT_NONE = "none"
FORMAT_RANGE = "range"
FORMAT_FULL = "full"
FORMAT_MODES = (FORMAT_NONE, FORMAT_RANGE, FORMAT_FULL)

AUTOFIX_NONE = "none"
AUTOFIX_SAFE = "safe"
AUTOFIX_MODES = (AUTOFIX_NONE, AUTOFIX_SAFE)

PYTHON_SUFFIXES = (".py", ".pyi")
MARKDOWN_SUFFIXES = (".md", ".markdown")

#: Extra lines around the edit that a formatter may legitimately touch.
FORMAT_MARGIN = 2


@dataclass
class ValidationRequest:
    """One validation run."""

    path: Path
    workspace_root: Path
    before: str
    after: str
    spans: list[tuple[int, int]] = field(default_factory=list)
    validate: str = VALIDATE_WARN
    format_mode: str = FORMAT_RANGE
    autofix: str = AUTOFIX_NONE
    timeout: float = 10.0
    require_all_stages: bool = False


@dataclass
class ValidationResult:
    """What the pipeline decided, and what the caller should write."""

    text: str
    ok: bool
    diff: BaselineDiff
    stages: list[StageResult]
    formatted: bool
    autofixed: bool

    @property
    def blocking(self) -> tuple[Diagnostic, ...]:
        return self.diff.new_errors

    def stage_dicts(self) -> list[dict[str, object]]:
        return [stage.as_dict() for stage in self.stages]

    def skipped_reasons(self) -> list[str]:
        return [s.reason for s in self.stages if s.status == "skipped" and s.reason]

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "diagnostics": self.diff.as_dict(),
            "stages": self.stage_dicts(),
            "formatted": self.formatted,
            "autofixed": self.autofixed,
        }


def validate(request: ValidationRequest) -> ValidationResult:
    """Run the pipeline and decide whether the edit may be written."""
    _check_modes(request)

    # `off` disables the *checking* stages (syntax and lint). Formatting and
    # autofix are transformations the caller asked for separately, so they
    # still run: `validate` answers "may this be written", while `format` and
    # `autofix` answer "what should it look like". Turning off one must not
    # silently turn off the other two.
    checking = request.validate != VALIDATE_OFF

    resolver = Resolver(request.workspace_root)
    config = resolve_all(request.path, request.workspace_root)
    kind = classify(request.path)

    stages: list[StageResult] = []

    # --- stage 1: syntax -------------------------------------------------
    before_syntax: tuple[Diagnostic, ...] = ()
    after_syntax: tuple[Diagnostic, ...] = ()
    if checking:
        before_syntax = run_syntax(request.before, request, resolver, kind, config)
        after_syntax = run_syntax(request.after, request, resolver, kind, config)
        stages.append(_syntax_stage(after_syntax, request))
    else:
        stages.append(stage_skipped("syntax", "validate is 'off'"))

    # A file that cannot be parsed produces a cascade of meaningless lint
    # results, so nothing after this point is trustworthy. The remaining stages
    # are skipped, but whether the edit is *written* is still the mode's call:
    # only `strict` refuses, while `warn` applies and reports loudly.
    if _has_syntax_errors(after_syntax):
        syntax_diff = diff(before_syntax, after_syntax)
        # Only `strict` refuses to write. `warn` applies the edit and reports
        # loudly, which is what the PRD's mode table specifies.
        ok = request.validate != VALIDATE_STRICT or not syntax_diff.has_new_errors
        return ValidationResult(
            text=request.after,
            ok=ok,
            diff=syntax_diff,
            stages=stages,
            formatted=False,
            autofixed=False,
        )

    working = request.after

    # --- stage 2: format (range-contained) -------------------------------
    formatted = False
    if request.format_mode != FORMAT_NONE:
        working, formatted, format_stage = run_format(
            working, request, resolver, kind, config
        )
        stages.append(format_stage)
    else:
        stages.append(stage_skipped("format", "format mode is 'none'"))

    # --- optional: safe autofix ------------------------------------------
    autofixed = False
    if request.autofix == AUTOFIX_SAFE:
        working, autofixed, autofix_stage = run_autofix(working, request, resolver)
        stages.append(autofix_stage)

    # --- stage 3: lint with baseline diff --------------------------------
    if checking:
        before_lint = run_lint(request.before, request, resolver, kind, config)
        after_lint = run_lint(working, request, resolver, kind, config)
        stages.append(_lint_stage(after_lint, request))
    else:
        before_lint = after_lint = ()
        stages.append(stage_skipped("lint", "validate is 'off'"))

    baseline = diff(before_lint + before_syntax, after_lint + after_syntax)

    ok = True
    if request.validate == VALIDATE_STRICT:
        ok = not baseline.has_new_errors
        if request.require_all_stages:
            ok = ok and _all_ran(stages, request)

    return ValidationResult(
        text=working,
        ok=ok,
        diff=baseline,
        stages=stages,
        formatted=formatted,
        autofixed=autofixed,
    )


# --- classification -------------------------------------------------------


def classify(path: Path) -> str:
    """Which profile applies, if any."""
    suffix = path.suffix.lower()
    if suffix in PYTHON_SUFFIXES:
        return "python"
    if suffix in MARKDOWN_SUFFIXES:
        return "markdown"
    return "other"


# --- stages ---------------------------------------------------------------


def run_syntax(
    text: str,
    request: ValidationRequest,
    resolver: Resolver,
    kind: str,
    config: FileConfig,
) -> tuple[Diagnostic, ...]:
    if kind == "python":
        return python_syntax(text, request.path, resolver, timeout=request.timeout)
    if kind == "markdown":
        return markdown_profile.check_as_diagnostics(text)
    # Other languages get no syntax stage in v1; that is the PRD's P2 scope.
    return ()


def python_syntax(
    text: str, path: Path, resolver: Resolver, *, timeout: float = 10.0
) -> tuple[Diagnostic, ...]:
    """Parse Python with Ruff, falling back to ``ast`` when Ruff is absent.

    The fallback matters: without it, a machine without Ruff would silently skip
    the syntax stage, and ``strict`` would let a syntax-breaking edit through.
    """
    result = ruff.check(text, path=path, resolver=resolver, timeout=timeout)
    if result.ran and not result.outcome.timed_out and result.outcome.error is None:
        return tuple(d for d in result.diagnostics if d.is_syntax)
    return _ast_fallback(text, path)


def _ast_fallback(text: str, path: Path) -> tuple[Diagnostic, ...]:
    lines = text.split("\n")
    try:
        python_symbols.parse_module(text, path=str(path))
    except EditMcpError as exc:
        line = int(exc.details.get("line") or 1)
        return (
            Diagnostic(
                tool="ast",
                code="SyntaxError",
                message=str(exc.details.get("reason") or exc.message),
                severity="error",
                line=line,
                column=int(exc.details.get("column") or 1),
                source_line=lines[line - 1] if 1 <= line <= len(lines) else "",
                is_syntax=True,
            ),
        )
    return ()


def run_format(
    text: str,
    request: ValidationRequest,
    resolver: Resolver,
    kind: str,
    config: FileConfig,
) -> tuple[str, bool, StageResult]:
    """Format, keeping changes inside the edited ranges.

    The range handed to the formatter must stop at the end of the edit. Letting
    it run to the end of the file would reformat exactly the untouched code this
    milestone exists to protect.
    """
    if not request.spans:
        return text, False, stage_skipped("format", "no edited ranges to format")

    if request.format_mode == FORMAT_FULL:
        start, end = 1, _max_line(text)
    else:
        start = max(1, min(s for s, _ in request.spans) - FORMAT_MARGIN)
        end = max(e for _, e in request.spans) + FORMAT_MARGIN

    if kind == "python":
        formatted, outcome = ruff.format_range(
            text,
            path=request.path,
            start_line=start,
            end_line=end,
            resolver=resolver,
            timeout=request.timeout,
        )
        if not outcome.ran:
            return text, False, stage_skipped("format", outcome.reason or "ruff")
        changed = formatted != text
        return formatted, changed, stage_ok("format")

    if kind == "markdown":
        if not config.prettier.configured:
            return (
                text,
                False,
                stage_skipped(
                    "format", config.prettier.reason or "Prettier is not configured."
                ),
            )
        result = prettier.format_markdown(
            text, path=request.path, resolver=resolver, timeout=request.timeout
        )
        if not result.outcome.ran:
            return (
                text,
                False,
                stage_skipped("format", result.outcome.reason or "prettier"),
            )
        filtered = prettier.hunk_filter(text, result.text, request.spans, FORMAT_MARGIN)
        return filtered, filtered != text, stage_ok("format")

    return text, False, stage_skipped("format", f"no formatter for {kind} files")


def run_autofix(
    text: str, request: ValidationRequest, resolver: Resolver
) -> tuple[str, bool, StageResult]:
    """Apply Ruff's safe fixes, limited to the edit's lines and the import block."""
    if classify(request.path) != "python":
        return text, False, stage_skipped("autofix", "autofix applies to Python only")

    allowed = set()
    for start, end in request.spans:
        allowed.update(range(start, end + 1))
    allowed |= import_block_lines(text)

    fixed, _diagnostics, outcome = ruff.safe_fix(
        text,
        path=request.path,
        resolver=resolver,
        timeout=request.timeout,
        allowed_lines=allowed,
    )
    if not outcome.ran:
        return text, False, stage_skipped("autofix", outcome.reason or "ruff")
    return fixed, fixed != text, stage_ok("autofix")


def import_block_lines(text: str) -> set[int]:
    """Line numbers of the file's imports.

    Safe fixes are allowed in the import block because that is where import
    sorting lives, and Ruff's import fix necessarily touches the whole block.
    """
    import ast

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return lines


def run_lint(
    text: str,
    request: ValidationRequest,
    resolver: Resolver,
    kind: str,
    config: FileConfig,
) -> tuple[Diagnostic, ...]:
    if kind == "python":
        result = ruff.check(
            text, path=request.path, resolver=resolver, timeout=request.timeout
        )
        return result.diagnostics if result.ran else ()

    if kind == "markdown":
        if not config.markdownlint.configured:
            return ()
        result = markdownlint.lint(
            text,
            path=request.path,
            resolver=resolver,
            config_dir=(
                config.markdownlint.config_path.parent
                if config.markdownlint.config_path
                else None
            ),
            timeout=request.timeout + 5.0,
        )
        return result.diagnostics if result.outcome.ran else ()

    return ()


# --- helpers --------------------------------------------------------------


def _check_modes(request: ValidationRequest) -> None:
    if request.validate not in VALIDATE_MODES:
        raise EditMcpError(
            ErrorCode.INVALID_EDIT,
            f"Unknown validate mode {request.validate!r}.",
            hint="Use 'off', 'warn', or 'strict'.",
            details={"validate": request.validate, "valid": list(VALIDATE_MODES)},
        )
    if request.format_mode not in FORMAT_MODES:
        raise EditMcpError(
            ErrorCode.INVALID_EDIT,
            f"Unknown format mode {request.format_mode!r}.",
            hint="Use 'none', 'range', or 'full'.",
            details={"format": request.format_mode, "valid": list(FORMAT_MODES)},
        )
    if request.autofix not in AUTOFIX_MODES:
        raise EditMcpError(
            ErrorCode.INVALID_EDIT,
            f"Unknown autofix mode {request.autofix!r}.",
            hint="Use 'none' or 'safe'.",
            details={"autofix": request.autofix, "valid": list(AUTOFIX_MODES)},
        )


def _syntax_stage(
    diagnostics: tuple[Diagnostic, ...], request: ValidationRequest
) -> StageResult:
    kind = classify(request.path)
    if kind == "other":
        return stage_skipped("syntax", "no syntax profile for this file type")
    errors = tuple(d for d in diagnostics if d.is_syntax)
    if errors:
        return StageResult(name="syntax", status="failed", diagnostics=errors)
    return stage_ok("syntax")


def _lint_stage(
    diagnostics: tuple[Diagnostic, ...], request: ValidationRequest
) -> StageResult:
    kind = classify(request.path)
    if kind == "other":
        return stage_skipped("lint", "no linter for this file type")
    if kind == "markdown" and not _markdownlint_configured(request):
        return stage_skipped("lint", "markdownlint is not configured for this project")
    return stage_ok("lint")


def _markdownlint_configured(request: ValidationRequest) -> bool:
    return resolve_all(request.path, request.workspace_root).markdownlint.configured


def _has_syntax_errors(diagnostics: tuple[Diagnostic, ...]) -> bool:
    return any(d.is_syntax for d in diagnostics)


def _all_ran(stages: list[StageResult], request: ValidationRequest) -> bool:
    """Whether every expected stage actually ran.

    Only consulted in ``strict`` mode with ``require_all_stages``, where a
    missing tool should fail the edit rather than let it through unvalidated.
    """
    return not any(s.status == "skipped" for s in stages)


def _max_line(text: str) -> int:
    return len(text.split("\n"))
