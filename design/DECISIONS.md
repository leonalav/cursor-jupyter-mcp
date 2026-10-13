# M5 design decisions

## Decision: cell id scheme for percent-format scripts is content-derived, optionally persisted

**What.** A percent-format cell's id is the first 8 hex characters of the
SHA-1 of its source. When the file is rewritten (after a mutation), the id
is also written into the file as a `# id:` comment so the cell has a
stable id even after its source changes.

**Why.** A random per-load id (the first attempt) made a read followed by
an edit see different ids for the same cell, because each load generated
fresh ids. A pure on-disk id (the second attempt) made a read of a file
without ids return an id that the next read could not reproduce, because
nothing forced a write between them. A content-derived id is stable
across reads of the same content with no side effects; a persisted id
preserves the address across an edit. The two together cover both cases.

**Alternatives.**
- Random id per load: rejected. Same source, different ids across calls;
  the cell tools became unreliable.
- Persisted `# id:` only: rejected. First outline returned ids that did
  not survive a re-read until something wrote the file.
- Hashing the cell's marker line plus its position: rejected. The id
  changed when the user moved the cell, which `nb_cell_move` does all
  the time.
- Treating the id as in-memory only and forcing a write on read: rejected.
  Reads had a side effect, and the user could not be sure the file was
  untouched after a `nb_outline`.

**Tradeoff.** A cell's id changes when its source changes. The user
re-reads the outline after an edit, the same way they re-read it after an
insert. The `# id:` line in the file is small (a single comment) and is
preserved by `to_bytes`.

## Decision: percent scripts are non-persistent on execute

**What.** `execute_cell`, `execute_all_cells`, and `execute_cells_in_range`
on a `.py` percent script run the cell source on the kernel and return
the outputs, but never write the outputs back to the file. `persist` is
forced to `false`.

**Why.** A percent script is a regular Python file the user runs as a
notebook. The user can also run it with `python script.py`, and the
on-disk form must remain runnable. Writing outputs would require
inventing a storage convention in the `.py` file (a sidecar, a header
comment, an embedded cell-state object), and every convention is wrong
for some other use. The kernel still keeps state across calls, so a
multi-cell run is still coherent.

**Alternatives.**
- Embed outputs as a sidecar `.cells.json`: rejected. Two files to keep
  in sync, and the script loses its self-contained property.
- Embed outputs as a comment block at the bottom of the file: rejected.
  A `print()` that emits the same text would collide with the sidecar.
- Always run as a notebook: rejected. The user wrote a `.py` for a reason
  and expects to be able to run it with `python`.

**Tradeoff.** A user who wants outputs persisted has to use a notebook.
That is the right surface for persistent outputs.

## Decision: a remote kernel is a session this server does not own

**What.** A session created via `connect_remote(connection_file)` is
marked `remote=True`. `restart` and `shutdown` on a remote session only
close the local channels and remove the registry entry. The kernel
process itself is the user's, on its own host.

**Why.** A remote kernel might be a JupyterHub session the user is
sharing with a browser tab, a long-running container, or any other
process this server cannot safely kill. Killing it from here would
destroy state the user is using elsewhere. The right way to end a
remote kernel is on the host that started it.

**Alternatives.**
- Allow `restart` and `shutdown` to reach the remote kernel anyway:
  rejected. The same registry code path would either be inert
  (silently broken) or destructive (silently wrong).
- Treat all kernels the same and document that the user is responsible
  for not connecting to shared ones: rejected. The `remote` flag is
  what tells the user, the registry, and the test what is going on.

**Tradeoff.** The `DummyManager` is a no-op on `shutdown_kernel` and
`interrupt_kernel`. The cell tools still see a working client and
report outputs the same way, but the user has to know that the kernel
survived the call. The status report carries `remote: true` and the
`connection_file` so a caller can tell.

## Decision: kernel discovery uses the Python KernelSpecManager, not a subprocess

**What.** `kernel_discover` calls `jupyter_client.kernelspec.KernelSpecManager.get_all_specs()`.
It does not run `jupyter kernelspec list` as a subprocess and parse the
output.

**Why.** The KernelSpecManager is the same library Jupyter itself uses
to resolve kernelspecs. It reads the same `kernel.json` files from the
same well-known locations, and it does so in-process, which is faster
and avoids the cross-platform dance of parsing a fixed-width table.
Calling the Python API is what `jupyter_client` was written for; the
subprocess is for end-users in a shell, not for code that already has
the library imported.

**Alternatives.**
- Subprocess to `jupyter kernelspec list`: rejected. Parses the
  printable output, slower, less reliable across versions, and the
  Python API is the source of truth.
- Walk `jupyter_path` directories directly: rejected. Reinvents what
  `KernelSpecManager` already does and misses platform-specific
  locations.

**Tradeoff.** None worth flagging. The result is a small, structured
list and a per-entry `argv` plus `executable_status` derived from
`shutil.which` on the first token of `argv`.

