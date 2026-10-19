# Architecture and integration evidence

## Execution path

```text
Helix selection (stdin) + buffer path (quoted heredoc)
  → dbt-enhance popup
  → private JSON handoff → tmux display-popup → Textual QueryApp
  → background worker → discover_project(buffer)
  → selected backend
    process (explicit opt-out): project's dbt show --inline ... --output json --log-format json
      → ShowNode rows + ArtifactWritten path → run_results.json SQL
    worker (default): authenticated per-project Unix socket → stdlib supervisor
      → persistent native dbt session in project's Python → native rows + compiled SQL
  → scrollable table + expandable SQL
```

`models.py` is the small shared contract: `QueryRequest`, `QueryResult`,
`EnhanceError`, and `QueryRunner`. Editor transport, dbt execution, and UI can each
be exercised through that contract without a live editor or warehouse.

- **`dbt.py`** owns discovery, executable resolution, backend dispatch, shared child
  environment defaults, and the existing isolated-process implementation. It
  imports no dbt Core code into the tool interpreter.
- **`worker.py`** owns the default worker client, a project-scoped request lock,
  private state, context fingerprints, interpreter selection, deadlines, and
  authenticated status/stop. It never signals a PID read from a state file.
- **`_worker_helper.py` / `_wire.py`** form a standalone project-Python supervisor
  with bounded JSON framing. A separate session child keeps dbt and connections
  warm; the supervisor remains responsive if dbt holds its Python GIL.
- **`_session.py`** contains the version-gated native dbt integration. It owns one
  adapter on one thread, refreshes manifests through dbt's partial parser, and
  invokes native inline setup, compilation, and `ShowRunner.execute`. It is not a
  replacement Jinja/ref resolver and never runs dbt concurrently in one process.
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
Python versions in separate projects. Both backends avoid embedding dbt's
process-global state in the UI. The worker uses private Core
interfaces only inside its separate project-interpreter child; unsupported setups
keep the explicit process backend rather than silently skipping native behavior.

**One dbt execution, not compile-then-show.** dbt's `show` runner applies the
adapter's `get_show_sql` macro to compiled SQL, then executes that string. Reading
the run-results artifact from that same invocation gives the SQL actually used,
including the row limit. Recompiling separately could disagree or duplicate
macros with side effects.

**Reuse the project's normal parse cache and artifacts.** No target/log path is
overridden. dbt resolves its project settings and environment overrides, reads
`partial_parse.msgpack`, validates the cache, and reparses only when needed. This
replaces the former per-query temporary target directory, which forced a full
project parse on every selection. Normal project manifests, run results, cache,
and logs are deliberately updated and retained; there is no separate cache layer.

`ShowNode` does not carry the final executed SQL, so `--no-write-json` would remove
our reliable source for it. JSON debug logging supplies `ArtifactWritten` with
the resolved `run_results.json` path, avoiding our own dbt YAML/Jinja path resolver.
The preview, write event, and artifact metadata must share an invocation ID; a
stale or concurrently overwritten artifact is an error, never another query's SQL.
Debug SQL log messages are not used to reconstruct compiled SQL. Successful process
output is JSON-decoded once for both preview and artifact discovery, within the
existing output-size limit. Worker mode instead reads the native execution
result's final `compiled_code` and Agate table, so it can disable JSON artifact
writes without losing compiled SQL or empty-result column names. Neither backend
redirects dbt to private artifact directories. Ordinary external dbt commands
sharing the target are not serialized.

**Default worker lifetime.** The process backend is an explicit opt-out. Worker mode
requires a recognizable Python dbt console script; its absolute shebang interpreter
is used without resolving away a virtualenv symlink. A stdlib-only helper runs
under that interpreter and starts a separate native-session child. dbt-enhance
need not be installed in the project's environment. The helper removes its own
package directory from the native import path so `dbt_enhance/dbt.py` cannot
shadow the project's actual `dbt` package. Successful requests retain that child
and its connection for up to ten idle minutes. Native compilation uses
fresh request flags/invocation state and a freshly validated native manifest, not
cached query results. Relation-cache state is cleared; tracked transactions are
rolled back between requests. Connection-local temporary objects/settings can
persist, which is an intentional semantic difference from fresh CLI invocations.

