# dbt-enhance

**Select SQL in Helix. Press Space, then x. Stay in your flow.**

A tmux popup compiles your selected dbt/Jinja SQL, executes it using the nearest
parent dbt project, and shows a keyboard-friendly results table. Expand the SQL
to see the actual statement dbt executed, including its preview limit.

- Works from Helix directly or from Helix opened by Yazi.
- Runs the **selection**, including unsaved edits—not automatically the whole file.
- Finds the closest ancestor `dbt_project.yml` for every invocation.
- Uses each project's own dbt executable, adapter, profile, macros, and environment.
- Scrollable rows/columns, syntax-highlighted SQL, cell inspection, loading/error states.
- No editor plugin, daemon, separate connection manager, or bundled warehouse adapter.

![Results and expanded compiled SQL (offline demo)](docs/preview.svg)

## Install in one command

From this checkout:

```sh
uvx --from . dbt-enhance setup
```

This installs a persistent `uv tool`, then adds **Space-x** to both normal and
select mode in your Helix configuration. Existing settings and comments are
preserved, symlinks are followed without replacing them, and changed files get a
backup. An existing Space-x binding is **not** overwritten unless you pass `--force`.
Reload Helix with `:config-reload` or restart it.

### Share it with someone else

Once you push this repository to a Git host, give them one command, replacing the URL:

```sh
uvx --from 'git+https://github.com/YOUR-ORG/dbt-enhance.git' dbt-enhance setup
```

No clone is needed. Setup installs the same Git commit that uvx fetched. You can
also distribute a built wheel and use `uvx --from /path/to/dbt_enhance-0.1.0-py3-none-any.whl dbt-enhance setup`.

**This repository is not published to PyPI yet.** The shorter
`uvx dbt-enhance setup` command is only appropriate after publishing this package
under that name. See [releasing](docs/releasing.md).

### Prerequisites

