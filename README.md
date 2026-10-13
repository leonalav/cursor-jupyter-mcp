# cursor-edit-mcp

Precise, guarded editing tools for Cursor's agent, served over MCP.

The problem this server exists to solve: agents edit code by rewriting whole
files or by fuzzy search-and-replace. Both land on the wrong lines when the file
has moved, and neither tells the agent whether it just broke something.

This server gives the agent two verbs — `file_read` and `edit_apply` — where
every edit is pinned to a content hash of the file it was planned against, and
every write is atomic. If the file changed underneath the agent, the edit is
rejected instead of silently landing in the wrong place.

**Status: M6.** Range edits, addressing, atomic writes, the validation pipeline
(syntax, range-contained format, lint with baseline diffing, `strict` mode), the
six notebook cell tools (now also operating on percent-format `.py` scripts),
cell execution against a real Python kernel (one kernel per file, persisted
outputs on `.ipynb` only, timeouts, restart, interrupt, shutdown), kernel
discovery, remote-kernel attachment via a Jupyter connection file, and
opt-in type checking with `pyright` or `mypy` are working.

---

## Requirements

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/) (recommended; `pipx` works too)

## Install

```bash
uv tool install cursor-edit-mcp
```

Or run it straight from a checkout:

```bash
uv run cursor-edit-mcp
```

## Wire it into Cursor

Copy `templates/mcp.json` to `.cursor/mcp.json` in your project:

```json
{
  "mcpServers": {
    "cursor-edit": {
      "type": "stdio",
      "command": "uvx",
      "args": ["cursor-edit-mcp"],
      "env": { "WORKSPACE_ROOT": "${workspaceFolder}" }
    }
  }
}
```

Then copy `templates/edit-mcp.mdc` to `.cursor/rules/edit-mcp.mdc` so the agent
knows to prefer these tools over whole-file rewrites.

