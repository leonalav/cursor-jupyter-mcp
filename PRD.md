# Cursor Precision Edit MCP: Scope, PRD & Design Spec

Working name: `cursor-edit-mcp` · Draft v0.2 · October 12, 2026 · Status: for review

This document contains three parts: (1) Design Scope, (2) Product Requirements Document, (3) Design Specification. Where I had to interpret the request, I state the assumption explicitly so you can correct it.

---

## Part 1: Design Scope

### 1.1 Problem statement

AI agents in Cursor change code through whole-file rewrites or fuzzy search-and-replace diffs. Five recurring failure modes follow from that:

1. **Misplaced or stale edits.** The edit lands on the wrong lines, or on content that has changed since the agent last read the file.
2. **Lint and format regressions.** Edits introduce indentation errors, unused imports, import-order violations, trailing whitespace, or type errors. The agent then burns turns fixing problems it created.
3. **Notebook corruption.** `.ipynb` files are JSON. Edited as raw text, they end up with invalid JSON, lost outputs or metadata, duplicate cell IDs, and malformed `source` arrays. Percent-format scripts (`# %%`) are also easy to break.
4. **Configuration blindness.** The agent cannot see the effective lint, format, and type-check configuration for a file. It guesses, edits config files incorrectly, or fights the tooling (for example, Ruff versus Black line length, or markdownlint versus Prettier).
5. **No execution feedback.** The agent edits a notebook cell but cannot run it and see the output, so it cannot confirm that its change works.

### 1.2 Goals

| ID | Goal |
| --- | --- |
| G1 | Make precise, verifiable **ranged edits** (line ranges, anchors, Python symbols, Markdown headings) with optimistic-concurrency protection against stale reads. |
| G2 | **Validate every edit before it is committed** (syntax, format, lint) using the project's own tool configuration, and report only the diagnostics the edit introduced. |
| G3 | Provide **first-class notebook cell operations** (create, read, update, delete, move) that cannot produce an invalid notebook. |
| G4 | Give the agent **read-only configuration diagnostics** so it understands, and stops fighting, the effective tooling setup. |
| G5 | Keep tool responses small and structured so that token cost stays low and the agent can self-correct without extra reads. |
| G6 | **Execute notebook cells** (one, a range, or all) on a persistent kernel, return each cell's output to the agent, and save the outputs into the notebook. |
| G7 | Make **Markdown and Python** the fully supported file types: edits to them never leave a file that fails to parse. |

### 1.3 Non-goals

- Replacing Cursor's native editor, Tab completion, or its agent. This server complements them.
- A general-purpose code-intelligence server (go-to-definition, refactoring, search). Cursor and language servers already do this.
- A notebook UI or a kernel hosting service. Execution is limited to local kernels owned by this server, one per notebook; the notebook editor stays Cursor's.
- Sandboxing executed code. Cells run with the user's own permissions, like any terminal command.
- Auto-fixing project configuration. v1 diagnoses and proposes patches; it does not apply them.
- Remote or multi-user hosting. v1 is a local, single-user process.

### 1.4 In scope / out of scope

**In scope (v1):** local stdio MCP server; range edits for text files, with **Markdown and Python as the fully supported profiles**; atomic multi-edit batches; dry-run and preview; syntax, format, and lint validation with baseline diffing (Ruff for Python, structural checks plus markdownlint/Prettier for Markdown); `.ipynb` (nbformat 4.x) and percent-format cell operations; **kernel-backed cell execution (`execute_cell`, `execute_all_cells`, `execute_cells_in_range`) with outputs returned to the agent**; config detection and "doctor" checks for Python and Markdown tooling; a Cursor rule file that steers the agent toward these tools.

**Out of scope (v1):** ESLint, Prettier and `tsc` runners for TypeScript/JavaScript (P2; other languages get syntax-only checks), remote kernels, sandboxing of executed code, Git operations, remote/HTTP transport, a GUI, and automatic config rewriting.

### 1.5 Assumptions (please confirm)

- **"Create cells" means notebook cells** (Jupyter `.ipynb`) and also `# %%` percent-format scripts, which Cursor and VS Code treat as cells. *(Confirmed.)*
- **"Lint problems" means lint, format, and type regressions caused by agent edits**, not general repo-wide lint cleanup. *(Confirmed.)*
- **"Configuration problems" means the agent misreading or mis-editing tool config**, such as Ruff, markdownlint, Prettier, tsconfig, and EditorConfig. *(Confirmed.)*
- The primary targets are **Markdown and Python**. TypeScript/JavaScript tooling is a later add-on.
- `cell_number` in the execution tools is **1-based and counts every cell**, markdown cells included, in file order, matching what the user sees in the notebook.
- Executed code runs on the user's machine with the user's permissions, in the project's own Python environment, which must have `ipykernel` installed.
- The server reads and writes files on disk. Cursor may hold unsaved editor buffers, which is a known risk (see 1.8).

### 1.6 Constraints

- Must run as a local process over stdio and be configurable through Cursor's `mcp.json`.
- Must use the project's installed tools and config rather than bundling its own opinions. If Ruff or markdownlint is not installed, validation degrades gracefully with a clear message.
- Cell execution must use the project's own Python environment, not the server's.
- Cursor limits how many tools it will expose across all MCP servers, so the catalog stays small: 20 tools in v1, with a documented consolidation to about 16 if needed.
- Must never write outside the workspace root.

### 1.7 Success criteria

- Lint/format/type regressions introduced per agent edit drop by at least 80% versus the baseline measured in Cursor without the server.
- Zero invalid or corrupted notebooks across the test corpus and dogfooding.
- Zero parse errors and zero broken Markdown structures (fences, tables, front matter) left behind by range edits across the test suite.
- At least 95% of range edits succeed on the first attempt, with failures returning an actionable error.
- p95 latency under 300 ms for edits without lint, and under 2 s with lint on a typical file.
- Execution tools return outputs that match what Jupyter produces for the same cells on the fixture notebooks. A failing cell always comes back with its traceback, never as silence.
- Execution overhead, excluding the cell's own runtime, stays under 200 ms once the kernel is warm.

