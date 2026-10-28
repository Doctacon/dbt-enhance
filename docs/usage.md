# Usage

## Helix and standalone flows

The flagship flow is the same TUI from Helix/tmux or a local terminal:

```sh
# Helix popup: launch Helix inside tmux, select SQL, press Space then x
tmux new-session -A -s work
hx models/orders.sql

# Standalone TUI
dbt-enhance query --file models/orders.sql --sql 'select 1 as ok'

# Existing machine-readable interface
dbt-enhance query --file models/orders.sql --sql 'select 1 as ok' --json
```

The Helix mapping runs in normal and select modes. First setup (or adoption of a
changed shell) requires explicit `--helix-shell-bridge` consent and the actual Helix
must be 25.07 or newer:

```sh
dbt-enhance setup --helix-shell-bridge
dbt-enhance setup --helix-shell-bridge --shortcut space.x
```

Setup defaults to `space.x`; `--shortcut <key.path>` selects another valid Helix
key-map path. Reload open Helix instances with `:config-reload` or restart them
after setup or removal. A source buffer must have a saved path. Select one
contiguous, complete query, including any needed CTEs, variables, and enclosing
Jinja blocks. Helix invokes `pipe-to` separately for multiple selections; they are
not joined. See [installation lifecycle](lifecycle.md) for the shell bridge.

The selection, including unsaved edits, is sent to dbt. Other project files are read
from disk. dbt-enhance does not save the buffer, run `dbt build`, install packages,
or edit a dbt profile.

## Viewer

The viewer keeps execution context quiet but visible. A completed preview identifies
the actual target when native preflight proved it; otherwise it says the target is
requested/unverified. It also shows backend, support tier, session state, adapter
when known, fetched/shown counts, cap, and elapsed time.

Fixed keys:

| Key | Action |
| --- | --- |
| Arrows / `hjkl` | Move through result cells |
| Page Up / Page Down | Scroll the focused view |
| `/` | Literal, case-insensitive full-source find in the focused table, SQL, or cell-detail view |
| `n` / `N` | Repeat the current find next / previous in that view |
| `r` | Reset ephemeral find/filter/sort/column view state |
| `s` | Expand or collapse actual compiled/executed SQL |
| `a` | Toggle full selected-cell detail |
| `y` | Copy the selected cell's original full value |
| `Y` | Copy all original compiled SQL |
| Ctrl+`p` | Search the actions menu |
| Tab / Shift-Tab | Move between visible panels |
| `q` / Escape | Close the viewer |

`Ctrl+P` opens the complete action list with an optional literal-search field.
Arrows browse it; Tab moves from search into the `OptionList`; Page Up/Page Down,
Home/End, and arrows navigate the list; Enter runs the highlighted action. Recovery
controls are therefore discoverable by browsing and do not require guessing search
text. The menu and review flows remain keyboard reachable in narrow 50×24 and
80×24 terminals.

Wired actions include find/reset; a column chooser with hide, restore, restore-all,
auto/manual width, prefix pin, and unpin; an AND-filter builder with add, remove,
clear, typed values, and NULL predicates; ascending/descending/clear sort; direct
hide and pin for the selected column; SQL/detail toggles and copies; project-worker
reset/disconnect; native-error review; support-report review/copy; and close.
Ordered filters and sorting are enabled only when returned value metadata supports
them. Unknown metadata permits only literal equals/contains and NULL predicates.
The model deliberately has no arbitrary column reordering.

Find uses complete source text rather than rendered truncation: the current
transformed visible table, all compiled SQL, or the original full focused cell.
`n`/`N` wrap through matches in the same view, including overlapping literal
matches. Source character offsets remain correct for Unicode; SQL highlighting
accounts for expanded tab coordinates; and SQL/detail panels scroll to the actual
rendered wrapped line instead of estimating from source prefixes.