`WORKSPACE_ROOT` sets the path jail: the server refuses to read or write
anything outside it. Cursor resolves `${workspaceFolder}` in `env` (verified
against Cursor's config-interpolation docs during M0). If a host ever leaves it
uninterpolated, or you delete the `env` block, the server falls back to its
working directory, which is the project folder for a stdio server.

## Validation

Before anything is written, the edit is validated. Stages run in order and each
can be skipped by mode or by missing tooling:

1. **Syntax** — Ruff's parser for Python (target-version aware), structural
   checks for Markdown. Falls back to `ast.parse` when Ruff is not installed,
   so the syntax stage never silently disappears.
2. **Format (range-contained)** — only the lines the edit touched, plus a
   two-line margin, may change. Ruff's `--range` is used when available.
3. **Lint** — run before and after the edit; only the difference is reported.

### Modes

| `validate` | Behavior |
| --- | --- |
| `off` | Skip the syntax and lint checks. Format and autofix still run. |
| `warn` (default) | Write the edit and return the new diagnostics. |
| `strict` | Refuse to write when a new error-severity problem appears. |

### Reporting only what you caused

Lint runs on the file **twice**, before and after, and the results are diffed by
fingerprint — rule, message, and normalized source line. That makes three things
true:

- A file with existing lint debt stays editable. Pre-existing problems are
  counted in `preexisting_count`, never reported as new.
- A warning that merely moved down a line because of your insert is not new.
- Only genuinely new problems land in `diagnostics.new`.

```json
"diagnostics": {
  "new": [{ "tool": "ruff", "code": "F841", "message": "...", "line": 4 }],
  "new_count": 1, "new_error_count": 1,
  "preexisting_count": 12, "resolved_count": 0
}
```

`stages` reports what actually ran. A stage that could not run is `skipped` with
a reason, so a clean result is never mistaken for an unvalidated one:

```json
"stages": [
  { "stage": "syntax", "status": "ok" },
  { "stage": "format", "status": "ok" },
  { "stage": "lint", "status": "skipped", "reason": "markdownlint is not configured…" }
]
```

### Options

| Option | Default | Effect |
| --- | --- | --- |
| `validate` | `warn` | `off` / `warn` / `strict` |
| `format` | `range` | `none` / `range` / `full` |
| `autofix` | `none` | `none` / `safe` |
| `require_all_stages` | `false` | In `strict`, also fail if a stage was skipped |

`off` disables the *checks* (syntax and lint) only. `format` and `autofix` are
independent: `validate: "off", format: "range"` formats the edit without
linting it.

With `autofix: "safe"`, only Ruff's *safe* fixes are applied, and only to lines
the edit touched plus the import block. Unsafe fixes are never applied
automatically.

Configuration is resolved per file through the tools' own rules — Ruff's
`pyproject.toml` / `ruff.toml`, `.markdownlint*`, `.prettierrc`. The server never
reinterprets a config file, because eventually it would disagree with the real
tool in ways that are hard to debug.

## Tools

### `edit_locate`

Resolves an anchor to a line range. Read-only.

```json
{ "path": "src/app.py", "by": "symbol", "name": "UserService.fetch_user" }
```

| `by` | Selector | Resolves to |
| --- | --- | --- |
| `anchor` (default) | `text` or `regex`, plus `occurrence` | The lines the match touches |
| `symbol` | `name`, e.g. `Class.method` | The definition, decorators included |
| `heading` | `text`, plus `include_subsections` | The Markdown section |

Returns `start_line`, `end_line`, `match`, and the file `hash`, so one call
gives you everything `edit_apply` needs.

`occurrence` is 0-based. If an anchor matches more than once and no occurrence
is given, the call fails as ambiguous and lists where it matched, rather than
guessing. A heading lookup stops at the next heading of the same or higher
level, so subsections are included unless you pass `include_subsections: false`.

A `#` inside a fenced code block is not a heading, so a Markdown example cannot
be mistaken for a section.

### `file_read`

Reads a whole file or a 1-based inclusive line range.

```json
{ "path": "src/app.py", "start_line": 10, "end_line": 25 }
```

Returns the content, the file's `line_count`, and a SHA-256 `hash` of the whole
file. That hash is the guard for the next call.

### `edit_apply`

Applies a batch of edits atomically.

```json
{
  "path": "src/app.py",
  "base_hash": "sha256:9f2c…",
  "edits": [
    { "op": "replace", "range": { "start_line": 42, "end_line": 47 },
      "expect": "def fetch_user(",
      "new_text": "def fetch_user(self, user_id: str) -> User:\n    …\n" },
    { "op": "insert", "after_line": 10, "new_text": "from pydantic import BaseModel\n" }
  ]
}
```

Operations:

| `op` | Fields | Effect |
| --- | --- | --- |
| `replace` | `range`, `new_text` | Replace lines `start_line..end_line` |
| `insert` | `after_line`, `new_text` | Insert after `after_line`; `0` means top of file |
| `delete` | `range` | Remove lines `start_line..end_line` |

Guarantees:

- Every line number refers to the snapshot `file_read` returned.
- The whole batch is validated before anything is written, so a rejected batch
  leaves the file untouched.
- The file is replaced atomically (temp file, fsync, rename).
- Line endings, final newline, encoding, BOM, and file mode are preserved.
- `dry_run: true` returns the diff without writing.

`indent_mode` controls what happens to `new_text`. The default,
`"relative"`, strips its common margin and re-applies the indentation of the
surrounding block, so code written flush-left lands at the right depth. The text
still has to be correct relative to itself — a body needs its own step. Pass
`"absolute"` to write it exactly as sent.

Because a `symbol` range includes decorators, replacing a decorated function
requires putting the decorator back into `new_text`.

### `validate_file`

Runs the validation pipeline without editing anything.

```json
{ "path": "src/app.py" }
{ "path": "src/app.py", "content": "def check():\n    return 1\n" }
```

Passing `content` validates that text *as if* it were the file's contents.
Returns every problem found (there is no "before" to diff against when
validating content that does not exist yet), plus the effective tooling.

### `type_check`

Opt-in type checking with `pyright` or `mypy`. The validation pipeline
covers syntax and lint on every edit; type checking is too slow and
noisy on a project that has not opted in, so it lives on its own tool
that you reach for when the question is "does this type-check?".

```json
{ "path": "src/users/service.py" }
{ "path": "src/users/service.py", "tool": "mypy" }
{ "path": "src/users/service.py", "tool": "pyright", "level": "warning" }
{ "path": "src/users/service.py", "content": "def f(x: int) -> int:\n    return x" }
```

`tool` is `auto` (the default; tries Pyright then mypy), `pyright`, or
`mypy`. `level` is `error` (default; only errors) or `warning` (errors
and warnings). `content` validates a proposed block without writing it,
the same way `validate_file` does. The tool is read-only: a sibling
temp file is created for the run and deleted when the call ends.

The result reports the tool that ran and its version, so a `pyright`
result is never mistaken for a `mypy` result. A missing tool returns
`skipped: true` with the reason rather than zero findings — silence on
this path is the worst possible output, because a clean answer from a
tool that was never asked is indistinguishable from a clean file.

### `config_detect`

Reports the effective tooling for a file, or for the whole workspace
when called with no `path`. Same shape as the `tooling` field on
`validate_file`, exposed on its own so the agent can plan a change
before touching anything:

```json
{ "path": "src/app.py" }
{ }
```

The workspace report lists every config file the project has for each
tool, marking which one is active. That is what the doctor consumes
to find conflicts.

### `config_doctor`

Diagnoses project-level tooling conflicts and misconfigurations. The
doctor is read-only: it inspects `pyproject.toml`, `ruff.toml`,
`.markdownlint*`, `.prettierrc*`, and `.editorconfig` for conflicts
and missing pieces, and returns a list of findings. Each finding has
a `severity` (`error`, `warning`, or `info`), an `evidence` block,
an `explanation`, a `suggested_patch` (unified diff), and a
`confirmation_token` for the apply step.

```json
{ }
{ "path": "pyproject.toml" }
```

The doctor covers v1-relevant checks: two co-existing Ruff configs,
conflicting `line-length` between Ruff and EditorConfig, legacy
`markdownlint` configs that newer versions ignore, and a missing
`ruff.toml` for projects that want their own rules.

### `config_apply_fix`

Applies one doctor-proposed patch with explicit confirmation. Pass
the `finding_id` and `confirmation_token` from the doctor's response;
a stale token (e.g. from an old chat log) is rejected with a hint to
re-run the doctor.

```json
{ "finding_id": "ruff_overlap::ruff::pyproject.toml::3",
  "confirmation_token": "<from config_doctor response>" }
```

This is the only mutating tool in the configuration service. The
patch is the unified diff the doctor proposed, applied to the file
the doctor referenced; the patch is verified to apply to the
current file content before the write goes through.

### `file_create`

Creates a file, validated first.

```json
{ "path": "src/new.py", "new_text": "def new():\n    return 1\n" }
```

Fails with `FILE_EXISTS` unless `overwrite: true`. Because a new file has no
previous version, there is no baseline: every diagnostic counts as new, so the
default `validate: "strict"` means a file that does not parse is not created.
Parent directories are created as needed.

## Errors

Failures come back as structured JSON with a code, a message, and a hint telling
the agent what to do next:

```json
{
  "code": "STALE_HASH",
  "message": "src/app.py changed since it was read.",
  "hint": "Re-read the file and re-issue the edit with the current base_hash.",
  "details": { "path": "src/app.py", "current_hash": "sha256:41ab…" }
}
```

| Code | Meaning |
| --- | --- |
| `STALE_HASH` | The file changed since `file_read`; re-read and retry |
| `ANCHOR_NOT_FOUND` | Nothing matched, or it matched more than once |
| `RANGE_OUT_OF_BOUNDS` | Line numbers fall outside the file |
| `EXPECTED_TEXT_MISMATCH` | `expect` was not found in the target range |
| `OVERLAPPING_EDITS` | Two edits in the batch touch the same lines |
| `SYNTAX_ERROR_INTRODUCED` | `strict`: the edit would leave the file unparseable |
| `LINT_REGRESSION` | `strict`: the edit introduces a new error-severity problem |
| `PATH_FORBIDDEN` | Path is outside the workspace or is protected |
| `NOTEBOOK_INVALID` | Not valid JSON, not a notebook, or fails the nbformat schema |
| `CELL_NOT_FOUND` | No cell with that id; the error lists the ids that do exist |
| `CELL_NOT_CODE` | Asked to run a markdown or raw cell on the kernel |
| `CELL_TYPE_INVALID` | `cell_type` was not `code`, `markdown`, or `raw` |
| `CELL_RANGE_INVALID` | Reversed or out-of-bounds execution range |
| `KERNEL_NOT_RUNNING` | `kernel_control` action on a notebook without a kernel |
| `KERNEL_START_FAILED` | The kernel could not start or never became ready |
| `FILE_NOT_FOUND` | No such file |
| `FILE_EXISTS` | `file_create` found the path already taken |
| `FILE_TOO_LARGE` | File exceeds `MAX_FILE_BYTES` (default 2 MB) |
| `TOO_MANY_EDITS` | Batch exceeds `MAX_EDITS` (default 100) |
| `INVALID_EDIT` | The edit or the arguments are malformed |
| `DECODE_ERROR` | The file is not decodable text |
| `WRITE_FAILED` | The file could not be written |

Rejections in `strict` mode carry the diff that was *not* applied, so the edit can
be corrected without another read.

## Configuration

All optional, set through the `env` block in `mcp.json`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `WORKSPACE_ROOT` | server CWD | Root of the path jail |
| `MAX_FILE_BYTES` | `2097152` | Largest file that may be read or written |
| `MAX_EDITS` | `100` | Largest batch `edit_apply` accepts |
| `MAX_CONTENT_CHARS` | `200000` | Longest content `file_read` returns |
| `TOOL_TIMEOUT_S` | `10` | Hard timeout per validation subprocess (Ruff, markdownlint, Prettier) |
| `PROTECTED_PATHS` | — | Extra names denied for mutation |

`.git/`, `node_modules/`, `.venv/`, `venv/`, `__pycache__/`, `.env*`, `*.pem`,
`*.key`, and SSH keys are denied for mutation by default.

## Development

```bash
uv sync
uv run pytest
uv run cursor-edit-mcp
uv run python scripts/acceptance.py   # end-to-end over the real protocol
uvx ruff check src tests && uvx ruff format --check src tests
```

Validation needs [Ruff](https://docs.astral.sh/ruff/) on `PATH` or in the
workspace `.venv`; the server looks in the venv first, then falls back to
`PATH`. `markdownlint-cli2` and Prettier are used only when the project
configures them. Without Ruff, the syntax stage falls back to `ast.parse` and
the lint stage is reported as skipped.

Notebook tools need [nbformat](https://pypi.org/project/nbformat/), which is a
declared dependency. No kernel is needed — that is M4.

## Notebooks

Notebook tools work on `.ipynb` files (nbformat 4.x). They never touch the
file's JSON as text — it is parsed, mutated as a structure, validated against
the nbformat schema, and re-serialized the way Jupyter does.

### Addressing

Cells have two names and they behave differently.

- **`cell_id`** is the identity. It survives inserting, deleting, and moving
  cells, so it is the right thing to pass between calls.
- **`cell_number`** is a position: 1-based, counting every cell including
  markdown, matching the number Cursor shows. It shifts as soon as anything
  moves.

Start with `nb_outline`. It returns every cell's number, id, type, first line,
line count, and an output summary, so one call is enough to find what you need.

```json
{ "path": "analysis.ipynb" }
```

### Editing a cell

`nb_cell_update` takes `cell_id`, and either a whole `source` or a `range` of
the cell:

```json
{ "path": "analysis.ipynb",
  "cell_id": "a1b2c3d4",
  "range": { "start_line": 12, "end_line": 14 },
  "expect": "df = pd.read_csv(",
  "source": "df = pd.read_csv(path)" }
```

**`range` is cell-relative.** Line 1 is the first line of *that cell*, not of the
file. `expect` works the same way as on `edit_apply`.

Changing a code cell's source clears its outputs and `execution_count`. An output
that no longer matches its code is worse than no output, because it looks
trustworthy. Pass `keep_outputs: true` to retain them.

### Position and type

```json
{ "path": "a.ipynb", "source": "## Cleaning", "cell_type": "markdown",
  "after_cell_id": "a1b2c3d4" }
{ "path": "a.ipynb", "source": "x = 1", "index": 0 }
{ "path": "a.ipynb", "cell_id": "e5f6a7b8", "destination": 0 }
```

Position an insert with exactly one of `after_cell_id`, `index`, `at_start`, or
`at_end`. `nb_cell_move` takes a 0-based `destination`. Changing `cell_type`
between `code`, `markdown`, and `raw` adds or removes the fields the schema
requires for that type.

### Linting cells

Code cells are linted as a notebook, so diagnostics come back as
`cell_id` plus a cell-relative line. `%matplotlib inline`, `%%time`, and
`!pip install` are neutralized before linting, so a cell full of magics is
still checked instead of reporting one syntax error per magic. Markdown and raw
cells are never linted.

`validate` works exactly as on `edit_apply`, including reporting only the
problems the change introduced — an existing warning in another cell does not
block you.

### Executing cells

Execution runs on a real Python kernel managed by `jupyter_client`. The kernel
is started lazily on the first execution and kept alive across calls — state
on a notebook's kernel is shared between cells, the same way Jupyter behaves.

```json
{ "path": "analysis.ipynb", "cell_id": "a1b2c3d4" }
{ "path": "analysis.ipynb", "stop_on_error": true, "restart_kernel": false }
{ "path": "analysis.ipynb", "cell_start": 3, "cell_end": 5 }
{ "path": "analysis.ipynb", "action": "status" }
{ "path": "analysis.ipynb", "action": "restart" }
{ "path": "analysis.ipynb", "action": "interrupt" }
{ "path": "analysis.ipynb", "action": "shutdown" }
```

**`execute_cell`** runs one code cell. The result carries its
`execution_count` reported by the kernel, `duration_ms`, an `output` list with
stream, `execute_result`, `display_data`, and error entries, and an `error`
object with `ename`, `evalue`, and the full `traceback` when something
threw. A markdown or raw cell returns `CELL_NOT_CODE`.

**`execute_all_cells`** runs every code cell in order. `stop_on_error: true`
(default) halts at the first failure; `restart_kernel: true` (default false)
throws the kernel's state away first.

**`execute_cells_in_range`** runs the cells in a 1-based inclusive range. The
range is inclusive and `cell_start <= cell_end`; out-of-bounds or reversed
ranges return `CELL_RANGE_INVALID`. Markdown and raw cells in the range are
reported as `skipped`, not run.

**`kernel_control`** manages the kernel itself. `status` (default) returns
its name, alive/ready flags, restart count, and time of last activity. No
kernel is running yet for an untouched notebook, and that is reported. The
other actions — `restart`, `interrupt`, `shutdown` — speak to a running
kernel. A `restart` wipes the interpreter and starts fresh; `interrupt` is
the SIGINT-equivalent for a long-running cell; `shutdown` removes the
kernel from the registry, and the next execution starts a new one.

**Persistence.** `persist: true` (default) writes the cell's outputs and
`execution_count` back to the notebook, the same way Jupyter does. Pass
`persist: false` to run without saving.

**Timeouts.** Each call takes a per-cell `timeout_s` (default 30). When it
expires the kernel is interrupted and the result reports
`error.reason = "timeout"`. Long-running cells can be interrupted manually
with `kernel_control: interrupt`.

**Images.** The `output` list keeps text mime types (`text/plain`, `text/html`,
`text/latex`, `application/json`). Image data is dropped by default to keep
responses small; pass `images: "base64"` to include `image/png` etc.

### Percent-format scripts

The same six cell tools accept percent-format `.py` files: a plain Python
file whose lines starting with `# %%` mark cell boundaries, the way VS Code
and Spyder do.

```python
# %%
import os

# %% [markdown]
# Notes

# %% my cell
x = 1
```

The cell tools read and write these files in place, with no round trip
through `.ipynb`. Imports, comments, and code outside any cell are
preserved; a missing `# %%` raises `SCRIPT_INVALID` so a regular `.py`
file is not silently turned into a virtual notebook. Cell types are
inferred from the marker: `# %%` is code, `# %% [markdown]` is markdown,
`# %% [raw]` is raw.

**Stable cell ids.** Every cell gets an 8-character id derived from the
SHA-1 of its source. The id is computed on load (so two reads of the
same file see the same id) and is also written into the file as a
`# id:` comment on the next mutation, so an edit does not lose the id
across reads. A cell's id is content-derived: editing the source
changes the id, so re-read the outline after an edit.

**Execution.** `execute_cell`, `execute_all_cells`, and
`execute_cells_in_range` work on percent scripts, with one difference:
`persist` is forced to `false` because the file has no on-disk location
for outputs. The kernel state still persists across calls.

### Kernel discovery and remote kernels

**`kernel_discover`** lists the Jupyter kernelspecs installed on this
machine, with the language, the argv that would be spawned, and whether
the interpreter is on `PATH`:

```json
{ "kernels": [
    { "name": "python3", "language": "python", "is_default": true,
      "executable": "...\\python.exe", "executable_status": "ok" } ] }
```

The tool is read-only — it never starts a kernel. Use the result to pick
a `kernelspec` for a notebook, or to confirm a non-Python language (R,
Julia, IRKernel, Bash) is available before opening a notebook that needs
it. The notebook's own `kernelspec` is what `execute_cell` uses; when
it names a kernelspec Jupyter can resolve, the right kernel is started
automatically.

**Remote kernels.** A JupyterHub server, a long-running container, or any
other tool that started a kernel elsewhere writes a *connection file* —
the JSON document Jupyter produces with `ip`, `shell_port`, `iopub_port`,
`stdin_port`, `hb_port`, `key`, and `signature_scheme`. Pass it to any
execute tool or to `kernel_control`:

```json
{ "path": "analysis.ipynb", "cell_id": "a1b2c3d4",
  "connection_file": "kernel-conn.json" }
```

The registry connects to the running kernel rather than starting a
local one. The session is marked `remote`: `kernel_control restart` and
`kernel_control shutdown` only close the local channels — the kernel
itself is the user's, and only the host that started it can end it.
Status reports `remote: true` and the `connection_file` so a caller can
tell the two paths apart.

### What is guaranteed

- The file is parsed with a JSON parser, never with regex.
- The notebook is validated against the nbformat schema before and after every
  change.
- Cell ids are unique on write; duplicates are repaired and reported rather
  than refused.
- Notebook and cell `metadata`, `attachments`, outputs, and fields outside the
  nbformat schema are preserved. Unknown cell fields are reported in the result.
- A load-then-save of an untouched notebook is byte-identical, so editing one
  cell does not rewrite the rest of the file.
- The file's own line endings and final-newline convention are preserved.
- A kernel is one-per-notebook. State from one notebook's kernel never leaks to
  another.

## Roadmap

| Milestone | Deliverable |
| --- | --- |
| ~~M0~~ | ~~Spike: stdio server, `file_read`, `edit_apply` with hash guard, Cursor wiring~~ |
| ~~M1~~ | ~~Batch semantics, anchors, symbol and heading addressing, dry run, relative indentation~~ |
| ~~M2~~ | ~~Validation: Python and Markdown profiles, Ruff, baseline diffing, `strict` mode~~ |
| ~~M3~~ | ~~Notebooks: nbformat engine, cell tools, virtual-document linting~~ |
| ~~M4~~ | ~~Execution: one real Python kernel per notebook, persist outputs, restart/interrupt~~ |
| ~~M5~~ | ~~Multi-kernel and remote kernels, percent-format scripts, kernel discovery~~ |
| ~~M6~~ | ~~Type checker (`pyright` / `mypy` on demand)~~ |
| ~~M7~~ | ~~`config_detect` / `config_doctor` / `config_apply_fix`~~ |
| M8 | Hardening: Windows paths, buffer conflicts, packaging |
| M8 | Hardening: Windows, buffer conflicts, packaging, docs |