### 1.8 Key risks

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Cursor has unsaved edits in a buffer while the server writes to disk | Lost or conflicting changes | Content-hash precondition on every write; document "save before agent edit"; investigate file-watcher reconciliation in M6. |
| The agent keeps using Cursor's native edit tool and ignores ours | No benefit | Ship a `.cursor/rules` file; keep tool descriptions crisp; measure adoption. |
| Linters are slow or flaky | Poor experience | Stdin-based single-file runs, timeouts, graceful degrade to `warn`. |
| Project config is exotic or broken | Validation unreliable | Resolve effective config through the tools' own CLIs; report `TOOL_UNAVAILABLE` rather than guessing. |
| Tool-count cap | Tools hidden | Keep the catalog at about 20 tools; documented consolidation to about 16. |
| The project environment has no `ipykernel` | Execution fails | Detect the interpreter, return `KERNEL_UNAVAILABLE` with the exact install command. |
| A long-running cell exceeds the client's tool-call timeout | Agent sees a failure while the cell still runs | Background execution (`background: true`) with polling; per-call timeouts and a progress notification per finished cell for foreground runs; interrupt on timeout. |
| Executed code has side effects (files, network, state) | Unwanted changes | Same trust level as the agent's terminal; tool annotations make Cursor ask for approval; execution can be disabled in config. |
| Stale kernel state makes outputs misleading | Wrong conclusions | Return `execution_count` and a restart counter; offer `restart_kernel`; clear outputs of edited cells. |

### 1.9 Decisions

1. **Validation default:** `warn`; `strict` is opt-in per call.
2. **Type-checking:** `pyright` and `mypy` are P2, not in v1, because of latency.
3. **Run all:** `execute_all_cells` stops at the first error; `stop_on_error: false` continues.
4. **Long-running cells:** supported in v1 through background execution (`background: true`, polled with `kernel_control`); see 3.6.1.
5. **Notebook outputs:** saved into the `.ipynb` by default (`persist: true`), capped at 2 MB per output.

---

## Part 2: Product Requirements Document (PRD)

### 2.1 Overview

**Product:** a local MCP server that gives Cursor's agent safe, precise editing primitives for text files and notebooks, plus kernel-backed cell execution, pre-commit validation and configuration diagnostics.

**Vision:** an agent edit should be as safe as a human edit made in an IDE with format-on-save and live linting, and it should tell the agent immediately whether it made things worse.

### 2.2 Users and personas

- **Primary: a developer using Cursor's agent for multi-file work.** They want fewer "fix the lint errors you just introduced" loops and no broken notebooks.
- **Secondary: a data scientist working in notebooks.** They need cell-level edits that preserve outputs, metadata, and clean diffs.
- **Tertiary: a team lead.** They want a shareable `mcp.json` and rule file so the whole team gets consistent agent behavior.

### 2.3 User stories

- As a developer, I want the agent to replace lines 42–47 of a file and fail loudly if those lines changed, so that edits never land on the wrong code.
- As a developer, I want each edit checked against my Ruff or markdownlint config before it is saved, so that the agent doesn't leave lint errors behind.
- As a developer, I want only the *new* diagnostics from an edit reported, so that existing debt doesn't drown the signal.
- As a developer, I want to replace a Markdown section by its heading, so that the agent never counts lines in a long README.
- As a developer, I want inserted Python code re-indented to its surrounding block, so that the agent never produces an `IndentationError`.
- As a data scientist, I want the agent to insert a markdown cell after cell 3 without touching outputs or metadata, so that my notebook diffs stay small.
- As a data scientist, I want a code cell I edit to be linted as Python even if it contains `%matplotlib inline`.
- As a data scientist, I want the agent to run cell 4 after editing it and see the output or traceback, so that it can confirm the fix without asking me.
- As a data scientist, I want cells 3 to 5 to run in order on a persistent kernel, so that variables from earlier cells are available.
- As a data scientist, I want to run all cells and get each cell's output keyed by its cell number, so that the agent can review the whole notebook in one call.
- As a developer, I want the agent to learn which formatter, line length, and indent style apply to a file before it writes anything.
- As a team lead, I want to commit one config so the whole team's Cursor setup uses the server identically.

### 2.4 Functional requirements

Priorities: **P0** = required for v1, **P1** = v1 if capacity allows, **P2** = fast follow.