The UI's runner interface is unchanged. A per-project advisory lock serializes
requests and is deadline/cancellation-aware. An opaque keyed project slot under a
private runtime directory contains a random authentication token and generation
fingerprint, not raw environment values. Configuration/environment/package changes
retire the old worker before replacement. Native preflight reports the resolved
profile path before reading it (including `.env` resolution); credential paths
follow after configuration. First-read fingerprints are retained across both
callbacks. Credential-file changes are watched;
manual edits to imported Python source require an explicit stop. Sockets and state
files are private to the OS user, and SQL is carried over bounded JSON frames.

The supervisor, not dbt's execution thread, enforces the accepted request timeout
and observes client disconnects. Recognized driver statement errors retain the
session only after successful rollback/request cleanup and an open-connection
check. DuckDB parser/binder/catalog/conversion/range/constraint errors and Snowflake
ProgrammingError SQLSTATE classes 22, 23, and 42 are recoverable. Other failures,
cancellation, malformed execution transport, or uncertain disconnect retire the
session; there is no automatic query replay. Recoverable errors carry the same
credential-file snapshots as successes, including a failed first request. The
supervisor returns to ready only after flushing the terminal error response. Stop uses authenticated IPC, not a PID file. Graceful
native cleanup is bounded by process termination. The separate UI timer remains
necessary, and local cleanup still cannot guarantee remote warehouse cancellation.

**Preview-specific connection work.** The child defaults `DBT_POPULATE_CACHE=false`
without overriding an explicit environment setting. This skips eager schema cache
loading, not the project partial-parse cache or Jinja/ref compilation; standard
`get_relation` calls fall back to
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
   run-results are written under `project_target_path` when `write_json` is enabled;
   `ArtifactWritten` reports their resolved path. Real tests cover project/environment
   path precedence, artifact invocation IDs, and reuse of the partial-parse cache.
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

`tests/test_session.py` proves native compilation/freshness, retained handles and
temporary tables, SQL-error recovery, rollback, fatal-error retirement, and physical
teardown on representative patches of the admitted Core minors (stable 1.10.x and
1.12.x, with private-API feature checks). The worker/helper suites cover private IPC, frozen-child
deadlines, context/credential invalidation, no replay, and real DuckDB worker
queries. An installed-wheel smoke also crosses a dbt-free Python 3.12 UI environment
and a separate Python 3.10 project environment with no dbt-enhance installation.

Real isolated DuckDB integration tests run Jinja macros and ephemeral `ref()`,
check actual compiled SQL and its limit, preserve typed values, and cover empty
results, database errors, terminal semicolons, trailing comments, existing LIMIT
clauses with a smaller outer cap, and relation/column introspection on a cold cache.
A repeated 14-column/10,000-row preview checks that a changed SQL selection produces
fresh rows and compiled SQL while dbt reports `PartialParsingSkipParsing`. Other
tests cover custom project/env artifact paths and rejection of stale run results.
Core 1.10.22, 1.12.5, and 1.12.6 have been exercised locally. Test fixtures never use
user profiles and clear inherited artifact-path overrides before local testing.

The optional terminal test creates a private tmux server, runs a real Helix
instance with generated config, selects only one query line from a file containing
invalid unselected SQL, opens the real popup, expands SQL, and checks the original
file remains unchanged. It requires Helix/tmux on the test machine.

In process mode, empty Agate JSON previews are `[]`, so column metadata is lost;
the UI states this explicitly rather than guessing a schema. Worker mode reads
column names directly from the native Agate table even when its rows are empty. The dbt event and
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
Those local fixtures do not themselves establish Snowflake/WSL behavior. The user
subsequently verified installed commit `53f15e` on WSL with an empty SSO token cache
and a forced browser callback wait: the UI showed a ticking 120-second deadline,
then timeout at 120.0 seconds with SSO guidance; the dbt child was gone at the next
sample. The original five-minute hang occurred on `e28e588` and is considered fixed
by v0.1.1. This evidence validates the existing process backend, not the new
worker's Snowflake connection lifetime or cancellation, which require separate
remote validation despite worker now being the default.
Cancellation cannot guarantee remote warehouse cancellation. Neither dbt compilation nor `show` is a read-only
security boundary; trusted macros and least-privilege warehouse credentials are
still required.
