# Architecture and integration evidence

## Execution path

```text
Helix selection (stdin) + buffer path (quoted heredoc)
  → dbt-enhance popup
  → private JSON handoff → tmux display-popup → Textual QueryApp
  → background worker → discover_project(buffer)
  → project's dbt show --inline ... --output json --log-format json
  → ShowNode event (rows) + run_results.json (actual executed SQL)
  → scrollable table + expandable SQL
```

`models.py` is the small shared contract: `QueryRequest`, `QueryResult`,
`EnhanceError`, and `QueryRunner`. Editor transport, dbt execution, and UI can each
be exercised through that contract without a live editor or warehouse.

- **`dbt.py`** owns discovery, executable resolution, isolated subprocesses,
  cancellation, bounded output, and dbt protocol parsing. It imports no dbt code.
- **`popup.py`** owns the private handoff and real terminal lifecycle. The child
  runs via the same Python executable as the parent. Credentials are not passed
  through tmux `-e` command arguments.
- **`helix.py` / `install.py`** own safe keybinding generation, comment-preserving
  TOML merging, backup/atomic replacement, and durable uv tool installation.
- **`ui.py`** owns rendering and a single cancellable worker. A fake runner or
  precomputed result exercises the same UI as a real query.
- **`cli.py`** connects these modules; `query --json` exposes structured results
  without a terminal UI. `demo` never imports or invokes dbt.

## Why these decisions

**External dbt, not a bundled adapter.** Users can keep incompatible adapters and
Python versions in separate projects. Using the project's public CLI also avoids
embedding dbt's process-global state in the UI.

**One dbt execution, not compile-then-show.** dbt's `show` runner applies the
adapter's `get_show_sql` macro to compiled SQL, then executes that string. Reading
the run-results artifact from that same invocation gives the SQL actually used,
including the row limit. Recompiling separately could disagree or duplicate
macros with side effects.

**Isolated artifacts rather than a shared manifest cache.** Unique target/log
directories avoid collisions between open editors or dbt invocations, and do not
overwrite users' `target/` or `logs/`. The trade-off is fresh parsing per query.

**tmux rather than terminal takeover.** Helix's `pipe-to` captures process streams;
it is not a terminal for a TUI. tmux owns a popup PTY while Helix waits safely. A
standalone `query` command remains available outside tmux.

**Persistent installation from uvx.** Referencing uvx's temporary cache interpreter
in a permanent editor key map is brittle. Setup discovers its installation
provenance, installs the same source as a uv tool, and writes the stable tool
executable path into the mapping. Git sources retain their resolved commit.

**Literal filename transport.** Helix expansions are not shell-escaped. SQL is
never inserted into a shell command; it arrives on stdin. The buffer path is read
with `IFS= read -r` from a top-level, quoted heredoc with a fresh 128-bit delimiter.
The resulting variable is passed as a quoted argument. This handles spaces,
apostrophes, dollars, backticks, and Unicode. Newline-containing filenames are not
supported. A top-level heredoc also avoids old macOS Bash command-substitution
parsing bugs. The generated mapping assumes Helix's default POSIX shell.

## Primary sources consulted

1. [Helix command-line expansions and exceptions](https://docs.helix-editor.com/command-line.html):
   `buffer_name` is relative to the editor working directory, `pipe-to` supports
   expansions but passes shell quoting through, and selection text can be piped.
2. [Helix commands](https://docs.helix-editor.com/commands.html): `pipe-to` runs for
   each selection and ignores output. The implementation does not use `pipe`,
   which would replace the selection.
3. [dbt show reference](https://docs.getdbt.com/reference/commands/show): native
   inline compilation/execution, JSON output, and preview limits.
4. [dbt Core 1.10.22 show.py](https://github.com/dbt-labs/dbt-core/blob/v1.10.22/core/dbt/task/show.py):
   `ShowRunner.execute` replaces `compiled_code` with `get_show_sql` output before
   execution. `ShowTask.task_end_messages` emits `ShowNode.preview` as Agate JSON.
5. [dbt Core 1.10.22 compile.py](https://github.com/dbt-labs/dbt-core/blob/v1.10.22/core/dbt/task/compile.py):
   inline SQL is parsed as a synthetic `inline_query` SQL operation, not the open
   model. This motivates the documented model-context limitations.
6. [dbt Core 1.10.22 runnable.py](https://github.com/dbt-labs/dbt-core/blob/v1.10.22/core/dbt/task/runnable.py):
   run-results are written under `project_target_path` when `write_json` is enabled.
   Source review alone did not establish CLI path precedence; real tests cover it.

## Validation and limits

Real isolated DuckDB integration tests run Jinja macros and ephemeral `ref()`,
check actual compiled SQL and its limit, preserve typed values, and cover empty
results, database errors, terminal semicolons, and trailing comments. Both Core
1.10.22 and 1.12.6 have been exercised locally. Test fixtures never use user profiles.

The optional terminal test creates a private tmux server, runs a real Helix
instance with generated config, selects only one query line from a file containing
invalid unselected SQL, opens the real popup, expands SQL, and checks the original
file remains unchanged. It requires Helix/tmux on the test machine.

Empty Agate JSON previews are `[]`, so there is no column metadata to display.
The UI states this explicitly rather than guessing a schema. The dbt event and
artifact formats are validated; missing or incompatible output is an error, not
an invented success. This protocol targets dbt Core, not Fusion or the Cloud CLI.

Local process-group cancellation is tested independently. On macOS, cleanup can
receive `EPERM` from an already-exited group containing only zombies: XNU's
[`killpg1` filter and `nfound` handling](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_sig.c)
explain the behavior. A minimized local process probe and repeated real dbt runs
reproduced it. Cleanup tolerates that error only on Darwin after confirming the
leader has exited; regression tests preserve permission failures elsewhere.
Cancellation cannot guarantee remote warehouse cancellation. Neither dbt compilation nor `show` is a read-only
security boundary; trusted macros and least-privilege warehouse credentials are
still required.