| ID | Requirement | Priority |
| --- | --- | --- |
| FR-1 | Read a file or a line range and return content plus a SHA-256 content hash and line count. | P0 |
| FR-2 | Apply a batch of range edits (replace, insert, delete) to one file atomically. All line numbers in a batch refer to the original snapshot; overlapping edits are rejected. | P0 |
| FR-3 | Require or accept `base_hash`; reject the write with `STALE_HASH` if the file changed. | P0 |
| FR-4 | Optional `expect` guard per edit: text that must appear in the target range, otherwise `EXPECTED_TEXT_MISMATCH`. | P0 |
| FR-5 | Locate a range by anchor (text or regex, with occurrence index), by Python symbol (`Class.method`), or by Markdown heading (the whole section). | P0 |
| FR-6 | `dry_run` returns the unified diff and diagnostics without writing. | P0 |
| FR-7 | Preserve line endings, final newline, BOM, file mode, and the Python encoding cookie. Re-indent inserted text relative to the target block (`indent_mode: relative`, the default). | P0 |
| FR-8 | Validation pipeline: syntax parse, then range-contained format, then lint with baseline diff. Modes `off`, `warn`, `strict`. In `strict`, an edit that introduces new errors is not written. | P0 |
| FR-9 | Resolve lint/format configuration per file through the tools' own resolution (Ruff, markdownlint, Prettier, EditorConfig). | P0 |
| FR-10 | Create a new file with the same validation and path rules. | P0 |
| FR-11 | Notebook outline: list cells with 1-based `cell_number`, stable ID, type, first line, and output summary. | P0 |
| FR-12 | Notebook cell read, insert, update, delete, move, addressed by cell ID (preferred) or index. | P0 |
| FR-13 | Guarantee valid nbformat output: unique cell IDs, correct `source` array form, schema validation before every write, Jupyter-compatible serialization for minimal diffs. | P0 |
| FR-14 | Lint notebook code cells as a virtual document, neutralizing magics (`%`, `%%`, `!`), and map diagnostics back to `cell_id` and cell-relative line. | P0 |
| FR-15 | Same cell operations for percent-format scripts (`# %%`, `# %% [markdown]`). | P1 |
| FR-16 | `config_detect`: report the effective tooling and versions for a file or the workspace. | P0 | done |
| FR-17 | `config_doctor`: detect conflicts and misconfigurations (see 3.7) and return findings with evidence and a suggested patch. It does not apply them. | P1 | done |
| FR-18 | Optional type-check stage (`pyright`, `mypy`) as an async or opt-in validation step. | P2 | done |
| FR-19 | `config_apply_fix` to apply a doctor-proposed patch after explicit confirmation. | P2 | done |
| FR-20 | Ship a `.cursor/rules` file and an `mcp.json` snippet. | P0 |
| FR-21 | `execute_cell`: run one code cell on the notebook's kernel and return its outputs. The cell is selected by `cell_number` (1-based) or `cell_id`. | P0 |
| FR-22 | `execute_all_cells`: run every code cell in order and return, per cell, `cell_number` and `output`. Stops at the first error by default (`stop_on_error`); remaining cells are reported `not_run`. | P0 |
| FR-23 | `execute_cells_in_range`: run cells `cell_start` to `cell_end` (1-based, inclusive) in order, with the same result shape. | P0 |
| FR-24 | Kernel lifecycle: one lazily started kernel per notebook with state kept across calls; `kernel_control` supports status, restart, interrupt, and shutdown; idle culling; cleanup on exit. | P0 |
| FR-25 | Run the kernel in the project's own Python environment. If `ipykernel` is missing, return `KERNEL_UNAVAILABLE` with an install hint. | P0 |
| FR-26 | Output normalization: merge stream chunks, honour carriage returns and `clear_output`, strip ANSI codes, prefer `text/plain`, truncate per cell with a flag, and save images to disk (inline on request). | P0 |
| FR-27 | Timeouts: per-cell and per-call limits. On timeout, interrupt the kernel and return partial output with `status: "timeout"`. | P0 |
| FR-28 | `persist` (default true): after execution, write each executed cell's outputs and `execution_count` back into the `.ipynb` atomically, only for cells whose source is unchanged since they ran. `persist: false` keeps the run agent-only. | P0 |
| FR-29 | Execute cells of percent-format scripts through the same tools (no persistence). | P1 |
| FR-30 | Markdown profile: block map (headings, fences, lists, tables, front matter), section addressing, structural checks (no unclosed fence, no broken table, front matter intact), and markdownlint when configured. | P0 |
| FR-31 | Python profile: version-aware syntax check, block-relative re-indentation, symbol addressing, and optional safe auto-fix of new diagnostics inside the edited range (`autofix: "safe"`). | P0 |
| FR-32 | Background execution: with `background: true`, the execute tools return a `job_id` immediately, and the agent polls progress and partial outputs through `kernel_control` (`status`). `interrupt` stops the job. | P1 |

### 2.5 Non-functional requirements

- **Performance:** p95 under 300 ms for edits without lint and under 2 s with lint on files up to 2,000 lines. Hard timeout per subprocess (default 10 s).
- **Execution limits:** foreground runs time out at 60 s per cell and 300 s per call by default, while background jobs have no per-call limit; output capped at 20,000 characters per cell; idle kernels shut down after 30 minutes.
- **Reliability:** all writes are atomic (write to temp file, fsync, rename). No partial writes.
- **Safety:** workspace path jail, protected paths, no shell interpolation (see 3.9). Executed code is not sandboxed and is covered by approval annotations and a config switch.
- **Portability:** macOS, Linux, Windows. Windows must handle CRLF and drive-letter paths correctly.
- **Token efficiency:** responses default to a summary plus a truncated diff (max about 60 lines) and the top 10 new diagnostics.
- **Observability:** structured logs to stderr and an optional JSONL audit log of every mutation and execution.
- **Compatibility:** MCP spec-compliant; tested with MCP Inspector and Cursor.

### 2.6 Success metrics

| Metric | Target | How measured |
| --- | --- | --- |
| New lint/format errors per agent edit | −80% vs. baseline | A/B on a fixed task suite, with and without the server |
| Notebook corruption incidents | 0 | Fixture corpus plus dogfooding |
| Python or Markdown edits that leave a parse error or broken structure | 0 | Test suite plus audit log |
| First-attempt edit success rate | ≥ 95% | Audit log |
| Execution fidelity | 100% of fixture outputs match Jupyter | Golden notebooks run through the server and through `nbclient` |
| Agent adoption (share of edits routed through this server) | ≥ 70% with rule file | Audit log versus total edits |
| Median extra agent turns spent fixing lint | −50% | Task-suite transcripts |

### 2.7 Release plan

| Milestone | Deliverable | Rough duration | Status |
| --- | --- | --- | --- |
| M0: Spike | stdio server on the Python MCP SDK, `file_read`, `edit_apply` with hash guard, Cursor wiring | 1 week | done |
| M1: Core edits | Batch semantics, anchors, symbol and heading addressing, dry run, EOL and relative indentation, atomic writes | 1–2 weeks | done |
| M2: Validation | Python and Markdown profiles, Ruff, baseline diffing, range-contained format, `strict` mode | 2 weeks | done |
| M3: Notebooks | nbformat engine, cell tools, virtual-document linting, percent format | 2 weeks | done |
| M4: Execution | Kernel manager, the three execute tools, `kernel_control`, output normalization, timeouts, background jobs | 2–3 weeks | done |
| M5: Multi-kernel | Multi-kernel and remote kernels (`kernel_discover`), percent-format script support | 1 week | done |
| M6: Type checker | `type_check` with `pyright` / `mypy` on demand | 1 week | done |
| M7: Config | `config_detect`, `config_doctor`, `config_apply_fix`, rule file | 1–2 weeks | done |
| M8: Hardening | Windows paths, packaging metadata, documentation sync | 1 week | done |

### 2.8 Dependencies

- Official MCP Python SDK, `pydantic`, `markdown-it-py`, and `tree-sitter` (syntax-only fallback for other languages).
- Notebook and kernel libraries: `nbformat` and `jupyter_client`.
- User-installed project tools: Ruff (required for Python validation), `ipykernel` in the project environment (required for execution), and optionally `markdownlint-cli2`, Prettier, `pyright` or `mypy`.
- Cursor's MCP support (`.cursor/mcp.json`).

---

## Part 3: Design Specification

### 3.1 Architecture

