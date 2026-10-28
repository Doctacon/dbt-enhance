# dbt-enhance

A terminal viewer for dbt and Jinja queries. Browse results, inspect compiled SQL,
and sort or filter fetched rows without changing your query.
**Runs standalone; Helix and tmux are optional.** Uses your project's existing dbt
installation, profiles, and credentials.

![dbt-enhance showing query results and compiled SQL](docs/preview.svg)

## Install & run

For macOS or Linux (including WSL2). Requires Git and
[uv](https://docs.astral.sh/uv/); uv can install the required Python 3.12.

```sh
uv tool install --python 3.12 git+https://github.com/Doctacon/dbt-enhance.git@main
dbt-enhance demo
```

The demo needs no dbt project or warehouse. For real queries, use an existing
**dbt Core 1.10 or 1.12** project with its **DuckDB or Snowflake** adapter installed.
The tool stays separate from your project's Python environment.

This installs the **unreleased `main` branch**, not a tagged release or a PyPI
package. See [support notes](docs/support.md) and [tested environments](docs/validation.md).

## Use

From your dbt project, with its usual dbt environment available:

```sh
dbt-enhance query --file models/orders.sql --sql 'select 1 as ok'
```

Use one of your model paths for `--file` to locate the project; `--sql` is the query
to execute and can include Jinja such as `{{ ref('orders') }}`. Add `--json` for
structured output instead of the viewer.

- **Arrows / hjkl** move between cells; **s** shows compiled SQL; **a** opens cell detail.
- **Ctrl+P** opens actions: sort, filter, hide or pin columns, reset the view, and recovery controls.
- **/** searches the focused view; **n / N** move between matches.
- **y** copies the full cell value; **Y** copies compiled SQL; **q / Esc** closes.

Previews default to **500 rows**. Sorting and filtering work on fetched rows only
and never rerun the query.

## Helix + tmux

Requires **Helix 25.07+** and **tmux 3.3+**. Setup adds **Space-x** in normal and
select modes and changes Helix's `editor.shell` to an owned bridge, backing up an
existing config when it changes.

```sh
dbt-enhance setup --helix-shell-bridge
tmux new-session -A -s work
hx models/orders.sql
```

Reload any already-open Helix instances with `:config-reload`, or restart them.
Select a complete query, then press **Space**, then **x**. Unsaved selection edits
are included; the buffer needs a saved file path. See the
[setup guide](docs/lifecycle.md) for custom shortcuts, shell compatibility, and removal.

Use trusted projects and preferably read-only targets: dbt macros can execute SQL
during compilation, a row cap does not cap warehouse scan cost, and closing the
viewer does not guarantee remote cancellation.

[Full usage & keys](docs/usage.md) · [Configuration & target allowlists](docs/configuration.md) ·
[Updates & removal](docs/lifecycle.md) · [Troubleshooting](docs/troubleshooting.md)

[Diagnostics](docs/diagnostics.md) · [Architecture](docs/architecture.md) ·
[Validation & checks](docs/validation.md) · [Release process](docs/releasing.md)