- macOS or Linux, [uv](https://docs.astral.sh/uv/), **Helix 25.07+**, **tmux 3.3+**.
- Helix's default POSIX shell configuration (`sh -c`). A custom Fish shell for
  Helix's `editor.shell` is not supported by the generated mapping; your login
  shell can still be Fish.
- An existing working **dbt Core** project with its warehouse adapter installed.
  Tested with Core 1.10.22 and 1.12.6 plus dbt-duckdb 1.9.6. dbt Fusion and the dbt
  Cloud CLI are not supported.
- Python 3.12+ for this tool; uv can provision Python. The project's dbt Python
  version is independent.

On macOS, install missing terminal tools with `brew install tmux helix`. Setup
never installs or changes your project's dbt/warehouse dependencies.

## Use it

Start a tmux workspace, then launch your usual workflow **inside it**:

```sh
tmux new-session -A -s work
yazi  # or: hx path/to/models/orders.sql
```

1. Open a saved-path buffer inside a dbt project.
2. Select **one complete query**, which can be only part of the file. Include any
   needed CTEs, Jinja variables, and enclosing Jinja blocks in that selection.
   Helix's `%` selects the whole buffer; `x` selects the current line.
3. Press **Space, x** (lowercase x, not Shift-X).
4. Inspect the results; close the popup to return to the unchanged selection.

| Key | Action |
| --- | --- |
| Arrows / `hjkl` | Navigate result cells, scrolling rows and columns |
| Page Up / Page Down | Scroll the focused view |
| `s` | Expand/collapse compiled SQL |
| `a` | Toggle selected-cell detail |
| Tab / Shift-Tab | Move focus between visible panels |
| `q` / Esc | Close; cancel the local dbt process if still running |

The default is **500 rows** and a **120-second local timeout**. The table is a
preview, not pagination over the whole result. A result exactly at the limit may
have more rows in the warehouse. Empty results get an explicit empty state;
dbt's JSON preview does not provide column names when there are no rows.

## How project connections work

Discovery starts at the **source buffer's directory**, walking upward to the
first `dbt_project.yml`, not at Helix's working directory. Nested or unrelated
projects therefore resolve independently. Symlinks resolve to their real paths.

Executable resolution is:

1. `--dbt` / `DBT_ENHANCE_DBT`, if set.
2. The project's `.venv/bin/dbt`.
3. The nearest ancestor `.venv/bin/dbt` (for monorepos).
4. `dbt` on the editor's `PATH`.

The command runs from that project's root. dbt itself chooses its profile and
default target, including `DBT_PROFILES_DIR`, `DBT_TARGET`, and environment-based
credentials. The popup receives the editor's current environment, not the tmux
server's possibly stale environment. Set environment variables **before starting
Helix/Yazi**; an already-running editor cannot inherit later shell changes.

Optional overrides work for both `query` and the shortcut:

| CLI option | Environment variable | Default |
| --- | --- | --- |
| `--limit` | `DBT_ENHANCE_LIMIT` | 500, maximum 10,000 |
| `--timeout` | `DBT_ENHANCE_TIMEOUT` | 120 seconds |
| `--target` | `DBT_ENHANCE_TARGET` | dbt's default |
| `--profiles-dir` | `DBT_ENHANCE_PROFILES_DIR` | dbt's normal discovery |
| `--dbt` | `DBT_ENHANCE_DBT` | executable discovery above |

For example: `DBT_ENHANCE_TARGET=dev DBT_ENHANCE_LIMIT=1000 hx models/orders.sql`.

## Try it without a warehouse

The UI demo needs neither dbt nor tmux:

```sh
uvx --from . dbt-enhance demo
```

This repository also includes a real, self-contained DuckDB project. No database
credentials or persistent tables are needed; its `ref()` resolves an ephemeral model:

```sh
uv sync
uv run dbt-enhance query --file examples/duckdb/models/order_summary.sql \
  < examples/duckdb/models/order_summary.sql
```

For machine-readable output, add `--json`. To run a literal query:

```sh
uv run dbt-enhance query --file examples/duckdb/models/order_summary.sql \
  --sql 'select {{ 6 * 7 }} as answer;' --json
```

`query` opens the same interface in the current terminal without tmux. Use
`dbt-enhance doctor --file path/to/model.sql` to check discovery and prerequisites
without connecting to a database or reading credentials.

## Important boundaries

- This is dbt's native **inline query** context, not a model build. `ref()`,
  `source()`, macros, `var()`, and `target` come from the project. Materialized
  upstream refs must already exist; ephemeral refs compile into CTEs.
- Model-specific `this`, `is_incremental()`, file-level configuration, and Jinja
  variables outside the selection do **not** automatically inherit the current
  model's runtime context. Use an explicit `ref('model_name')` when appropriate.
- It does not save your editor buffer, run `dbt build`, install packages, or change
  profiles. Other project files are read from disk. Run `dbt deps` yourself if needed.
- A final SQL semicolon is removed before previewing (including before trailing
  comments). Semicolons *generated by Jinja* should be omitted by your template.
  Select one query, not a multi-statement script.
- Use one contiguous selection. Helix invokes `pipe-to` separately for each
  selection; multiple selections do not become one query. Scratch buffers need a
  saved path. Filenames containing newlines are unsupported.
- **Use trusted projects and a read-only dbt target.** This is not a SQL sandbox:
  dbt macros can execute SQL during compilation. A row limit does not limit
  warehouse scan cost. Closing the popup kills local processes, but warehouse
  cancellation is adapter-dependent; configure server-side statement timeouts.
- Each execution disables dbt anonymous usage statistics and uses private temporary
  dbt target/log directories, leaving your usual artifacts alone. The popup handoff temporarily contains SQL and the
  editor environment in a `0600` file under a `0700` directory; the child consumes
  it immediately. Normal exits clean remaining temporary files. Forced crashes
  can leave private temporary directories. SQL also appears in dbt's process
  arguments; follow your OS's process-inspection security policy.
- Selected SQL is limited to 1 MiB (the OS may impose a smaller argument limit).
  dbt output is capped at 16 MiB; lower `--limit` or select fewer columns if exceeded.
  Cell detail previews show up to 8,000 characters; SQL itself is not truncated.

## Setup, troubleshooting, and removal

```sh
# Use a different config file, or explicitly override a conflicting shortcut:
uvx --from . dbt-enhance setup --config /path/to/config.toml
uvx --from . dbt-enhance setup --force
```

- **No popup:** ensure Helix/Yazi was launched inside tmux, not outside it.
- **Wrong dbt executable:** check `doctor --file ...`, then set `DBT_ENHANCE_DBT`
  to an absolute executable path. Poetry/Conda environments can use this override.
- **Profile/dependency/Jinja error:** the popup displays dbt's error. Check the
  same project's `dbt debug`, `dbt deps`, and selected SQL using its own dbt environment.
- **Slow first query:** each preview starts a fresh dbt process and parses the
  project in isolation. This favors correctness across projects over warm-cache speed.
- **Shortcut still missing:** reload Helix; check for a project-local Helix config
  that overrides your user key map.

To remove: delete only the two `space.x` mappings in the config (or restore the
printed backup), then run `uv tool uninstall dbt-enhance`.

## Development

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
# Optional real terminal smoke test; private tmux server and isolated profiles:
DBT_ENHANCE_EDITOR_SMOKE=1 uv run pytest tests/test_editor_smoke.py -v
```

Tests include actual DuckDB execution, cancellation, project discovery, safe
filename transport, private popup handoff, config preservation, and headless UI
interaction. See [architecture and source evidence](docs/architecture.md).
