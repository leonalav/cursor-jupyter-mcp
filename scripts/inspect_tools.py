"""Inspect all remaining MCP tools end-to-end.

Covers everything not yet tested by smoke_notebook.py:
  1.  file_read       — full and range reads
  2.  edit_locate     — anchor / symbol / heading modes
  3.  edit_apply      — replace / insert / delete
  4.  file_create     — new file + overwrite guard
  5.  validate_file   — against existing file + proposed content
  6.  type_check      — pyright on an existing .py file
  7.  config_detect   — workspace + per-file
  8.  config_doctor   — run the doctor
  9.  config_apply_fix — apply one doctor patch
  10. nb_cell_read    — by id and by number
  11. nb_cell_update  — replace source + range edit
  12. nb_cell_delete  — by id
  13. nb_cell_move    — reposition a cell
  14. kernel_control  — status / interrupt / restart
  15. kernel_discover — list kernelspecs

The notebook (test.ipynb) and the created scratch files are left in a
clean state afterwards, or deleted if they are scratch.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from cursor_edit_mcp.settings import Settings
from cursor_edit_mcp.tools.config import config_detect, config_doctor, config_apply_fix
from cursor_edit_mcp.tools.edit_apply import edit_apply, EditSpec, LineRange
from cursor_edit_mcp.tools.edit_locate import edit_locate
from cursor_edit_mcp.tools.execute import execute_cell, kernel_control
from cursor_edit_mcp.tools.file_create import file_create
from cursor_edit_mcp.tools.file_read import file_read
from cursor_edit_mcp.tools.kernel_discover import kernel_discover
from cursor_edit_mcp.tools.nb_cells import (
    nb_cell_delete,
    nb_cell_insert,
    nb_cell_move,
    nb_cell_read,
    nb_cell_update,
    nb_outline,
)
from cursor_edit_mcp.tools.type_check import type_check
from cursor_edit_mcp.tools.validate_file import validate_file

SEPARATOR = "=" * 70


def ok(result: dict, label: str) -> None:
    status = "✅" if result.get("ok", False) else "❌"
    print(f"{status}  {label}")
    if not result.get("ok"):
        print(f"     raw: {json.dumps(result, indent=2, default=str)[:400]}")


def section(title: str) -> None:
    print(f"\n{SEPARATOR}")
    print(f"  {title}")
    print(SEPARATOR)
    sys.stdout.flush()


# ── 1. file_read ────────────────────────────────────────────────────────────

section("1. file_read")

settings = Settings(workspace_root=Path("A:/custom_jupyter_mcp"))

# Full read
r = file_read(path="src/cursor_edit_mcp/__init__.py", settings=settings)
print(f"full read — ok={r['ok']}, lines={r['line_count']}, "
      f"hash={r['short_hash']}, eol={r['eol']}")

# Range read
r2 = file_read(path="src/cursor_edit_mcp/__init__.py", start_line=1, end_line=3, settings=settings)
print(f"range [1-3] — ok={r2['ok']}, partial={r2['partial']}, "
      f"start={r2['start_line']}, end={r2['end_line']}, "
      f"content={r2['content']!r}")
BASE_HASH = r["hash"]  # keep for edit_apply


# ── 2. edit_locate ──────────────────────────────────────────────────────────

section("2. edit_locate")

# anchor — exact text
loc1 = edit_locate(path="src/cursor_edit_mcp/__init__.py", text="if TYPE_CHECKING:", settings=settings)
print(f"anchor(text) — start={loc1['start_line']}, end={loc1['end_line']}, match={loc1['match']!r}")

# anchor — regex
loc2 = edit_locate(
    path="src/cursor_edit_mcp/__init__.py", regex=r"__version__\s*=", settings=settings
)
print(f"anchor(regex) — start={loc2['start_line']}, end={loc2['end_line']}, match={loc2['match']!r}")

# anchor — with include_text (use occurrence since '__version__' is ambiguous)
loc3 = edit_locate(
    path="src/cursor_edit_mcp/__init__.py", text="__version__", occurrence=0, include_text=True, settings=settings
)
print(f"anchor+text — start={loc3['start_line']}, end={loc3['end_line']}, "
      f"snippet={loc3.get('text', '')[:60]!r}")

# heading — find a Markdown section in a doc file
doc_file = "README.md"
if Path(doc_file).exists():
    # find the first heading (could be H1 or H2)
    # use a known section if present
    loc4 = edit_locate(path=doc_file, by="heading", text="Install", settings=settings)
    print(f"heading('Install') — start={loc4['start_line']}, end={loc4['end_line']}, "
          f"ok={loc4['ok']}")
else:
    print("heading — README.md not present, skipped")

# symbol — find a class
try:
    loc5 = edit_locate(
        path="src/cursor_edit_mcp/settings.py",
        by="symbol",
        name="Settings",
        settings=settings,
    )
    print(f"symbol(Settings) — start={loc5['start_line']}, end={loc5['end_line']}, "
          f"match={loc5['match']!r}")
except Exception as e:
    print(f"symbol(Settings) — skipped ({type(e).__name__}: {e})")


# ── 3. edit_apply ───────────────────────────────────────────────────────────

section("3. edit_apply")

scratch_py = "scripts/_inspect_scratch.py"
# Write a clean fixture
(Path(scratch_py).write_text(
    "value = 1\n# original line 2\nvalue = 2\n",
    encoding="utf-8",
))

r_full = file_read(path=scratch_py, settings=settings)

# 3a. replace
result_replace = edit_apply(
    path=scratch_py,
    edits=[EditSpec(
        op="replace",
        range=LineRange(start_line=1, end_line=1),
        new_text="value = 10",
    )],
    base_hash=r_full["hash"],
    validate="off",
    settings=settings,
)
ok(result_replace, "replace line 1")

# 3b. insert
r_after_replace = file_read(path=scratch_py, settings=settings)
result_insert = edit_apply(
    path=scratch_py,
    edits=[EditSpec(
        op="insert",
        after_line=2,
        new_text="# inserted comment",
    )],
    base_hash=r_after_replace["hash"],
    validate="off",
    settings=settings,
)
ok(result_insert, "insert after line 2")

# 3c. delete
r_after_insert = file_read(path=scratch_py, settings=settings)
result_delete = edit_apply(
    path=scratch_py,
    edits=[EditSpec(
        op="delete",
        range=LineRange(start_line=2, end_line=2),
    )],
    base_hash=r_after_insert["hash"],
    validate="off",
    settings=settings,
)
ok(result_delete, "delete line 2")

final_content = Path(scratch_py).read_text(encoding="utf-8")
print(f"  final content: {final_content!r}")


# ── 4. file_create ──────────────────────────────────────────────────────────

section("4. file_create")

new_file = "scripts/_inspect_new.txt"

# Make sure no stale file is left over from a prior run
Path(new_file).unlink(missing_ok=True)

# 4a. create (should succeed)
result_create = file_create(
    path=new_file,
    new_text="hello from file_create\n",
    validate="off",
    settings=settings,
)
ok(result_create, "create new file")
print(f"  created — bytes={result_create['bytes']}, line_count={result_create['line_count']}")

# 4b. create (should raise FILE_EXISTS)
try:
    file_create(path=new_file, new_text="duplicate\n", validate="off", settings=settings)
    print("❌  create without overwrite should have raised FILE_EXISTS")
except Exception as e:
    ok({"ok": "FILE_EXISTS" in str(e)}, "create raises FILE_EXISTS when file exists")

# 4c. create with overwrite
result_overwrite = file_create(
    path=new_file,
    new_text="overwritten content\n",
    overwrite=True,
    validate="off",
    settings=settings,
)
ok(result_overwrite, "create with overwrite=True")


# ── 5. validate_file ───────────────────────────────────────────────────────

section("5. validate_file")

# 5a. against existing file
v1 = validate_file(path="scripts/_inspect_scratch.py", settings=settings)
print(f"validate_file(existing) — ok={v1['ok']}, "
      f"new_count={v1['diagnostics']['new_count']}, "
      f"stages={[s['status'] for s in v1['stages']]}")

# 5b. against proposed content
v2 = validate_file(
    path="scripts/_inspect_scratch.py",
    content="value = 'valid string'\n",
    settings=settings,
)
print(f"validate_file(proposed) — ok={v2['ok']}, "
      f"validated={v2['validated']}, "
      f"new_count={v2['diagnostics']['new_count']}")


# ── 6. type_check ──────────────────────────────────────────────────────────

section("6. type_check")

# 6a. type-check existing file (scripts/_inspect_scratch.py has no type hints — should pass)
tc1 = type_check(
    path="scripts/_inspect_scratch.py",
    tool="pyright",
    settings=settings,
)
print(f"type_check(file, pyright) — ok={tc1['ok']}, tool={tc1['tool']}, "
      f"version={tc1.get('tool_version', '?')}, "
      f"skipped={tc1.get('skipped', False)}")

# 6b. type-check proposed content with a deliberate error
tc2 = type_check(
    path="scripts/_inspect_scratch.py",
    content="x: int = 'not an int'\n",
    tool="pyright",
    settings=settings,
)
print(f"type_check(proposed bad content) — ok={tc2['ok']}, "
      f"errors={tc2['diagnostics']['new_error_count']}")

# 6c. type-check proposed clean content
tc3 = type_check(
    path="scripts/_inspect_scratch.py",
    content="def add(a: int, b: int) -> int:\n    return a + b\n",
    tool="pyright",
    settings=settings,
)
print(f"type_check(proposed good content) — ok={tc3['ok']}, "
      f"diagnostics={tc3['diagnostics']['new_count']}")


# ── 7. config_detect ───────────────────────────────────────────────────────

section("7. config_detect")

# 7a. workspace-level
cd_ws = config_detect(settings=settings)
tools = cd_ws.get("tools", {})
print(f"detect(workspace) — scope={cd_ws.get('scope')}, "
      f"ruff={'configured' if tools.get('ruff', {}).get('configured') else 'no'}, "
      f"paths={[p['tool'] for p in cd_ws.get('tooling_paths', [])]}")

# 7b. per-file
cd_file = config_detect(path="src/cursor_edit_mcp/settings.py", settings=settings)
print(f"detect(file) — scope={cd_file.get('scope')}, "
      f"linter={cd_file.get('tooling', {}).get('linter', '?')}, "
      f"formatter={cd_file.get('tooling', {}).get('formatter', '?')}")


# ── 8. config_doctor ───────────────────────────────────────────────────────

section("8. config_doctor")

# 8a. workspace doctor
doc_ws = config_doctor(settings=settings)
findings = doc_ws.get("findings", [])
summary = doc_ws.get("summary", {})
print(f"doctor(workspace) — total={summary.get('total', 0)}, "
      f"errors={summary.get('errors', 0)}, warnings={summary.get('warnings', 0)}")

# 8b. file doctor
doc_file2 = config_doctor(path="src/cursor_edit_mcp/settings.py", settings=settings)
findings2 = doc_file2.get("findings", [])
summary2 = doc_file2.get("summary", {})
print(f"doctor(file) — total={summary2.get('total', 0)}, "
      f"errors={summary2.get('errors', 0)}")


# ── 9. config_apply_fix ────────────────────────────────────────────────────

section("9. config_apply_fix")

findings = doc_ws.get("findings", [])
if findings:
    f = findings[0]
    fid = f.get("id") or f.get("finding_id")
    token = f.get("confirmation_token") or f.get("token")
    if fid and token:
        af = config_apply_fix(
            finding_id=str(fid),
            confirmation_token=str(token),
            settings=settings,
        )
        ok(af, f"apply_fix({fid})")
    else:
        print(f"  doctor finding has no id/token: {list(f.keys())}")
else:
    print("  no workspace findings to apply")


# ── 10. nb_cell_read ───────────────────────────────────────────────────────

section("10. nb_cell_read")

# 10a. by cell_number
r_cell1 = nb_cell_read(path="test.ipynb", cell_number=1, settings=settings)
print(f"read(cell=1) — ok={r_cell1['ok']}, "
      f"cell_number={r_cell1['cells'][0]['cell_number']}, "
      f"type={r_cell1['cells'][0]['type']}, "
      f"source_snippet={r_cell1['cells'][0]['source'][:30]!r}")

# 10b. by cell_id (use the markdown cell's id from outline)
outline = nb_outline(path="test.ipynb", settings=settings)
md_id = outline["cells"][0]["cell_id"]
r_cell2 = nb_cell_read(path="test.ipynb", cell_id=md_id, include_outputs=True, settings=settings)
print(f"read(id={md_id}, outputs) — ok={r_cell2['ok']}, "
      f"type={r_cell2['cells'][0]['type']}, "
      f"outputs={r_cell2['cells'][0].get('outputs', [])}")


# ── 11. nb_cell_update ─────────────────────────────────────────────────────

section("11. nb_cell_update")

# 11a. update whole cell source
code_id = outline["cells"][1]["cell_id"]
code_cell_number = outline["cells"][1]["cell_number"]
out_update = nb_cell_update(
    path="test.ipynb",
    cell_id=code_id,
    source="# modified: x = 41\nx = 41\ny = 1\nx + y\n",
    validate="off",
    settings=settings,
)
ok(out_update, f"update cell {code_cell_number} (whole source)")
print(f"  updated — {out_update.get('updated', {}).get('cell_id', '?')}")

# 11b. range edit within a cell (replace line 1 of the markdown cell)
md_cell_number = outline["cells"][0]["cell_number"]
out_range = nb_cell_update(
    path="test.ipynb",
    cell_number=md_cell_number,
    range={"start_line": 1, "end_line": 1},
    source="# Inspection run",
    validate="off",
    settings=settings,
)
ok(out_range, f"update markdown cell {md_cell_number} (range edit line 1)")


# ── 12. nb_cell_delete ─────────────────────────────────────────────────────

section("12. nb_cell_delete")

# Insert a temporary cell to delete
ins = nb_cell_insert(
    path="test.ipynb",
    source="# TEMP CELL — delete me\nprint('temp')\n",
    cell_type="code",
    at_end=True,
    validate="off",
    settings=settings,
)
temp_id = ins["inserted"]["cell_id"]
outline_before_delete = nb_outline(path="test.ipynb", settings=settings)
count_before = outline_before_delete["cell_count"]

out_del = nb_cell_delete(path="test.ipynb", cell_id=temp_id, validate="off", settings=settings)
ok(out_del, f"delete temp cell {temp_id}")
outline_after_delete = nb_outline(path="test.ipynb", settings=settings)
print(f"  cell count: {count_before} -> {outline_after_delete['cell_count']}")


# ── 13. nb_cell_move ───────────────────────────────────────────────────────

section("13. nb_cell_move")

# Insert a cell we can move, then move it
ins2 = nb_cell_insert(
    path="test.ipynb",
    source="# MOVEME\nz = 99\n",
    cell_type="code",
    at_start=True,
    validate="off",
    settings=settings,
)
movable_id = ins2["inserted"]["cell_id"]
outline_premove = nb_outline(path="test.ipynb", settings=settings)
premove_n = next(c["cell_number"] for c in outline_premove["cells"] if c["cell_id"] == movable_id)
print(f"  inserted at start, cell_number={premove_n}")

out_move = nb_cell_move(
    path="test.ipynb",
    cell_id=movable_id,
    destination=3,  # 0-based → third position
    validate="off",
    settings=settings,
)
ok(out_move, f"move {movable_id} to position 3")
outline_postmove = nb_outline(path="test.ipynb", settings=settings)
postmove_n = next(c["cell_number"] for c in outline_postmove["cells"] if c["cell_id"] == movable_id)
print(f"  cell_number after move: {premove_n} -> {postmove_n}")


# ── 14. kernel_control ─────────────────────────────────────────────────────

section("14. kernel_control")

# 14a. status (kernel was started by smoke test)
kc_status = kernel_control(path="test.ipynb", action="status", settings=settings)
print(f"kernel status — alive={kc_status['kernel']['alive']}, "
      f"ready={kc_status['kernel']['ready']}, "
      f"restarts={kc_status['kernel']['restarts']}")

# 14b. interrupt
kc_interrupt = kernel_control(path="test.ipynb", action="interrupt", settings=settings)
print(f"kernel interrupt — ok={kc_interrupt['ok']}")

# 14c. restart
kc_restart = kernel_control(path="test.ipynb", action="restart", settings=settings)
print(f"kernel restart — ok={kc_restart['ok']}, "
      f"restarts={kc_restart['kernel']['restarts']}")

# 14d. shutdown (then we'll re-connect by running a cell)
kc_shutdown = kernel_control(path="test.ipynb", action="shutdown", settings=settings)
print(f"kernel shutdown — ok={kc_shutdown['ok']}, "
      f"alive={kc_shutdown['kernel']['alive']}")


# ── 15. kernel_discover ─────────────────────────────────────────────────────

section("15. kernel_discover")

kd = kernel_discover()
print(f"discover — ok={kd['ok']}, count={kd['count']}, "
      f"default={kd['default_kernel']}")
print(f"  languages: {kd['languages']}")
print(f"  reachable: {kd['reachable']}")
print(f"  unreachable: {kd['unreachable']}")
if kd["kernels"]:
    k0 = kd["kernels"][0]
    print(f"  first kernel — name={k0['name']}, "
          f"display_name={k0['display_name']}, "
          f"language={k0['language']}, "
          f"exec_status={k0['executable_status']}")


# ── Cleanup ─────────────────────────────────────────────────────────────────

section("Cleanup")

Path(scratch_py).unlink(missing_ok=True)
Path(new_file).unlink(missing_ok=True)
print("  deleted scripts/_inspect_scratch.py and scripts/_inspect_new.txt")


# ── Restore test.ipynb ──────────────────────────────────────────────────────

section("Restore test.ipynb")

# Remove the MOVEME cell and the modified cells
outline_final = nb_outline(path="test.ipynb", settings=settings)
for c in outline_final["cells"]:
    if c["type"] == "code" and "# MOVEME" in (c.get("first_line") or ""):
        nb_cell_delete(path="test.ipynb", cell_id=c["cell_id"], validate="off", settings=settings)
        print(f"  removed MOVEME cell {c['cell_id']}")
    if c["type"] == "code" and "# modified:" in (c.get("first_line") or ""):
        nb_cell_update(
            path="test.ipynb",
            cell_id=c["cell_id"],
            source="x = 41\ny = 1\nx + y\n",
            validate="off",
            settings=settings,
        )
        print(f"  restored modified cell {c['cell_id']}")
    if c["type"] == "markdown" and "# Inspection run" in (c.get("first_line") or ""):
        nb_cell_update(
            path="test.ipynb",
            cell_id=c["cell_id"],
            source="# Smoke test\n\nA markdown cell to anchor the notebook and confirm the outline / insert tools work end-to-end.\n",
            cell_type="markdown",
            validate="off",
            settings=settings,
        )
        print(f"  restored markdown cell {c['cell_id']}")

outline_restored = nb_outline(path="test.ipynb", settings=settings)
print(f"  restored — cell_count={outline_restored['cell_count']}, "
      f"cells={[c['first_line'] for c in outline_restored['cells']]}")


print(f"\n{SEPARATOR}")
print("  All tools inspected.")
print(SEPARATOR)