```
Cursor Agent ──(MCP / stdio)──▶ cursor-edit-mcp
                                 │
   ┌──────────────┬──────────────┼───────────────┬──────────────┐
   ▼              ▼              ▼               ▼              ▼
Edit Engine    Notebook       Execution       Config         Profiles
(ranges,       Engine         Engine          Service        (Python,
 anchors,      (nbformat,     (kernel mgr,    (detect,        Markdown)
 hash, EOL,    percent        output          doctor)
 indent)       format)        normalizer)
   │              │              │
   └──────┬───────┘              ▼
          ▼                 Jupyter kernel
 Validation Pipeline        (project's Python env)
 parse ▶ format ▶ lint
          │
          ▼
 Tool Runners (sandboxed subprocesses)
   ruff · markdownlint · prettier · pyright
          │
          ▼
 Atomic Writer ▶ Workspace (disk)
```

Every edit tool follows one path: **read snapshot → apply in memory → validate → commit atomically → return a compact result.** Nothing touches disk until validation has passed (in `strict`) or been reported (in `warn`).

Execution tools take a separate path: **resolve the cell → send it to the kernel → normalize the outputs → return** (see 3.6.1). They change kernel state and, unless `persist: false`, write the outputs back into the notebook.

### 3.2 Technology decisions

| Decision | Choice | Rationale |
| --- | --- | --- |
| Language | **Python 3.11+** | Kernel execution (`jupyter_client`), notebook integrity (`nbformat`), Ruff, and `markdown-it-py` are all Python-native, and Python and Markdown are the target file types. Changed from the TypeScript choice in v0.1. |
| MCP framework | Official MCP Python SDK (FastMCP) | Typed tool schemas, stdio transport, progress notifications, and tool annotations. |
| Transport | **stdio** | Simplest, local, and what Cursor expects for local servers. |
| Python syntax, lint, format | Ruff, run as a subprocess on stdin | One tool covers syntax errors (target-version aware), lint, import sorting, and formatting, and it honours the project's `pyproject.toml` or `ruff.toml`. |
| Markdown | `markdown-it-py` for structure and line maps; `markdownlint-cli2` and Prettier when the project configures them | The block map gives exact line ranges for sections, fences, tables, and front matter. |
| Kernel | `jupyter_client` (`AsyncKernelManager`) | Reference implementation of the Jupyter messaging protocol, including interrupt and restart. |
| Notebook I/O | `nbformat` (read, validate, write) | Canonical serialization and schema validation instead of hand-rolled JSON. |
| Input validation | Pydantic | Schemas generated from type hints, with clear errors. |
| Packaging | PyPI package run with `uvx` (or `pipx`) | Zero-install team rollout. The kernel runs in the project's own environment, not the server's. |
| Other languages | tree-sitter syntax checks only; ESLint, Prettier, and `tsc` runners are P2 | Keeps v1 focused on Markdown and Python. |

*Why not TypeScript (the v0.1 choice):* it fit ESLint and Prettier well, but execution and notebooks need `jupyter_client` and `nbformat`, which would have required a Python sidecar process. With Markdown and Python as the targets, one Python process is simpler and more reliable.

### 3.3 Tool catalog (20 tools)

| Tool | Purpose | Mutates |
| --- | --- | --- |
| `file_read` | Read whole file or `[start_line, end_line]` with hash and line count. | No |
| `edit_locate` | Resolve an anchor to a line range: text or regex plus occurrence, a Python symbol (`Class.method`), or a Markdown heading (the whole section). | No |
| `edit_apply` | Apply a batch of range edits atomically with validation, relative indentation, and optional safe auto-fix. | Yes |
| `file_create` | Create a file with validation; fails if it exists unless `overwrite`. | Yes |
| `validate_file` | Run the validation pipeline on the current file or on proposed content, without editing. | No |
| `type_check` | Run `pyright` or `mypy` on the current file or on proposed content, opt-in. | No |
| `config_detect` | Report effective linters, formatters, type-checkers, versions, and config sources for a file or workspace. | No |
| `config_doctor` | Diagnose configuration conflicts; return findings with suggested patches. | No |
| `config_apply_fix` | Apply one doctor patch with explicit confirmation. | Yes |
| `nb_outline` | List cells (1-based `cell_number`, ID, type, first line, output summary). | No |
| `nb_cell_read` | Read one or more cells in full. | No |
| `nb_cell_insert` | Insert a code, markdown, or raw cell at a position (`after_cell_id`, `index`, `start`, `end`). | Yes |
| `nb_cell_update` | Replace a cell's source, optionally a range within the cell; optionally change type. | Yes |
| `nb_cell_delete` | Delete cells by ID. | Yes |
| `nb_cell_move` | Move a cell to a new position. | Yes |
| `execute_cell` | Run one code cell on the notebook's kernel and return its outputs. | Kernel state and notebook outputs (unless `persist: false`) |
| `execute_all_cells` | Run all code cells in order; returns `cell_number` and `output` for each. | Kernel state and notebook outputs (unless `persist: false`) |
| `execute_cells_in_range` | Run cells `cell_start` to `cell_end` (1-based, inclusive) in order. | Kernel state and notebook outputs (unless `persist: false`) |
| `kernel_control` | `status` (including background job progress and results), `restart`, `interrupt`, or `shutdown` for a notebook's kernel. | Kernel state |
| `kernel_discover` | List available Jupyter kernels and their connection files. | No |
| `config_detect` | Report effective linters, formatters, type-checkers, versions, and config sources for a file or workspace. | No |
| `config_doctor` | Diagnose configuration conflicts; return findings with suggested patches. | No |

Tools are designed as separate verbs so the model picks the right one reliably. If the tool cap becomes a problem, the four `nb_cell_*` mutators can be folded into a single `nb_cells_apply` batch tool.

### 3.4 Edit model and addressing

**Addressing.** Lines are **1-based and inclusive**. All edits in a batch are expressed against the **original snapshot** identified by `base_hash`. The engine sorts edits bottom-up before applying, so earlier edits don't shift later line numbers. Overlapping ranges fail with `OVERLAPPING_EDITS`.

**Operations.**

- `replace`: replace `start_line..end_line` with `new_text`.
- `insert`: insert `new_text` after `after_line` (`0` means at the top of the file).
- `delete`: remove `start_line..end_line`.

**Guards.**

- `base_hash` (file-level): the write is rejected if the on-disk hash differs (`STALE_HASH`). The error includes the current hash and the current text of the affected region so the agent can retry in one step.
- `expect` (edit-level): a substring that must appear in the current text of the target range.

**Text hygiene.**

- Detect and preserve the file's EOL style, final-newline policy, and BOM.
- Re-indent inserted text to match the indentation context (using EditorConfig first, then inference from surrounding lines). This can be disabled per call with `reindent: false`.
- Reject mixed tabs and spaces introduced into a previously uniform file when `strict` is on.

