"""Python symbol addressing.

Resolves a dotted name to the line range of its definition, decorators
included. Built on ``ast`` so the range comes from the parser's own line
information rather than from text matching, which cannot be fooled by a
similarly named string literal or comment.
"""

from __future__ import annotations

import ast
import difflib
from dataclasses import dataclass

from ..edit.anchors import Span
from ..errors import EditMcpError, anchor_not_found

#: Node types that can be named in a dotted path.
_DEFINITION_NODES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
)

#: Nesting depth beyond this is almost certainly a mistake in the request.
_MAX_DEPTH = 6


@dataclass(frozen=True)
class SymbolDefinition:
    """One definition found in the file, at any nesting depth."""

    qualified_name: str
    start_line: int
    end_line: int
    kind: str
    docstring: str | None

    @property
    def leaf_name(self) -> str:
        return self.qualified_name.rsplit(".", 1)[-1]

    def as_candidate(self) -> dict[str, object]:
        first_line = self.docstring.splitlines()[0] if self.docstring else ""
        return {
            "name": self.qualified_name,
            "kind": self.kind,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "first_line": first_line[:160],
        }


def _kind_of(node: ast.AST) -> str:
    if isinstance(node, ast.ClassDef):
        return "class"
    if isinstance(node, ast.AsyncFunctionDef):
        return "async function"
    return "function"


def _definition_span(node: ast.AST) -> tuple[int, int]:
    """Start line including decorators, end line including the body."""
    start = int(node.lineno)  # type: ignore[attr-defined]
    end = int(node.end_lineno or node.lineno)  # type: ignore[attr-defined]

    for decorator in getattr(node, "decorator_list", []):
        start = min(start, int(decorator.lineno))

    return start, end


def _docstring_of(node: ast.AST) -> str | None:
    try:
        return ast.get_docstring(node)  # type: ignore[arg-type]
    except TypeError:  # pragma: no cover - node without a body
        return None


def iter_symbols(tree: ast.Module) -> list[SymbolDefinition]:
    """Every function and class in the file, with dotted qualified names."""

    found: list[SymbolDefinition] = []

    def walk(node: ast.AST, prefix: str, depth: int) -> None:
        if depth > _MAX_DEPTH:
            return
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, _DEFINITION_NODES):
                walk(child, prefix, depth)
                continue

            name = child.name
            qualified = f"{prefix}{name}"
            start, end = _definition_span(child)
            found.append(
                SymbolDefinition(
                    qualified_name=qualified,
                    start_line=start,
                    end_line=end,
                    kind=_kind_of(child),
                    docstring=_docstring_of(child),
                )
            )
            walk(child, f"{qualified}.", depth + 1)

    walk(tree, "", 0)
    return found


def parse_module(text: str, *, path: str) -> ast.Module:
    """Parse Python source, converting a syntax error into a tool error."""
    try:
        return ast.parse(text)
    except SyntaxError as exc:
        raise EditMcpError(
            "DECODE_ERROR",
            f"{path} is not valid Python: {exc.msg}.",
            hint=(
                "Fix the syntax error first. edit_locate needs a parseable file "
                "to read symbol line information."
            ),
            details={
                "path": path,
                "line": exc.lineno,
                "column": exc.offset,
                "reason": exc.msg,
            },
        ) from exc


def resolve_symbol(text: str, *, name: str, path: str, include_body: bool = True) -> Span:
    """Resolve a dotted symbol name such as ``UserService.fetch_user``.

    An exact qualified-name match wins. Failing that, a bare name is allowed to
    match any definition with that final component, which is what an agent means
    when it says ``fetch_user`` and only one such function exists.
    """
    if not name.strip():
        raise EditMcpError(
            "INVALID_EDIT",
            "Symbol name is empty.",
            hint="Pass name: 'ClassName.method_name' or name: 'function_name'.",
            details={"path": path},
        )

    tree = parse_module(text, path=path)
    symbols = iter_symbols(tree)

    exact = [s for s in symbols if s.qualified_name == name.strip()]
    if exact:
        chosen = exact[0]
    else:
        leaf = name.strip().rsplit(".", 1)[-1]
        by_leaf = [s for s in symbols if s.leaf_name == leaf]
        if len(by_leaf) == 1:
            chosen = by_leaf[0]
        elif not by_leaf:
            raise _no_match(path, name, symbols)
        else:
            candidates = [s.as_candidate() for s in by_leaf[:10]]
            locations = ", ".join(
                f"{c['name']} (line {c['start_line']})" for c in candidates
            )
            raise anchor_not_found(
                path,
                f"symbol {name!r}",
                hint=(
                    f"{leaf!r} is defined {len(by_leaf)} times. "
                    f"Use the qualified name: {locations}"
                ),
                candidates=candidates,
            )

    end_line = chosen.end_line if include_body else chosen.start_line
    return Span(
        start_line=chosen.start_line,
        end_line=end_line,
        match=f"{chosen.kind} {chosen.qualified_name}",
        occurrence=0,
    )


def _no_match(path: str, name: str, symbols: list[SymbolDefinition]) -> EditMcpError:
    """Build an error that points at the symbols the file actually has.

    A wrong or invented symbol name is a common failure. Listing the real
    definitions turns it into a correction rather than another read.
    """
    leaf = name.strip().rsplit(".", 1)[-1]

    # Compare against the final component, since the caller may have written
    # either "fetch_user" or "UserService.fetch_user". Comparing a leaf against
    # dotted names would never match.
    close = difflib.get_close_matches(
        leaf, [s.leaf_name for s in symbols], n=5, cutoff=0.7
    )
    if close:
        by_leaf = {s.leaf_name: s for s in symbols}
        candidates = [
            by_leaf[name_].as_candidate() for name_ in close if name_ in by_leaf
        ]
        suggestions = ", ".join(
            by_leaf[name_].qualified_name for name_ in close if name_ in by_leaf
        )
        return anchor_not_found(
            path,
            f"symbol {name!r}",
            hint=f"Did you mean: {suggestions}?",
            candidates=candidates[:10],
        )

    top_level = [s.qualified_name for s in symbols if "." not in s.qualified_name]
    hint = (
        f"This file defines {len(symbols)} symbol(s), including: "
        f"{', '.join(top_level[:8])}."
        if top_level
        else "This file defines no functions or classes."
    )
    return anchor_not_found(path, f"symbol {name!r}", hint=hint)


def outline(text: str, *, path: str) -> list[dict[str, object]]:
    """All definitions, for the not-found hint and for future inspection tools."""
    tree = parse_module(text, path=path)
    return [s.as_candidate() for s in iter_symbols(tree)]
