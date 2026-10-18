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
- **`ui.py`** owns rendering, a UI-thread deadline, and a single daemon query
  thread. Progress and final outcomes arrive through thread-safe queued messages;
  a terminal-state guard rejects late deliveries. A fake runner or precomputed
  result exercises the same UI as a real query.
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
`ShowNode` does not carry the final executed SQL, so `--no-write-json` would remove
our reliable source for it. Debug SQL log messages are not used as a replacement.

**Preview-specific connection work.** The child defaults `DBT_POPULATE_CACHE=false`
without overriding an explicit environment setting. This skips eager schema cache
loading, not Jinja/ref compilation; standard `get_relation` calls fall back to
warehouse introspection on a cache miss. Custom macros may need the opt-in cache.
Anonymous usage stats and artifact-ingest uploads are explicitly disabled, without
clearing `DBT_CLOUD_*` inputs a profile might require. `--no-version-check` is not
used: it disables `require-dbt-version` enforcement, not a general PyPI check.
Optional Snowflake platform detection defaults off where the connector supports
its environment switch, preserving an explicit operator value.

**A deadline independent of cleanup.** A backend polling timeout cannot deliver
its exception while a `finally` block is stuck. Process cleanup now uses only
bounded waits and preserves the primary timeout/cancellation if cleanup fails.
The popup separately owns its deadline, displays expiry, requests cancellation,
and ignores all late results/progress/errors. A daemon query thread and non-blocking
message delivery avoid executor shutdown waiting indefinitely. Closing gives the
worker a bounded two-second grace to terminate ordinary children before process
exit; truly stuck OS/interop processes may remain. The UI watchdog cannot recover
a blocked UI event loop or operating system.

**Non-interactive SSO with safe guidance.** Browser callback authentication may
work with stdin disconnected, but paste-URL fallback cannot. Unbuffered Python
output and `read1()` make small connector prompts observable before process exit.
Only recognized fixed prompt states reach progress, never raw auth URLs. Known
SSO EOF/fallback errors and SSO timeouts point to terminal `dbt debug` and approved
credential caching. The tool does not manage credentials or alter authentication.

**Literal existing LIMIT clauses.** Lexical detection skips strings, comments,
Jinja regions and nested parentheses. A selection with a literal top-level LIMIT
is wrapped in a derived table while retaining dbt's requested preview cap and
unaltered executed-SQL artifact. Other queries keep their outer ordering unchanged.
This is not full SQL/Jinja parsing: generated clauses are not detected and final
ordering of a wrapped query is warehouse-dependent.

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
7. [dbt Core 1.10.22 CLI parameters](https://github.com/dbt-labs/dbt-core/blob/v1.10.22/core/dbt/cli/params.py):
   cache population, artifact-upload opt-out, and the purpose of version checks.
8. [Snowflake browser authentication](https://github.com/snowflakedb/snowflake-connector-python/blob/main/src/snowflake/connector/auth/webbrowser.py):
   browser callback versus `input()` redirect fallback, including blocking socket waits.
9. [Snowflake platform detection](https://github.com/snowflakedb/snowflake-connector-python/blob/main/src/snowflake/connector/platform_detection.py):
   optional cloud metadata detection and its environment opt-out (version-dependent).
10. [Snowflake federated authentication](https://docs.snowflake.com/en/user-guide/admin-security-fed-auth-use)
    and [dbt-snowflake credentials](https://github.com/dbt-labs/dbt-snowflake/blob/main/dbt/adapters/snowflake/connections.py):
    ID-token caching depends on account policy and working local storage; adapter
    versions already enable the connector's temporary-credential option.

## Validation and limits

Real isolated DuckDB integration tests run Jinja macros and ephemeral `ref()`,
check actual compiled SQL and its limit, preserve typed values, and cover empty
results, database errors, terminal semicolons, trailing comments, existing LIMIT
clauses with a smaller outer cap, and relation/column introspection on a cold cache. Both Core
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
Regression fixtures also model unreapable processes, cleanup permission errors,
hidden/EOF SSO prompts, late worker callbacks, and closing with a non-cooperative
runner. A real local child ignoring SIGTERM must be killed before the popup exits.
These reproduce repository failure modes, **not** a live Snowflake/WSL SSO session;
the exact remotely reported hang remains an environment-specific hypothesis.
Cancellation cannot guarantee remote warehouse cancellation. Neither dbt compilation nor `show` is a read-only
security boundary; trusted macros and least-privilege warehouse credentials are
still required.