**Request example (`edit_apply`):**

```json
{
  "path": "src/users/service.py",
  "base_hash": "sha256:9f2c…",
  "edits": [
    { "op": "replace",
      "range": { "start_line": 42, "end_line": 47 },
      "expect": "def fetch_user(",
      "new_text": "def fetch_user(self, user_id: str) -> User:\n    …\n" },
    { "op": "insert", "after_line": 10,
      "new_text": "from pydantic import BaseModel\n" }
  ],
  "validate": "strict",
  "format": "range",
  "indent_mode": "relative",
  "autofix": "none",
  "dry_run": false
}
```

**Response example:**

```json
{
  "ok": true,
  "path": "src/users/service.py",
  "new_hash": "sha256:41ab…",
  "lines": { "before": 188, "after": 191 },
  "diff": "@@ -40,8 +40,11 @@ …",
  "diagnostics": {
    "new": [],
    "preexisting_count": 3,
    "resolved_count": 1
  },
  "formatted": true,
  "written": true
}
```

### 3.4.1 Python and Markdown profiles

Both profiles are P0. Each adds addressing, indentation, and structure rules on top of the generic edit model.

**Python**

- **Addressing.** `edit_locate(by: "symbol", name: "UserService.fetch_user")` returns the line range of the function or class, decorators included, using `ast` line information.
- **Indentation.** With `indent_mode: "relative"` (the default), the server removes the common margin of `new_text` and re-applies the indentation of the target: the first line of a replaced range, or the neighboring statement for an insert. `"absolute"` keeps the text exactly as sent. Mixed tabs and spaces are rejected.
- **Syntax.** Ruff's parser, aware of the project's `target-version`; `ast.parse` is the fallback when Ruff is not installed.
- **Lint and format.** Ruff with the project's `pyproject.toml`, `ruff.toml`, or `.ruff.toml`.
- **Safe auto-fix.** With `autofix: "safe"`, the server applies Ruff's safe fixes only for diagnostics that are new and fall on touched lines or the import block (typically import sorting, unused imports, and whitespace). It never changes code outside that scope. Default is `none`.
- **Preserved as-is.** Encoding cookie, shebang, docstrings, and existing `# noqa` and `# type:` comments. The server never adds or removes them.

**Markdown**

- **Block map.** `markdown-it-py` (CommonMark plus GFM tables) gives each block's line range: headings, paragraphs, lists, tables, fenced code, and YAML front matter.
- **Addressing.** `edit_locate(by: "heading", text: "## Installation")` returns the section from that heading to the line before the next heading of the same or higher level. `include_subsections` defaults to true.
- **Structural checks (always on, no config needed).** No unclosed code fence (fence character and length are matched), consistent column count and delimiter row in tables, valid front matter, and consistent list indentation.
- **Whitespace is meaningful.** Two trailing spaces make a hard line break, and four leading spaces can create a code block. The server never strips trailing whitespace outside the touched lines, and it warns when an inserted line accidentally starts an indented code block.
- **Lint and format.** `markdownlint-cli2` when the project has a `.markdownlint*` config; Prettier (`--parser markdown`) when configured, always through hunk filtering. `config_detect` reports both.
- **Not covered in v1.** Code inside fenced blocks is not linted.

### 3.5 Validation pipeline

Stages run in order. Each stage can be skipped by mode or by missing tooling.

1. **Syntax parse.** Ruff's parser for Python (target-version aware), `markdown-it-py` structural checks for Markdown, tree-sitter for other languages. A new syntax error blocks the write in `strict`.
2. **Format (range-contained).** If a formatter is configured (Ruff format, Prettier), only the lines touched by the edit plus a small margin may change. Where the formatter has a range mode (`ruff format --range`), the server uses it. Otherwise it formats the post-edit text and keeps only the hunks that overlap the touched lines (plus 2 lines of margin), discarding the rest, so unrelated code is never reformatted.
3. **Lint with baseline diff.** Lint the file content **before** and **after** the edit in memory, using stdin where the tool supports it (for example, `ruff check --stdin-filename`) so config resolves by file path; for tools without stdin support, use a temporary sibling file. Diagnostics are fingerprinted by *(rule, message, normalized source line)* so they survive line shifts. The response reports only `new` diagnostics, plus counts of pre-existing and resolved ones.
4. **Type check (P2).** `pyright` or `mypy`, run on demand or in the background because of latency.

**Modes.**

| Mode | Behavior |
| --- | --- |
| `off` | Apply the edit; no validation. |
| `warn` (default) | Apply the edit; return new diagnostics. |
| `strict` | Do not write if any new error-severity diagnostic is introduced; return the diagnostics and the rejected diff. |

If a tool is missing or times out, the stage is marked `skipped` with a reason, and the edit proceeds in `warn` and fails in `strict` only if `require_all_stages: true` is set.

### 3.6 Notebook model

**Formats.** `.ipynb` (nbformat 4.x, preferably 4.5+ with cell IDs) and percent-format scripts, selected automatically by extension and content.

**Addressing.** Cells are addressed by **`cell_id`** (stable) with `index` as a fallback. `nb_outline` is the entry point: the agent gets IDs there and uses them in later calls.

**Integrity guarantees.**

- Parse with a JSON parser, never regex. Validate the notebook against the nbformat JSON schema **before and after** every mutation.
- Assign unique cell IDs (8-character, nbformat-compatible) for new cells; repair duplicates on write and report the repair.
- Store `source` in canonical form (list of lines, each ending in `\n` except the last).
- Serialize the way Jupyter does (`indent=1`, sorted keys, UTF-8 without ASCII escaping, trailing newline) to keep Git diffs minimal. A load-then-save of an unmodified notebook must be byte-identical (tested).
- Preserve notebook- and cell-level `metadata`, attachments, and unknown fields.

**Outputs.** New code cells get `outputs: []` and `execution_count: null`. When a code cell's source is changed, its outputs and execution count are cleared by default, because stale outputs are misleading; pass `keep_outputs: true` to retain them. Executing the cell later writes fresh outputs back (see 3.6.1).

**Cell-level ranged edits.** `nb_cell_update` accepts an optional `range` (cell-relative lines) with the same `expect` guard as file edits.

**Linting notebooks.**

