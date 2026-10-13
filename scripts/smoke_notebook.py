"""End-to-end smoke test for the notebook tools.

Steps:
1. Build a fresh test.ipynb from scratch: 1 markdown cell + 5 code cells.
2. Execute cell 2 (first Python cell).
3. Execute cells 3-6 in range.
4. Execute all cells.
5. Grep every cell's output text.

The notebook is left on disk with the cells we wrote, and a snapshot of
the final state is printed so the test can be eyeballed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from cursor_edit_mcp.settings import Settings
from cursor_edit_mcp.tools.execute import (
    execute_all_cells,
    execute_cell,
    execute_cells_in_range,
)
from cursor_edit_mcp.tools.nb_cells import (
    nb_cell_insert,
    nb_outline,
)


def _output_text(o: dict) -> str:
    """Pull the printable text out of one nbformat output dict."""
    if "text" in o:
        v = o["text"]
        return "".join(v) if isinstance(v, list) else v
    if "data" in o and "text/plain" in o["data"]:
        v = o["data"]["text/plain"]
        return "".join(v) if isinstance(v, list) else v
    return f"<{o.get('output_type', '?')}>"


def main() -> int:
    workspace = Path(__file__).resolve().parent.parent
    settings = Settings(workspace_root=workspace)
    nb_path = "test.ipynb"

    # Reset: write a minimal empty notebook.
    minimal = {
        "cells": [],
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            }
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    Path(workspace / nb_path).write_text(
        json.dumps(minimal, indent=1), encoding="utf-8"
    )
    print(f"== reset {nb_path} to empty ==")

    # 1. Build 1 markdown + 5 code cells.
    nb_cell_insert(
        path=nb_path,
        source=(
            "# Smoke test\n\n"
            "A markdown cell to anchor the notebook and confirm the "
            "outline / insert tools work end-to-end.\n"
        ),
        cell_type="markdown",
        at_start=True,
        validate="off",
        settings=settings,
    )

    code_cells = [
        "x = 41\ny = 1\nx + y\n",
        "import math\nmath.sqrt(144)\n",
        "def greet(name):\n    return f'hello, {name}!'\n\ngreet('world')\n",
        "for i in range(3):\n    print(f'loop {i}')\n",
        "total = sum(range(1, 11))\ntotal\n",
    ]
    for code in code_cells:
        nb_cell_insert(
            path=nb_path,
            source=code,
            cell_type="code",
            at_end=True,
            validate="off",
            settings=settings,
        )

    outline = nb_outline(path=nb_path, settings=settings)
    print("== outline after build ==")
    print(json.dumps(outline, indent=2, default=str))

    # 2. Execute cell 2 (the first Python cell).
    print("== execute_cell(cell_number=2) ==")
    res_one = execute_cell(
        path=nb_path, cell_number=2, persist=True, settings=settings
    )
    print(json.dumps(res_one, indent=2, default=str))

    # 3. Execute cells 3-6 (the rest) in range.
    print("== execute_cells_in_range(3, 6) ==")
    res_range = execute_cells_in_range(
        path=nb_path, cell_start=3, cell_end=6, persist=True, settings=settings
    )
    print(json.dumps(res_range, indent=2, default=str))

    # 4. Execute all cells.
    print("== execute_all_cells() ==")
    res_all = execute_all_cells(
        path=nb_path, persist=True, settings=settings
    )
    print(json.dumps(res_all, indent=2, default=str))

    # 5. Grep every cell's output contents.
    print("== output contents (grep over every cell) ==")
    nb_text = Path(workspace / nb_path).read_text(encoding="utf-8")
    nb_doc = json.loads(nb_text)
    for cell in nb_doc.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        outputs = cell.get("outputs", []) or []
        joined = " | ".join(
            _output_text(o) for o in outputs
        )
        snippet_lines = cell.get("source", "")
        if isinstance(snippet_lines, list):
            snippet_lines = "".join(snippet_lines)
        snippet = snippet_lines.splitlines()[0] if snippet_lines else ""
        exec_count = cell.get("execution_count")
        print(f"[exec={exec_count}] {snippet!r}  ->  {joined!r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