## Decision: cell tools dispatch on file extension, not on content

**What.** `nb_outline` and friends look at the file extension (`.ipynb`
or `.py`) and pick the right engine. A `.py` file with no `# %%`
markers is rejected with `SCRIPT_INVALID` at load time.

**Why.** The two file types are different enough that the right
behavior on a `.py` is a percent script, and a regular `.py` is a
different file entirely. Inspecting the content to decide would make
the cell tools' behavior depend on a heuristic, and the user would
have to know that rule to predict the outcome. The extension is a
direct, fast, and conventional signal; the marker check is a
hard-fail for the case where the user pointed the tool at the wrong
file.

**Alternatives.**
- Always parse as percent: rejected. Treats every `.py` as a cell
  document; ordinary modules become one-cell notebooks, and the user
  loses the ability to edit them with `edit_apply`.
- Inspect content at load time: rejected. The cost is paid on every
  read, the user has to know the rule, and the result still has to
  fall back to "ordinary Python" anyway.

**Tradeoff.** A `.py` file that happens to have a `# %%` comment in
the middle of a string literal would be rejected. This is a real
false positive, but a very rare one in practice; the user can rename
the file to `.py.txt` and the issue goes away. The alternative is
content sniffing, which is worse for everyone else.

## Decision: percent script cells are linted through a virtual notebook

**What.** `script_linting.lint` builds a virtual nbformat document
containing the script's code cells (with magics neutralised), runs
Ruff on it with `--stdin-filename` set to the script's path, and
maps diagnostics back to `cell_id` and the script's 1-based cell
number. The same pattern as `notebook.linting`.