- Where the linter has native notebook support (Ruff), use it.
- Otherwise build a **virtual document** by concatenating code cells with sentinel comments, neutralizing magics (`%x`, `%%x`, `!cmd` are commented out and tracked), run the linter, then map each diagnostic back to `cell_id` and cell-relative line.
- Markdown and raw cells are never linted.

**Percent format.** `# %%` begins a code cell, `# %% [markdown]` a markdown cell. The cell tools operate on these delimiters, generate IDs only in the tool output (not in the file), and run the normal text validation pipeline on the result.

### 3.6.1 Cell execution

Three tools run cells on a persistent kernel and return each cell's output to the agent. `kernel_control` manages the kernel.

**Cell numbering.** `cell_number` is 1-based and counts every cell in the notebook, markdown cells included, in file order. That is the number the user sees. The execution tools also accept `cell_id`. Because `cell_number` shifts when cells are inserted, deleted, or moved, the agent uses `cell_id` for edits and re-reads `nb_outline` after structural changes. Markdown and raw cells inside a range are not run and are reported as `skipped`.

| Tool | Input | Behavior |
| --- | --- | --- |
| `execute_cell` | `path`, `cell_number` or `cell_id`, `timeout_s?`, `persist?`, `images?` | Runs one code cell and returns its result. A markdown or raw cell returns `CELL_NOT_CODE`. |
| `execute_all_cells` | `path`, `stop_on_error?` (default true), `restart_kernel?` (default false), `timeout_s?`, `persist?`, `images?` | Runs every code cell in order. After an error, remaining cells are reported `not_run` unless `stop_on_error` is false. |
| `execute_cells_in_range` | `path`, `cell_start`, `cell_end` (1-based, inclusive), plus the same options | Runs the cells in order. `cell_start` greater than `cell_end`, or a value outside 1 to N, returns `CELL_RANGE_INVALID`. |

**Result shape.** Each entry carries `cell_number` and an `output` list, as requested; the extra fields are additive.

```json
{
  "notebook": "analysis.ipynb",
  "kernel": { "name": "python3", "status": "idle", "restarts": 0 },
  "results": [
    { "cell_number": 1, "cell_id": "a1b2c3d4", "status": "ok",
      "execution_count": 11, "duration_ms": 120,
      "output": [
        { "output_type": "stream", "name": "stdout", "text": "loaded 1,204 rows\n" }
      ] },
    { "cell_number": 2, "cell_id": "e5f6a7b8", "status": "error",
      "execution_count": 12, "duration_ms": 45,
      "output": [
        { "output_type": "error", "ename": "KeyError", "evalue": "'price'",
          "traceback": "KeyError: 'price'  (cell 2, line 3)\n..." }
      ] },
    { "cell_number": 3, "cell_id": "c9d0e1f2", "status": "not_run", "output": [] }
  ],
  "stopped_at": 2
}
```

`status` is one of `ok`, `error`, `timeout`, `not_run`, or `skipped`. Items in `output` use the nbformat output shapes, so the agent sees a familiar structure: `stream` (`name`, `text`), `execute_result` and `display_data` (`data` keyed by MIME type), and `error` (`ename`, `evalue`, `traceback`).

**Output normalization.**

- Merge consecutive stream chunks of the same stream, apply carriage-return semantics so progress bars such as `tqdm` show only their final state, and honour `clear_output`.
- Strip ANSI escape codes. Tracebacks use cell-relative line numbers and drop IPython-internal frames.
- Prefer `text/plain`. Use `text/html` only when no plain alternative exists, converted to text.
- Images are saved to `.cache/cursor-edit-mcp/` and returned as `{ "saved_to": "...", "bytes": N }`. With `images: "inline"` they are returned as MCP image content for vision-capable agents; `"omit"` returns counts only.
- Cap each cell at 20,000 characters, keeping the head and tail and eliding the middle, with `truncated: true`.
- The kernel runs with stdin disabled, so a cell that calls `input()` fails with a clear error instead of hanging.

**Kernel lifecycle.**

- One kernel per notebook path, started on the first execute call. Variables and imports persist across calls, so cell 3 sees names defined in cell 1.
- Interpreter order: the `python` path in server config, then `.venv/` in the workspace, then `VIRTUAL_ENV`, then the notebook's `kernelspec`. If `ipykernel` cannot be imported there, the call returns `KERNEL_UNAVAILABLE` with the exact install command.
- The working directory is the notebook's folder. The environment is inherited from the Cursor process.
- Calls for the same notebook are serialized. A call that arrives while a cell is running returns `KERNEL_BUSY` and names the running cell; there is no hidden queue.
- An idle kernel shuts down after 30 minutes, and all kernels shut down when the server exits. `restart_kernel: true` or `kernel_control` with `restart` clears state.

**Timeouts.** In foreground runs the per-cell limit is `timeout_s` (default 60 s) and the per-call limit is 300 s. On timeout the server interrupts the kernel, waits 5 s, restarts it if it is still busy, and returns the partial output with `status: "timeout"`. A progress notification is sent after each finished cell.

**Background execution.** Any execute tool accepts `background: true`. The call returns at once with `{ "job_id": "...", "status": "running" }` and the kernel keeps working. The agent polls with `kernel_control(action: "status", job_id)`, which returns the job state (`running`, `done`, `interrupted`, or `failed`), the cell now running with its elapsed time, and the results of all finished cells in the same `cell_number` and `output` shape. `kernel_control(action: "interrupt")` stops the job. Background jobs have no per-call limit, and the per-cell `timeout_s` defaults to 1 hour. With `persist: true`, outputs are written as each cell finishes (an exception to the one-write-per-call rule below), so a long run shows progress in the file and a crash loses at most the running cell. Only one job runs per kernel; a second execute call returns `KERNEL_BUSY`. Finished job results are kept for 1 hour or until the kernel shuts down.

**Persistence.** `persist` defaults to **true**: after the call, each executed cell's `outputs` and `execution_count` are written into the `.ipynb`, so the notebook always shows the results of the last run. `persist: false` gives an agent-only run that leaves the file untouched. Every response carries `persisted: true` or `false`, plus a `persist_error` when false.