Table transforms operate only on fetched preview rows. Original row and column
identities survive filtering, sorting, hiding, and duplicate column names, so detail
and copy continue to address the source cell. The full original fetched cell value
and full original compiled SQL are preserved for copies even when rendering is
truncated, wrapped, transformed, or scrolled. NULL copies as `NULL`, booleans as
lowercase `true`/`false`, and empty strings leave the clipboard unchanged with a
notice. Terminal clipboard behavior varies: local macOS uses `pbcopy`; tmux normally
uses its paste buffer/OSC 52 path; remote terminals must allow OSC 52.
`tmux save-buffer -` remains available after a tmux-routed copy.

View state is local to the current preview, never written back to SQL, and reset by
`r`; resetting or changing it does not rerun the query. Pinning fixes a visible
column prefix only while at least a minimum-width scrolling column remains
reachable. An oversized pin request is refused without losing the prior valid pin;
shrinking the terminal or changing column widths can automatically unpin a prefix
that no longer fits. Closing an idle, completed worker preview leaves the reusable
worker session open. Closing during execution requests cancellation and local
cleanup; it does not prove that remote warehouse work stopped.

The worker action applies to the project-wide shared worker, not only the current
preview or target. It first inspects whether that worker is idle or active, then
shows a confirmation with **Cancel** focused by default. Confirming an active reset
may interrupt active and queued work; an idle reset refuses if the worker becomes
active before the operation. The action is explicit and neither path reruns or
replays SQL. The local native-error view stays local and cannot copy with `y`/`Y`.
The support-report view must be reviewed first and can copy only the exact strict
metadata JSON snapshot currently displayed; it excludes SQL, values, paths, and
native error text.

## Preview semantics and limits

- Default row cap: 500; allowed range: 1–10,000.
- Default local deadline: 120 seconds.
- Selected SQL: at most 1 MiB. Captured dbt output: at most 16 MiB.
- The cap is a preview, not pagination. Exactly the cap can mean more rows exist.
- Worker mode can preserve empty-result column names. Public process output may be
  `[]`, in which case dbt supplied no empty-result schema to display.
- A final literal semicolon is removed before previewing. Select one query, not a
  multi-statement script.
- A literal top-level `LIMIT` is nested so dbt can apply the outer preview cap.
  Jinja-generated limits are not detected; final order of a wrapped query is not
  guaranteed across warehouses.

The displayed SQL comes from the same execution: native worker results in worker
mode, and the invocation-matched `run_results.json` artifact in process mode. There
is no compile-then-execute approximation.

## Project and dbt resolution

Discovery starts at the source file's directory and walks upward to the nearest
`dbt_project.yml`; it does not start at Helix's working directory. Symlinks resolve
to canonical paths. dbt executable precedence is:

1. `--dbt` or `DBT_ENHANCE_DBT`;
2. `<project>/.venv/bin/dbt`;
3. the nearest ancestor `.venv/bin/dbt`;
4. `dbt` on the editor process's `PATH`.

The command runs from the project root. dbt chooses the profile and default target,
including `DBT_PROFILES_DIR`, `DBT_TARGET`, and credentials from the inherited local
environment. Set environment variables before launching Helix/Yazi; an existing
editor cannot inherit later shell changes.

Normal target/log paths and `partial_parse.msgpack` are reused. dbt logs, manifests,
run results, and compiled files can be updated just as with `dbt show`. The child
defaults `DBT_POPULATE_CACHE=false` to avoid eager relation-cache population; set it
explicitly to `true` if trusted custom macros require preload. This does not disable
partial parsing. An explicit `SNOWFLAKE_DISABLE_PLATFORM_DETECTION` value is
preserved; otherwise optional Snowflake platform detection is disabled where the
connector supports that switch.

## dbt inline context

This is dbt's native inline-query context, not the open model's build context.
`ref()`, `source()`, macros, `var()`, and `target` come from the project. Materialized
upstream objects must exist; ephemeral refs compile into CTEs. Model-specific `this`,
`is_incremental()`, file-level config, and Jinja outside the selection are not
implicitly inherited. Use an explicit `ref()` where appropriate.

No export, query history, AI feature, new editor integration, hosted service, shared
repository config, rerun button, or target remapping is part of this interface.
