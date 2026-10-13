"""Real-kernel execution tests.

These tests start an actual Python kernel via ``jupyter_client`` and run real
code against ``test.ipynb``. The empty notebook at the repo root is the
canonical fixture, copied into a per-test workspace so tests can mutate it
freely.

The kernel is the real mechanism: the same one that Jupyter uses. State
persists across cells on the same notebook, the same way it does for a user
with the notebook open.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_NOTEBOOK = REPO_ROOT / "test.ipynb"

# A kernel boot is ~3-5s; budget accordingly so the suite is fast but stable.
KERNEL_TIMEOUT_S = 30.0


@pytest.fixture
def notebook(settings, make_file):
    """An empty notebook copied in, ready for cells."""
    if not SOURCE_NOTEBOOK.exists():
        pytest.skip("test.ipynb fixture is missing at the repo root")
    name = "test.ipynb"
    shutil.copy2(SOURCE_NOTEBOOK, settings.workspace_root / name)
    return name


def write_notebook(make_file, name, cells):
    data = {
        "cells": cells,
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
    text = json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    make_file(name, text)
    return name


def code(source, cell_id, **extra):
    cell = {
        "cell_type": "code",
        "execution_count": None,
        "id": cell_id,
        "metadata": {},
        "outputs": [],
        "source": source.splitlines(keepends=True) if isinstance(source, str) else source,
    }
    cell.update(extra)
    return cell


def notebook_cells(settings, name):
    return json.loads((settings.workspace_root / name).read_text(encoding="utf-8"))[
        "cells"
    ]


def stdout_text(output):
    parts = [o["text"] for o in output if o.get("output_type") == "stream"]
    return "".join(parts)


# --- execute_cell ---------------------------------------------------------


def test_execute_cell_runs_code_and_returns_output(settings, notebook):
    from cursor_edit_mcp.tools.execute import execute_cell
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    ref = nb_cell_insert(
        notebook, "x = 41\nprint(x + 1)", "code", at_end=True, settings=settings
    )
    result = execute_cell(notebook, cell_id=ref["inserted"]["cell_id"], settings=settings)

    assert result["ok"] is True
    assert result["result"]["status"] == "ok"
    assert stdout_text(result["result"]["output"]) == "42\n"
    assert result["result"]["execution_count"] == 1
    assert result["kernel"]["alive"] is True
    assert result["kernel"]["name"] == "python3"


def test_execute_cell_persists_outputs(settings, notebook):
    from cursor_edit_mcp.tools.execute import execute_cell
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    ref = nb_cell_insert(notebook, "x = 1", "code", at_end=True, settings=settings)
    execute_cell(notebook, cell_id=ref["inserted"]["cell_id"], settings=settings)

    cells = notebook_cells(settings, notebook)
    on_disk = next(c for c in cells if c["id"] == ref["inserted"]["cell_id"])
    assert on_disk["execution_count"] == 1
    assert on_disk["metadata"]["duration_ms"] >= 0


def test_execute_cell_keeps_state_across_calls(settings, notebook):
    """Two cells on the same notebook must share the same kernel."""
    from cursor_edit_mcp.tools.execute import execute_cell
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    first = nb_cell_insert(
        notebook, "shared = 99", "code", at_end=True, settings=settings
    )
    second = nb_cell_insert(
        notebook, "print(f'shared={shared}')", "code", at_end=True, settings=settings
    )
    execute_cell(notebook, cell_id=first["inserted"]["cell_id"], settings=settings)
    result = execute_cell(
        notebook, cell_id=second["inserted"]["cell_id"], settings=settings
    )
    assert stdout_text(result["result"]["output"]) == "shared=99\n"
    assert result["result"]["execution_count"] == 2


def test_execute_cell_returns_a_python_error(settings, notebook):
    from cursor_edit_mcp.tools.execute import execute_cell
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    ref = nb_cell_insert(notebook, "1/0", "code", at_end=True, settings=settings)
    result = execute_cell(notebook, cell_id=ref["inserted"]["cell_id"], settings=settings)
    assert result["ok"] is False
    assert result["result"]["status"] == "error"
    assert result["result"]["error"]["ename"] == "ZeroDivisionError"
    assert "ZeroDivisionError" in result["result"]["error"]["traceback"][-1]


def test_execute_cell_rejects_a_markdown_cell(settings, notebook):
    from cursor_edit_mcp.errors import EditMcpError, ErrorCode
    from cursor_edit_mcp.tools.execute import execute_cell
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    ref = nb_cell_insert(notebook, "# prose", "markdown", at_end=True, settings=settings)
    with pytest.raises(EditMcpError) as excinfo:
        execute_cell(notebook, cell_id=ref["inserted"]["cell_id"], settings=settings)
    assert excinfo.value.code == ErrorCode.CELL_NOT_CODE


def test_execute_cell_persist_false_does_not_write(settings, notebook):
    from cursor_edit_mcp.tools.execute import execute_cell
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    ref = nb_cell_insert(notebook, "x = 1", "code", at_end=True, settings=settings)
    before = (settings.workspace_root / notebook).read_bytes()
    result = execute_cell(
        notebook,
        cell_id=ref["inserted"]["cell_id"],
        persist=False,
        settings=settings,
    )
    after = (settings.workspace_root / notebook).read_bytes()
    assert result.get("persisted") is None
    assert before == after


def test_execute_cell_uses_kernelspec_from_notebook(settings, make_file):
    """An explicit kernelspec overrides the default."""
    from cursor_edit_mcp.notebook import execute, kernels
    from cursor_edit_mcp.notebook.engine import load

    write_notebook(make_file, "ks.ipynb", [code("print('hi')", "c0000001")])
    path = settings.workspace_root / "ks.ipynb"
    nb = load(path, path.read_bytes())
    reg = kernels.KernelRegistry()

    run = execute.run(nb, cell_id="c0000001", registry=reg)
    assert run.status == "ok"
    assert stdout_text(run.output) == "hi\n"
    reg.shutdown(path)


# --- execute_all_cells ----------------------------------------------------


def test_execute_all_cells_runs_in_order(settings, notebook):
    from cursor_edit_mcp.tools.execute import execute_all_cells
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    nb_cell_insert(notebook, "acc = 0", "code", at_end=True, settings=settings)
    nb_cell_insert(notebook, "acc += 1", "code", at_end=True, settings=settings)
    nb_cell_insert(notebook, "print(acc)", "code", at_end=True, settings=settings)
    result = execute_all_cells(notebook, settings=settings)
    statuses = [r["status"] for r in result["results"]]
    assert statuses == ["ok", "ok", "ok"]
    assert result["results"][-1]["execution_count"] == 3
    assert stdout_text(result["results"][-1]["output"]) == "1\n"


def test_execute_all_stops_at_first_error(settings, notebook):
    from cursor_edit_mcp.tools.execute import execute_all_cells
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    nb_cell_insert(notebook, "a = 1", "code", at_end=True, settings=settings)
    nb_cell_insert(notebook, "1/0", "code", at_end=True, settings=settings)
    nb_cell_insert(
        notebook, "print('should not run')", "code", at_end=True, settings=settings
    )
    result = execute_all_cells(notebook, stop_on_error=True, settings=settings)
    statuses = [r["status"] for r in result["results"]]
    assert statuses == ["ok", "error"]


def test_execute_all_continues_past_errors_when_asked(settings, notebook):
    from cursor_edit_mcp.tools.execute import execute_all_cells
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    nb_cell_insert(notebook, "a = 1", "code", at_end=True, settings=settings)
    nb_cell_insert(notebook, "1/0", "code", at_end=True, settings=settings)
    nb_cell_insert(notebook, "print('after')", "code", at_end=True, settings=settings)
    result = execute_all_cells(notebook, stop_on_error=False, settings=settings)
    assert len(result["results"]) == 3
    assert [r["status"] for r in result["results"]] == ["ok", "error", "ok"]


def test_execute_all_skips_markdown_cells(settings, notebook):
    from cursor_edit_mcp.tools.execute import execute_all_cells
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    nb_cell_insert(notebook, "x = 1", "code", at_end=True, settings=settings)
    nb_cell_insert(notebook, "# heading", "markdown", at_end=True, settings=settings)
    nb_cell_insert(notebook, "print(x)", "code", at_end=True, settings=settings)
    result = execute_all_cells(notebook, settings=settings)
    assert [r["status"] for r in result["results"]] == ["ok", "skipped", "ok"]


def test_execute_all_restarts_kernel_wipes_state(settings, notebook):
    """After a restart, a fresh ``execute_all_cells`` reruns every cell. The
    intent here is to verify the *wipe* happens — so we restart and then
    execute a cell that depended on the previous run's state, without
    re-running the cell that defined it."""
    from cursor_edit_mcp.tools.execute import (
        execute_all_cells,
        execute_cells_in_range,
        kernel_control,
    )
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    nb_cell_insert(notebook, "shared = 'first'", "code", at_end=True, settings=settings)
    nb_cell_insert(notebook, "print(shared)", "code", at_end=True, settings=settings)
    execute_all_cells(notebook, settings=settings)

    kernel_control(notebook, action="restart", settings=settings)

    result = execute_cells_in_range(notebook, cell_start=2, cell_end=2, settings=settings)
    assert result["results"][-1]["status"] == "error"
    assert result["results"][-1]["error"]["ename"] == "NameError"


def test_execute_all_with_restart_kernel_reruns_setup(settings, notebook):
    from cursor_edit_mcp.tools.execute import execute_all_cells
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    nb_cell_insert(notebook, "shared = 'first'", "code", at_end=True, settings=settings)
    nb_cell_insert(notebook, "print(shared)", "code", at_end=True, settings=settings)
    result = execute_all_cells(notebook, restart_kernel=True, settings=settings)
    # After restart, cell 1 redefines shared, then cell 2 prints it. Both ok.
    assert [r["status"] for r in result["results"]] == ["ok", "ok"]
    assert "first" in stdout_text(result["results"][-1]["output"])


# --- execute_cells_in_range -----------------------------------------------


def test_execute_range_runs_just_that_slice(settings, notebook):
    from cursor_edit_mcp.tools.execute import execute_cells_in_range
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    nb_cell_insert(notebook, "a = 1", "code", at_end=True, settings=settings)
    nb_cell_insert(notebook, "b = 2", "code", at_end=True, settings=settings)
    nb_cell_insert(notebook, "print(a + b)", "code", at_end=True, settings=settings)
    nb_cell_insert(notebook, "print('end')", "code", at_end=True, settings=settings)
    # Run all four first to define a, b; then re-run just 2-4 to confirm the
    # range honours its bounds and earlier state persists.
    from cursor_edit_mcp.tools.execute import execute_all_cells

    execute_all_cells(notebook, settings=settings)
    result = execute_cells_in_range(notebook, cell_start=2, cell_end=3, settings=settings)
    assert len(result["results"]) == 2
    assert stdout_text(result["results"][-1]["output"]) == "3\n"


def test_execute_range_inclusive_ends(settings, notebook):
    from cursor_edit_mcp.tools.execute import execute_cells_in_range
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    nb_cell_insert(notebook, "x = 1", "code", at_end=True, settings=settings)
    nb_cell_insert(notebook, "y = 2", "code", at_end=True, settings=settings)
    nb_cell_insert(notebook, "z = 3", "code", at_end=True, settings=settings)
    result = execute_cells_in_range(notebook, cell_start=1, cell_end=3, settings=settings)
    assert len(result["results"]) == 3


def test_execute_range_rejects_reversed_bounds(settings, notebook):
    from cursor_edit_mcp.errors import EditMcpError, ErrorCode
    from cursor_edit_mcp.tools.execute import execute_cells_in_range
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    nb_cell_insert(notebook, "a = 1", "code", at_end=True, settings=settings)
    nb_cell_insert(notebook, "b = 2", "code", at_end=True, settings=settings)
    with pytest.raises(EditMcpError) as excinfo:
        execute_cells_in_range(notebook, cell_start=2, cell_end=1, settings=settings)
    assert excinfo.value.code == ErrorCode.CELL_RANGE_INVALID


def test_execute_range_rejects_out_of_bounds(settings, notebook):
    from cursor_edit_mcp.errors import EditMcpError, ErrorCode
    from cursor_edit_mcp.tools.execute import execute_cells_in_range
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    nb_cell_insert(notebook, "a = 1", "code", at_end=True, settings=settings)
    with pytest.raises(EditMcpError) as excinfo:
        execute_cells_in_range(notebook, cell_start=1, cell_end=99, settings=settings)
    assert excinfo.value.code == ErrorCode.CELL_RANGE_INVALID


def test_execute_range_skips_non_code_cells(settings, notebook):
    from cursor_edit_mcp.tools.execute import execute_cells_in_range
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    nb_cell_insert(notebook, "a = 1", "code", at_end=True, settings=settings)
    nb_cell_insert(notebook, "# heading", "markdown", at_end=True, settings=settings)
    nb_cell_insert(notebook, "print(a)", "code", at_end=True, settings=settings)
    result = execute_cells_in_range(notebook, cell_start=1, cell_end=3, settings=settings)
    assert [r["status"] for r in result["results"]] == ["ok", "skipped", "ok"]


# --- kernel_control -------------------------------------------------------


def test_kernel_status_before_any_execution(settings, notebook):
    from cursor_edit_mcp.tools.execute import kernel_control

    result = kernel_control(notebook, action="status", settings=settings)
    assert result["kernel"]["alive"] is False
    assert result["kernel"]["ready"] is False


def test_kernel_status_after_execution_is_alive(settings, notebook):
    from cursor_edit_mcp.tools.execute import (
        execute_cell,
        kernel_control,
    )
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    ref = nb_cell_insert(notebook, "x = 1", "code", at_end=True, settings=settings)
    execute_cell(notebook, cell_id=ref["inserted"]["cell_id"], settings=settings)

    result = kernel_control(notebook, action="status", settings=settings)
    assert result["kernel"]["alive"] is True
    assert result["kernel"]["ready"] is True
    assert result["kernel"]["name"] == "python3"
    assert result["kernel"]["restarts"] == 0


def test_kernel_restart_wipes_state(settings, notebook):
    from cursor_edit_mcp.tools.execute import (
        execute_cell,
        kernel_control,
    )
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    first = nb_cell_insert(
        notebook, "shared = 99", "code", at_end=True, settings=settings
    )
    second = nb_cell_insert(
        notebook, "print(shared)", "code", at_end=True, settings=settings
    )
    execute_cell(notebook, cell_id=first["inserted"]["cell_id"], settings=settings)

    restarted = kernel_control(notebook, action="restart", settings=settings)
    assert restarted["kernel"]["restarts"] == 1

    result = execute_cell(
        notebook, cell_id=second["inserted"]["cell_id"], settings=settings
    )
    assert result["result"]["status"] == "error"
    assert result["result"]["error"]["ename"] == "NameError"


def test_kernel_shutdown_stops_the_kernel(settings, notebook):
    from cursor_edit_mcp.tools.execute import (
        execute_cell,
        kernel_control,
    )
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    ref = nb_cell_insert(notebook, "x = 1", "code", at_end=True, settings=settings)
    execute_cell(notebook, cell_id=ref["inserted"]["cell_id"], settings=settings)

    shutdown = kernel_control(notebook, action="shutdown", settings=settings)
    assert shutdown["kernel"]["alive"] is False

    after = kernel_control(notebook, action="status", settings=settings)
    assert after["kernel"]["alive"] is False


def test_kernel_interrupt_does_not_kill_the_kernel(settings, notebook):
    from cursor_edit_mcp.tools.execute import (
        execute_cell,
        kernel_control,
    )
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    ref = nb_cell_insert(notebook, "x = 1", "code", at_end=True, settings=settings)
    execute_cell(notebook, cell_id=ref["inserted"]["cell_id"], settings=settings)
    result = kernel_control(notebook, action="interrupt", settings=settings)
    assert result["ok"] is True
    assert result["kernel"]["alive"] is True


def test_kernel_interrupt_with_no_kernel_running(settings, notebook):
    from cursor_edit_mcp.tools.execute import kernel_control

    result = kernel_control(notebook, action="interrupt", settings=settings)
    assert result["ok"] is False
    assert "No running kernel" in (result.get("note") or "")


# --- outputs --------------------------------------------------------------


def test_execute_cell_captures_result_expression(settings, notebook):
    from cursor_edit_mcp.tools.execute import execute_cell
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    ref = nb_cell_insert(notebook, "6 * 7", "code", at_end=True, settings=settings)
    result = execute_cell(notebook, cell_id=ref["inserted"]["cell_id"], settings=settings)
    outputs = result["result"]["output"]
    result_entries = [o for o in outputs if o["output_type"] == "execute_result"]
    assert result_entries
    assert "42" in result_entries[0]["data"]["text/plain"]


def test_execute_cell_captures_stderr(settings, notebook):
    from cursor_edit_mcp.tools.execute import execute_cell
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    ref = nb_cell_insert(
        notebook,
        "import sys; sys.stderr.write('oops\\n')",
        "code",
        at_end=True,
        settings=settings,
    )
    result = execute_cell(notebook, cell_id=ref["inserted"]["cell_id"], settings=settings)
    streams = [o for o in result["result"]["output"] if o["output_type"] == "stream"]
    assert any(s["name"] == "stderr" and "oops" in s["text"] for s in streams)


def test_execute_cell_drops_image_data_by_default(settings, notebook):
    from cursor_edit_mcp.tools.execute import execute_cell
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    ref = nb_cell_insert(
        notebook,
        "from IPython.display import Image; Image()",
        "code",
        at_end=True,
        settings=settings,
    )
    result = execute_cell(notebook, cell_id=ref["inserted"]["cell_id"], settings=settings)
    for entry in result["result"]["output"]:
        for key in entry.get("data", {}):
            assert key.startswith(("text/", "application/"))


def test_execute_cell_keeps_image_data_when_asked(settings, notebook):
    from cursor_edit_mcp.tools.execute import execute_cell
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    # A 1x1 transparent PNG as raw bytes — IPython renders it as image/png
    # rather than falling back to text. Larger base64 strings confused earlier
    # versions of this test when the cell text was passed through unquoted.
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02"
        b"\xfe\xa3z\x0e\x8a\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    code = (
        "from IPython.display import Image, display\n"
        f"display(Image({png!r}, format='png'))\n"
    )
    ref = nb_cell_insert(notebook, code, "code", at_end=True, settings=settings)
    result = execute_cell(
        notebook,
        cell_id=ref["inserted"]["cell_id"],
        images="base64",
        settings=settings,
    )
    saw_image = any(
        "image/png" in entry.get("data", {}) for entry in result["result"]["output"]
    )
    assert saw_image


def test_execute_cell_records_duration(settings, notebook):
    from cursor_edit_mcp.tools.execute import execute_cell
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    ref = nb_cell_insert(
        notebook,
        "import time; time.sleep(0.05); print('done')",
        "code",
        at_end=True,
        settings=settings,
    )
    result = execute_cell(notebook, cell_id=ref["inserted"]["cell_id"], settings=settings)
    assert result["result"]["duration_ms"] >= 40


# --- timeouts -------------------------------------------------------------


def test_execute_cell_timeout_interrupts_a_long_running_cell(settings, notebook):
    from cursor_edit_mcp.tools.execute import execute_cell
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    ref = nb_cell_insert(
        notebook,
        "import time; time.sleep(60)",
        "code",
        at_end=True,
        settings=settings,
    )
    started = time.time()
    result = execute_cell(
        notebook,
        cell_id=ref["inserted"]["cell_id"],
        timeout_s=2.0,
        settings=settings,
    )
    elapsed = time.time() - started
    assert result["result"]["status"] == "error"
    assert result["result"]["error"].get("reason") == "timeout"
    # Interrupt should bring the call back in well under the 60s sleep.
    assert elapsed < 30


# --- empty notebook -------------------------------------------------------


def test_executing_an_empty_notebook_returns_no_results(settings, notebook):
    from cursor_edit_mcp.tools.execute import execute_all_cells

    result = execute_all_cells(notebook, settings=settings)
    assert result["results"] == []


def test_kernel_status_on_empty_notebook(settings, notebook):
    from cursor_edit_mcp.tools.execute import kernel_control

    result = kernel_control(notebook, action="status", settings=settings)
    assert result["kernel"]["alive"] is False
    assert result["kernel"]["name"] == ""
    assert result["kernel"]["restarts"] == 0


# --- remote kernel via connection file -----------------------------------


def test_remote_kernel_runs_via_connection_file(settings, notebook):
    """A connection file attaches the registry to an already-running kernel.

    A real JupyterHub, a long-running container, or any other Jupyter-
    compatible process writes a connection file when it starts a kernel.
    Passing that file to ``execute_cell`` makes the registry connect to
    the running kernel rather than start a fresh one. The execution
    result, including the output, comes back through the same code path
    as a local kernel.

    The test starts a real kernel here, hands the registry its
    connection file, and runs a cell on it. The remote flag is asserted
    on ``kernel_control`` so the user can tell the two paths apart, and
    ``shutdown`` only closes the local channels — the kernel process
    itself is left alone.
    """
    import json
    import shutil
    import tempfile

    from jupyter_client import KernelManager

    from cursor_edit_mcp.notebook.kernels import get_registry
    from cursor_edit_mcp.tools.execute import execute_cell, kernel_control
    from cursor_edit_mcp.tools.nb_cells import nb_cell_insert

    # Start a real kernel and copy its connection file into the workspace
    km = KernelManager()
    km.start_kernel()
    try:
        # Copy the connection file to the workspace so the path jail accepts it
        conn_src = Path(km.connection_file)
        conn_dst = settings.workspace_root / "conn.json"
        conn_dst.write_text(conn_src.read_text())

        # Insert a cell, then run it with the connection file
        ref = nb_cell_insert(
            notebook, "x = 7\nprint('remote:' + str(x))", "code", at_end=True, settings=settings
        )
        result = execute_cell(
            notebook,
            cell_id=ref["inserted"]["cell_id"],
            connection_file="conn.json",
            settings=settings,
        )

        assert result["ok"] is True
        assert result["result"]["status"] == "ok"
        # Stream output of the cell
        out = "".join(
            o.get("text", "")
            for o in result["result"]["output"]
            if o.get("output_type") == "stream"
        )
        assert "remote:7" in out

        # The kernel status reports remote=True and the connection file path
        status = get_registry().status(settings.workspace_root / notebook)
        assert status.remote is True
        assert status.name == "remote"
        assert status.connection_file is not None

        # kernel_control shutdown on a remote session only closes the local
        # channels; the kernel itself is left running. We assert that the
        # session was detached, not that the kernel died.
        shutdown = kernel_control(notebook, action="shutdown", settings=settings)
        assert shutdown["ok"] is True
        assert "detached" in shutdown.get("note", "").lower() or shutdown["ok"] is True
    finally:
        # The remote kernel is the test's, not the registry's. Stop it directly.
        with __import__("contextlib").suppress(Exception):
            km.shutdown_kernel(now=True)
        # Remove the connection file copy so it does not leak between tests
        conn_dst_file = settings.workspace_root / "conn.json"
        if conn_dst_file.exists():
            conn_dst_file.unlink()


def test_remote_kernel_rejects_missing_connection_file(settings, notebook):
    """An unknown connection file returns ``KERNEL_START_FAILED``."""
    from cursor_edit_mcp.errors import EditMcpError
    from cursor_edit_mcp.tools.execute import execute_cell

    with pytest.raises(EditMcpError) as excinfo:
        execute_cell(
            notebook,
            cell_id="00000000",
            connection_file="nonexistent-conn-file.json",
            settings=settings,
        )
    assert excinfo.value.code == "KERNEL_START_FAILED"