- **One write per call.** The write happens after the last cell finishes, including after an error or timeout (covering the cells that did run), through the normal atomic writer.
- **Merge by cell ID.** The server re-reads the file at write time and applies outputs by `cell_id`. If a cell's source changed after it was executed, that cell's outputs are skipped, so a stale result never lands on edited code.
- **Full fidelity in the file.** Persisted outputs come from the original kernel messages, including base64 images and `text/html`, up to 2 MB per output. Larger outputs are truncated in the file with a marker. The agent still receives the normalized view, capped at 20,000 characters.
- **Untouched cells.** Cells that were not run keep their existing outputs. Cells edited since their last run have empty outputs, per the edit rule in 3.6.
- **Failures don't fail the run.** If the write fails (for example `STALE_HASH` because Cursor holds unsaved changes in that notebook), the execution results are still returned, with `persisted: false` and the reason.
- **Percent-format scripts** have no output store, so they are executed without persistence.

**Safety.** Executed code is not sandboxed; it runs with the user's permissions. The notebook path still passes the path jail, the three execute tools carry MCP annotations so Cursor asks for approval unless the user enabled auto-run, `execution.enabled: false` disables them, and every run is logged to the audit log (notebook, cell IDs, duration, status; not the outputs).

### 3.7 Configuration service

**`config_detect`** resolves what actually applies to a path, using the tools themselves rather than guessing:

- ESLint: `eslint --print-config <file>` (and flat versus legacy format detection).
- Prettier: `prettier --find-config-path <file>` and `--file-info`.
- Ruff: `ruff check --show-settings <file>` (including `line-length`, `select`, per-file ignores, notebook includes).
- TypeScript: `tsc --showConfig`.
- EditorConfig: parse `.editorconfig` for indent style, size, EOL, and final newline.

The output is a compact summary (tool, version, config source, key settings) rather than raw dumps.

**`config_doctor`** checks, at minimum:

| Check | Example finding |
| --- | --- |
| Conflicting config formats | Both `.eslintrc.*` and `eslint.config.js` present; ESLint version ignores one. |
| Formatter/linter overlap | ESLint stylistic rules active without `eslint-config-prettier`. |
| Line-length or indent mismatch | Ruff `line-length = 100` but Black or EditorConfig says 88. |
| Config precedence surprises | `ruff.toml` shadowing `pyproject.toml` settings. |
| tsconfig coverage | A file under edit is not matched by `include`, so type-aware lint rules fail. |
| Alias resolution | `paths` aliases in `tsconfig` not reflected in the ESLint import resolver. |
| Missing tools or versions | `ruff` not on PATH; ESLint plugin missing from `node_modules`. |
| Notebook support | Ruff not configured to include `.ipynb`; notebook-specific per-file ignores absent. |

Each finding returns `severity`, `evidence` (file and line), `explanation`, and a `suggested_patch` as a unified diff. In v1 the doctor **never writes**. `config_apply_fix` (P2) will apply a patch only with an explicit confirmation token.

### 3.8 Error model

All errors are structured, small, and carry a `hint` the agent can act on in the next call.

| Code | When | Hint |
| --- | --- | --- |
| `STALE_HASH` | `base_hash` does not match disk | Includes current hash and the affected region; re-issue with the new hash. |
| `RANGE_OUT_OF_BOUNDS` | Lines outside the file | Includes the actual line count. |
| `EXPECTED_TEXT_MISMATCH` | `expect` not found in range | Includes the current text of the range. |
| `OVERLAPPING_EDITS` | Two edits touch the same lines | Lists the conflicting edit indexes. |
| `ANCHOR_NOT_FOUND` | Text, symbol, or heading not found, or ambiguous | Lists candidates with line numbers. |
| `SYNTAX_ERROR_INTRODUCED` | New parse error in `strict` | Includes line, column, and message. |
| `LINT_REGRESSION` | New error-severity diagnostics in `strict` | Lists the new diagnostics. |
| `PATH_FORBIDDEN` | Outside workspace or protected | Names the rule that blocked it. |
| `NOTEBOOK_INVALID` | Input or result fails nbformat validation | Names the schema path. |
| `CELL_NOT_FOUND` | Unknown `cell_id` | Suggests calling `nb_outline`. |
| `CELL_NOT_CODE` | `execute_cell` on a markdown or raw cell | Names the cell type. |
| `CELL_RANGE_INVALID` | `cell_start` greater than `cell_end`, or outside 1 to N | Includes the cell count. |
| `KERNEL_UNAVAILABLE` | No usable interpreter, or `ipykernel` missing | Names the interpreter tried and the install command. |
| `KERNEL_BUSY` | Another cell is running on this notebook's kernel | Names the running cell; wait, or call `kernel_control` with `interrupt`. |
| `TOOL_UNAVAILABLE` | Linter/formatter missing or timed out | Names the tool and how to install it. |

An execution timeout is not an error code. It is reported as `status: "timeout"` on the cell, with the partial output.

### 3.9 Security and safety

- **Path jail.** Every path is resolved to a real path (symlinks followed) and must lie inside the workspace root. `PATH_FORBIDDEN` otherwise.
- **Protected paths.** `.git/`, `node_modules/`, `.venv/`, and secret-bearing files (for example `.env*`, `*.pem`, `id_*`) are denied for mutation by default; the denylist and allowlist are configurable.
- **No shell.** Subprocesses are spawned with argument arrays; user-provided strings are never interpolated into a shell command.
- **Resource limits.** Per-call timeout (default 10 s) for linters and formatters, maximum file size (default 2 MB), maximum edits per batch (default 100), and maximum subprocess output size. Execution has its own limits (see 3.6.1).
- **Config is code.** Some tools execute project code: Ruff and markdownlint can load plugins or custom rules, and Prettier loads JavaScript config files. The server therefore runs tools only inside the workspace the user already trusts, and documents this clearly.
- **Code execution.** The `execute_*` tools run arbitrary notebook code with the user's permissions, and that code is not sandboxed. Mitigations: the path jail applies to the notebook, the kernel's working directory is the notebook's folder, the tools are annotated so Cursor asks for approval, execution can be disabled with `execution.enabled: false`, and every run is audit-logged.
- **Audit log.** Optional JSONL log of path, operation, before/after hash, and timestamp for every mutation.

### 3.10 Concurrency and consistency

- A **per-file async mutex** serializes writes to the same path.
- The **hash precondition** is the primary defense against both agent staleness and concurrent human edits.
- **Atomic write:** write a temp file in the same directory, fsync, then rename. File mode and ownership are preserved.
- **Editor-buffer risk:** the server edits disk. If Cursor has unsaved changes, the hash check will usually fail, which is the desired behavior. The rule file instructs the agent to ask the user to save when `STALE_HASH` recurs. A file-watcher-based reconciliation is a stretch goal for M6.

### 3.11 Cursor integration