**Why.** Ruff reads `.ipynb` natively; the same path that works for
notebooks works for percent scripts if the cells are staged as a
notebook. Staging gives Ruff the same code, the same config
resolution (because the path is the script's), and the same
diagnostic shape we already know how to parse. Rolling a separate
percent-format linter would duplicate all of that.

**Alternatives.**
- Lint the whole script as a single Python file: rejected. A syntax
  error in cell 2 would block the lint of cell 3, the way Python
  normally does for a single file; the cell-level view is what
  notebooks get and what the user expects.
- Write a percent-format parser for Ruff: rejected. Ruff is a fast
  moving target; using the notebook path keeps the engine on the
  project-maintained side.

**Tradeoff.** The virtual document includes only code cells. A
diagnostic at virtual cell N maps to script cell N only if the
script's code cells line up with virtual cells 1, 2, 3 in order —
which they do, because the staging is in order and skips markdown
and raw cells. The mapping is exact as long as the script does not
change between the staging and the read.

## Decision: type checking is a separate tool, not a validation stage

**What.** `type_check` is a stand-alone MCP tool, not a pipeline stage.
It runs `pyright` or `mypy` on demand, returning the same diagnostic
shape as `validate_file`.

**Why.** Ruff's syntax + lint stage runs in tens of milliseconds on a
typical file; Pyright and mypy run in hundreds to seconds, depending
on import resolution. Putting type checking on every `edit_apply` would
break the p95 latency target (p95 < 2 s on a 2,000-line file). The
PRD's M6 decision — "Type-checking is P2, not in v1, because of
latency" — is what kept the pipeline clean, and that argument applies
just as well to an opt-in stage. An explicit tool call is the right
surface for a slow, powerful check.

**Alternatives.**
- Add `type_check: "pyright"|"mypy"` to the validation pipeline:
  rejected. A slow stage on the critical path of every edit makes the
  server feel sluggish on a project that has not configured for it.
- Background type-checking after every edit: rejected. Background work
  needs cancellation, partial results, and a polling tool — a
  substantially different product from what the agent loop wants.

**Tradeoff.** The agent must call `type_check` explicitly. The reward
is that `edit_apply` stays fast, and a user who never asks for type
checking never pays for it.

## Decision: type_check writes a sibling temp file, not stdin

**What.** The runners write the candidate text to a sibling temp file in
the same directory as the user's file, run the tool, parse the output,
and delete the temp file in a `finally` block. Ruff's `--stdin-filename`
would be cleaner, but Pyright and mypy do not support stdin + custom
filename.

**Why.** The temp file lives in the same directory as the user's file,
which is what makes the project's `pyrightconfig.json`,
`[tool.pyright]`, `mypy.ini`, `[tool.mypy]`, and `setup.cfg` settings
resolve exactly as they would for the user's file. Without that, a
project with a subdirectory `pyrightconfig.json` would get different
results from `type_check` than from the editor.

**Alternatives.**
- A hidden cache directory (e.g. `.cache/cursor-edit-mcp/`): rejected.
  Config resolution walks up the tree from the temp file's location,
  not from the project root. A project with config in a subdirectory
  (common in monorepos) would silently disagree with the editor.
- Send content on stdin with a flag to set the filename: rejected.
  Neither Pyright nor mypy supports that combination.

**Tradeoff.** A temp file exists on disk for a few hundred
milliseconds. The name is `_mcp_pyright_<stem>_<random>.py` (or the
mypy equivalent) — not a dotfile (Pyright skips hidden files silently),
and deleted in `finally` so a crash cannot leave it behind.

## Decision: Pyright is the default; mypy is the fallback

**What.** `tool: "auto"` (the default) tries Pyright first and falls
back to mypy. Explicit `tool: "pyright"` or `tool: "mypy"` overrides.

**Why.** Pyright is faster and its output is closer to what a language
server reports, which is the experience most developers are calibrating
against. On a developer machine with Pyright installed (the common
case), the agent's first answer is the most useful one. The mypy
fallback keeps the tool useful on a CI box where Pyright is absent,
or in a project that already has a `mypy.ini`.

**Alternatives.**
- Default to mypy because it is more conservative: rejected. "Conservative"
  is not a property of the tool but of how the project configures it.
- Require the user to pick every time: rejected. "auto" is the right
  default for a developer machine; the escape is one parameter away.

**Tradeoff.** A user with both installed sees Pyright by default. That
is the right default for a developer machine, and it is one parameter
to override.

## Decision: doctor patches are unified diffs, not find-and-replace rules

**What.** Each ``Finding`` carries a ``suggested_patch`` field that is
a real unified diff against the on-disk config file. ``config_apply_fix``
parses the diff back into edits and applies it.

**Why.** A unified diff is the smallest format that survives the file
changing under the doctor's feet. The diff carries the lines to remove
and the lines to add, and a parser that verifies the remove lines still
match the on-disk file before writing will reject a stale patch
without false positives. A "find and replace" format would have to
specify how to handle ambiguity (which occurrence?) and unicity (what
if the pattern matches twice?); a diff is unambiguous by construction.

**Alternatives.**
- A small JSON patch format (e.g. JSON Patch): rejected. JSON Patch
  is the right shape for JSON documents, but the doctor edits TOML
  and INI files, where line-based diffs are the natural form.
- Reuse ``edit_apply`` with a precomputed edit list: rejected. The
  doctor is run from a tool the agent may not have edit permissions
  for in this turn, and the apply step has a confirmation token to
  pass. A separate apply tool with its own error model is clearer.

**Tradeoff.** The apply tool needs a small unified-diff parser. The
parser handles single-hunk single-file diffs the doctor emits; that
is enough for the catalogue of checks the doctor runs today. A real
patch library would be larger than the doctor itself, and a
third-party dependency for one function is not worth it.

## Decision: confirmation token is SHA-256 of (id, root), not a session-bound secret

**What.** The token is ``sha256(f"{id}|{root}").hexdigest()`` — 64
hex characters, no randomness, no server state. The same finding
produces the same token across processes, machines, and time.

**Why.** A session-bound secret would need a server-side token
store, and a token store is a stateful service running on a stdio
process. The token's job is not to be unforgeable — the doctor's
suggested patch is right there in the response, and anyone with
read access to the response can copy it. The token's job is to
prevent replay: a token from yesterday's diagnosis is the wrong
token for today's diagnosis. SHA-256 of (id, root) is good enough
for that, and it has no server-side state.

**Alternatives.**
- UUID v4 stored in memory: rejected. The stdio process can be
  restarted between the doctor and the apply, and a lost in-memory
  store would force a re-run for no real safety benefit.
- A signed token (HMAC over the patch): rejected. The token is
  reported alongside the patch in the same response, so a
  signature on the patch adds no security.

**Tradeoff.** A user who replays an old token gets a stale-patch
error and is told to re-run the doctor. That is exactly the right
behaviour: a doctor's finding is "this is wrong, here is a fix",
and applying a fix to a workspace that has changed underfoot is the
exact failure mode the apply step is meant to prevent.

## Decision: doctor checks are an explicit, ordered catalogue

**What.** The doctor is a tuple of check functions, each one a small
function that takes a :class:`WorkspaceConfigReport` and returns zero
or more findings. New checks are added by appending a function to
the tuple. The runner iterates the tuple in order.

**Why.** The doctor's value is the catalogue, not the runtime. A
fixed, ordered catalogue is reviewable in one place: when a new
check is added, the diff to the catalogue is the diff to the
doctor's behaviour. A pluggable registry would invite drift: a
check that only fires in some configurations, a check that is
registered twice, a check that gets disabled in production. None
of those is worth the flexibility.

**Alternatives.**
- A discovery mechanism (checks register themselves via decorators):
  rejected. A decorator-based registry is harder to grep and
  impossible to read top-to-bottom.
- Per-tool check classes (``RuffDoctor``, ``MarkdownlintDoctor``):
  rejected. The cross-file checks (Ruff vs EditorConfig) span tools,
  and per-tool classes would force a separate "cross-cutting" home
  for them.

**Tradeoff.** Adding a check is a small change to a single file, and
the catalogue is read in order. That is a feature: a reviewer can
read the file top-to-bottom and know what the doctor does.