**`.cursor/mcp.json` (project-level):**

```json
{
  "mcpServers": {
    "cursor-edit": {
      "command": "uvx",
      "args": ["cursor-edit-mcp"],
      "env": { "WORKSPACE_ROOT": "${workspaceFolder}" }
    }
  }
}
```

Verify during M0 that your Cursor version interpolates `${workspaceFolder}`; if not, fall back to `os.getcwd()`.

**`.cursor/rules/edit-mcp.mdc` (steering the agent):**

- Prefer `edit_apply` over whole-file rewrites for any change to an existing file.
- Call `file_read` first and pass its `base_hash` back.
- Use `edit_locate` instead of guessing line numbers: `by: "heading"` for Markdown sections, `by: "symbol"` for Python functions and classes.
- Use `nb_*` tools for every `.ipynb` change; never edit notebook JSON as text.
- After editing a code cell, run it with `execute_cell`. Use `execute_cells_in_range` to re-run dependent cells and `execute_all_cells` for a final check.
- Use `cell_id` for edits and `cell_number` for execution, and re-read `nb_outline` after inserting or deleting cells.
- Call `config_detect` before the first edit in an unfamiliar file type.
- On `LINT_REGRESSION`, fix the reported diagnostics and retry; do not switch to `validate: off`.

### 3.12 Observability

- Structured JSON logs to **stderr** only (stdout is reserved for the MCP protocol).
- Per-call metrics: latency by stage, tool used, outcome, and error code.
- Optional audit log (3.9) doubles as the data source for the adoption and success metrics in the PRD.

### 3.13 Testing strategy

- **Unit tests:** range math, batch ordering, overlap detection, EOL/BOM/indent handling, hash guards.
- **Property-based tests:** applying a batch equals applying the same edits sequentially in reverse line order; a replace followed by its inverse restores the original bytes.
- **Profile tests:** corpora of real READMEs and Python modules. Every edit result must parse (`ast.parse`, `markdown-it-py`), keep fences balanced, and preserve unedited bytes. A relative-indent matrix covers tabs, spaces, nested blocks, multi-line strings, and decorators.
- **Golden notebooks:** a fixture corpus (with outputs, attachments, widgets, magics, odd metadata). Round-trip of an unmodified notebook must be byte-identical; every mutation result must pass the nbformat schema and open in Jupyter.
- **Execution tests:** fixture notebooks run through the server and through `nbclient`; normalized outputs must match. Cases: interleaved stdout and stderr, rich display, `tqdm` carriage returns, `clear_output`, exceptions, `input()`, infinite loops (timeout and interrupt), kernel crash, missing `ipykernel`, and state persisting across calls.
- **Lint fixtures:** known-good and known-bad edits per language, verifying that baseline diffing reports exactly the new diagnostics.
- **Contract tests:** each tool's schema and error codes, run through MCP Inspector.
- **End-to-end:** a scripted Cursor task suite run with and without the server to produce the PRD's regression and turn-count metrics.
- **Cross-platform CI:** macOS, Linux, Windows (CRLF, drive letters, long paths).

### 3.14 Repository layout

```
cursor-edit-mcp/
├─ src/cursor_edit_mcp/
│  ├─ server.py           # FastMCP bootstrap, tool registration
│  ├─ tools/              # one module per tool (pydantic schemas + handlers)
│  ├─ edit/               # ranges, anchors, hashing, EOL/indent, atomic writer
│  ├─ profiles/           # python.py (symbols, indent), markdown.py (block map, checks)
│  ├─ notebook/           # nbformat engine, percent-format engine, virtual docs
│  ├─ execution/          # kernel manager, runner, output normalizer, timeouts
│  ├─ validate/           # pipeline, baseline diff, fingerprints
│  ├─ runners/            # ruff, markdownlint, prettier, pyright adapters
│  ├─ config/             # detect, doctor checks
│  ├─ security/           # path jail, denylist, limits
│  └─ errors.py           # error codes and hints
├─ fixtures/              # notebooks, lint cases, config scenarios
├─ tests/
├─ templates/             # mcp.json snippet, .cursor/rules file
├─ pyproject.toml
└─ README.md
```

### 3.15 Example flows

**A. Safe edit with lint regression caught**

1. Agent calls `file_read(path, 30–60)` and receives text and `base_hash`.
2. Agent calls `edit_apply(..., validate: "strict")`.
3. Server applies in memory, lints before and after, and finds one new `no-unused-vars` error.
4. Server returns `LINT_REGRESSION` with the diagnostic and line; nothing is written.
5. Agent corrects the edit, retries, and the server writes it and returns a diff with `diagnostics.new: []`.

**B. Notebook cell insert**

1. Agent calls `nb_outline(path)` and gets cell IDs.
2. Agent calls `nb_cell_insert(after_cell_id: "a1b2c3d4", type: "markdown", source: "## Cleaning")`.
3. Server validates schema, assigns a new ID, serializes in Jupyter style, and returns the updated outline slice.

**C. Configuration diagnosis**

1. Agent calls `config_detect(path: "app/models.py")` and learns Ruff is configured with line length 100 while EditorConfig says 88.
2. Agent calls `config_doctor` and receives the finding plus a suggested patch.
3. Agent proposes the patch to the user instead of silently changing config.

**D. Edit a cell and run it**

1. Agent calls `nb_outline` and sees that cell 4 (`cell_id` `e5f6a7b8`) failed with a `KeyError`.
2. It calls `nb_cell_update` with the corrected source.
3. It calls `execute_cells_in_range(cell_start: 3, cell_end: 5)`.
4. The server starts the kernel in the workspace `.venv`, runs cells 3 to 5 in order, and returns `cell_number` and `output` for each. Cell 4 now has `status: "ok"`.

**E. Python and Markdown range edits**

1. Agent calls `edit_locate(path: "src/users/service.py", by: "symbol", name: "UserService.fetch_user")` and gets lines 58–81.
2. It calls `edit_apply` with a new method body sent without indentation. The server re-indents it to the class body, Ruff reports no new diagnostics, and the file is written.
3. For `README.md`, `edit_locate(by: "heading", text: "## Installation")` returns lines 12–29. `edit_apply` replaces them, the structural checks confirm the fences are balanced, and markdownlint reports nothing new.

---

*End of document. Suggested next step after review: start M0 (the spike) to validate Cursor wiring and `${workspaceFolder}` behavior before investing in the validation pipeline.*
