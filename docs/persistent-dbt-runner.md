# Persistent dbt process and connection reuse

Status: the **persistent worker is the default in this checkout**;
`--backend process` opts out. Native lifecycle tests have passed on Core **1.10.22**,
**1.12.5**, and **1.12.6** with local DuckDB. No live Snowflake/WSL worker validation or release is
claimed. The original process-only experiment below remains separate evidence.

## Recommendation

Target both a warm Python/dbt process **and a live warehouse connection**, scoped
to the selected project, interpreter, profile, and target. Disk partial parsing
alone does not avoid repeated imports or connection setup. RSA key-pair auth makes
login non-interactive but does not keep a connection alive.

These are separate optimizations:

| Retained state | Avoided work | Current status |
| --- | --- | --- |
| `partial_parse.msgpack` | Full project parsing when the cache is valid | Implemented |
| Python process and imports | Python/dbt/adapter module cold starts | Implemented in default worker |
| Warehouse connection/session | Repeated connection and authentication setup | DuckDB retention verified; Snowflake not yet verified |
| Query results | Re-execution of selected SQL | Not proposed; each selection must execute freshly |

A loop around `dbtRunner.invoke()` demonstrates the second row, **not the third**.
Its ordinary invocation lifecycle resets adapters and cleans up connections.
Do not describe this as connection pooling or assume `client_session_keep_alive`
or `reuse_connections` will preserve connections across those invocations.

## Measured process-reuse experiment

The [reproducible probe](benchmarks/dbt_process_reuse.py) compares the current
subprocess runner with sequential `dbtRunner.invoke()` calls in one process. It
uses identical CLI arguments, JSON preview parsing, compiled-SQL artifact checks,
and an already-warm partial-parse cache. There is no injected in-memory manifest.

Local macOS, Python 3.12.7, dbt-duckdb 1.9.6, one ephemeral fixture model, three
samples per workload; numbers below are medians:

| Core | Workload | Fresh CLI process | Reused Python process |
| --- | --- | ---: | ---: |
| 1.12.6 | One row | 2.733s | 0.088s |
| 1.12.6 | 14 columns × 10,000 rows | 3.214s | 0.585s |
| 1.10.22 | One row | 2.377s | 0.068s |
| 1.10.22 | 14 columns × 10,000 rows | 2.873s | 0.571s |

Initial in-process import cost was 1.765s / 1.546s respectively; the first
invocation after importing took another 0.646s / 0.553s. Those startup costs
still occur when creating or restarting a worker; the reused-process column
measures subsequent calls.

The probe also verified different selections return different rows, an ordinary
SQL error can be followed by a successful query, and an edited ephemeral model
is picked up. These checks are not a full resource-lifecycle test. One Core
1.10.22 run emitted a non-fatal `multiprocessing.Pool.__del__` / `Bad file
descriptor` exception during pool finalization, despite successful assertions and
exit code zero. The trace shows a finalizer writing to an invalid/closed pipe file
descriptor; it does not establish exactly when finalization occurred or why. This
could be teardown ordering or a lifecycle defect; neither has been demonstrated.
No incorrect query results or resource leak were demonstrated. This experimental
warning needs characterization, not treatment as a known defect in the shipped
runner or proof that live connection reuse is unsafe.

**These are not Snowflake or WSL performance measurements.** They exclude TUI
rendering and daemon/IPC overhead and do not measure authentication, network
latency, warehouse resume, or retained Snowflake connections. The roughly constant
process overhead saved in both workloads supports pursuing the design, not a
promise that a particular remote query will finish in these times.

Reproduce using only disposable local DuckDB data and profiles:

```sh
uv sync --locked
uv run python docs/benchmarks/dbt_process_reuse.py
uv run --with 'dbt-core==1.10.22' python docs/benchmarks/dbt_process_reuse.py
```

## Native retained-session implementation and validation

`_session.py` owns a native configuration/adapter lifecycle on one OS thread.
Each request refreshes native invocation state and uses
`ManifestLoader.get_full_manifest(config, reset=True)`, inline task initialization,
`ShowRunner.compile`, and `ShowRunner.execute`. The connection context explicitly
retains the handle. It does **not** loop over `dbtRunner.invoke`, inject a stale
manifest, suppress global cleanup, or implement its own Jinja/ref resolver.
Compiled SQL and typed rows come from the native result; empty-result column names
are retained without writing JSON result/manifest artifacts. Normal partial-parse
cache and log paths remain authoritative.

Support admits stable patches in **Core 1.10.x and 1.12.x**, including **1.12.5**.
Other minor lines, prereleases, and suffixed builds are rejected. Startup still
feature-detects the required private APIs. Historical regression measurements use
Core 1.10.22 and 1.12.6 with dbt-duckdb 1.9.6; admission does not imply every patch
was individually tested. The updated session/recovery/worker suite also passes on
Core **1.12.5**. The regression suite covers:

- Native compilation/execution, changed selections, edited models/macros/sources,
  fresh invocation IDs/times, preview limits, and empty-result columns.
- The identical retained adapter handle and a surviving connection-local temporary
  table, including a series of repeated requests.
- Recognized SQL statement errors and pre-execution native compilation/parse errors
  retain the session after successful rollback and cleanup. Failed manifests are
  discarded and reparsed; cold parse failures do not open a connection. Warm
  failures preserve the handle and temporary objects. Unknown/internal, connection/authentication,
  cleanup, and output-cap failures still retire it. No failed query is replayed.
- Tracked transactions roll back, explicit close is idempotent, the native
  connection is marked closed, and **another process can open the same writable
  DuckDB database after close**. Checking only an empty adapter registry was not
  sufficient: dbt-duckdb defaults to `keep_open=True`, so retirement also closes
  its owned root environment connection and clears that environment.
- End-to-end worker timeout/error recovery, changed 14-column/10,000-row previews,
  native numeric/date values, and fresh compiled SQL.

A separate stdlib supervisor enforces request deadlines and client-disconnect
cleanup independently of dbt's thread/GIL. Tests freeze the child with `SIGSTOP`
and send a large request, proving stop and timeout still work when the child cannot
read. Native IPC writes must remain nonblocking. Other offline tests cover private
socket/state permissions, authenticated control, SSO URL redaction, credential
rotation through symlinks, bounded file checks, context invalidation (including a
profile directory resolved inside dbt through `.env`), and no replay after an
uncertain execution.

The installed wheel was also exercised from a **dbt-free Python 3.12 tool
environment** against a separate **Python 3.10.20 project environment containing
dbt Core 1.12.6 but no dbt-enhance**. Native queries and the retained temporary
table passed, as did explicit stop followed by reopening the database from another
process. This verifies the packaged helpers, not just source-tree imports.

On Linux, parent-death signaling protects the session if the supervisor dies.
macOS uses a watchdog thread; abrupt supervisor death combined with a native
extension indefinitely holding the session GIL remains a limitation of that
fallback. Normal stop/timeout is handled by the separate supervisor on both OSes.
Local process death still does not establish remote warehouse cancellation.

## Transport-inclusive worker benchmark

The [worker benchmark](benchmarks/dbt_worker.py) uses the public `run_query`
backend seam, including worker startup, authenticated IPC, result decoding, and
compiled-SQL delivery. It compares identical changing selections against the
existing process backend, using a minimal disposable DuckDB project and the
normal warm partial-parse cache. The worker's temporary-table check proves a
retained connection, not merely reuse of its Python PID.

macOS arm64, Python 3.12.7, dbt-duckdb 1.9.6; three samples per workload,
medians:

| Core | Workload | Process backend | Warm worker |
| --- | --- | ---: | ---: |
| 1.12.6 | One row | 2.677s | 0.032s |
| 1.12.6 | 14 columns × 10,000 rows | 3.161s | 0.440s |
| 1.10.22 | One row | 2.310s | 0.030s |
| 1.10.22 | 14 columns × 10,000 rows | 2.804s | 0.435s |

The first worker request, with an already-warm disk cache, took **2.398s** /
**2.116s** respectively. Startup/authentication is amortized, not eliminated.
These totals exclude tool/TUI startup and rendering and are **not** predictions
for a large project, Snowflake network/authentication, or WSL. Unlike the earlier
experiment, worker mode avoids JSON result/manifest writes because it has the
native compiled result directly; this comparison measures the complete backend
change, not the isolated contribution of any one optimization.

```sh
uv run python docs/benchmarks/dbt_worker.py
uv run --with 'dbt-core==1.10.22' python docs/benchmarks/dbt_worker.py
```

## dbt lifecycle constraints

- `dbtRunner.invoke()` provides a Python entry point with CLI-style arguments.
  Multiple **concurrent** invocations in one process are unsupported in the
  tested Core versions; invocations must be serialized. [1, 2]
- Core resets invocation context/IDs and installs command-specific flags/logging.
  The adapter-management context resets adapters before invocation and cleans
  connections afterward. Task cleanup can also close connections. Reusing the
  Python interpreter is therefore not equivalent to retaining a warehouse
  session. [2–4]
- Core 1.10/1.12 accept a supplied manifest, but the manifest and rich Python
  result objects are version-sensitive. A permanently injected manifest would
  need correct invalidation for project files, macros, packages, profiles,
  variables, environment, and adapter versions. Do not bypass those checks merely
  to obtain a faster benchmark. [1, 2]
- Core 1.12.6 loads `.env` with `override=False` on import and invocation. Editing
  `.env` does not necessarily replace values already present in a long-lived
  process's environment. A worker needs explicit environment/configuration
  invalidation, not just another invocation. [5]

The implementation therefore owns the adapter/runtime lifecycle rather than
suppressing cleanup globally or replacing execution with a Snowflake-only shortcut.
Adapter-specific SQL handling remains native. Real Snowflake retained-session,
credential-rotation, and cancellation behavior still need validation. [6]

## Worker boundaries

The current `QueryRunner` interface is preserved: request, cancellation event,
progress callback, and `QueryResult`. The default worker implementation owns:

1. **A lazily started project-local process.** Use the interpreter/environment of
   the resolved dbt executable, not the uv tool's environment. Custom executable
   wrappers receive a clear instruction to select the existing process backend;
   there is no automatic fallback after SQL may have executed.
   Workers continue using normal project target/log paths, not isolated artifacts.
2. **One active query per worker.** Serialize requests, with bounded waiting or an
   explicit busy response. Never let different requests mutate dbt globals in
   parallel. Separate project/profile/target/credential contexts must not share a
   live connection accidentally.
3. **An owned connection lifecycle.** Retain a healthy session for an idle period
   (ten minutes), close it on expiry, and start fresh when necessary.
   dbt-enhance never retries a query after an uncertain disconnect: it may already
   have executed, and trusted dbt macros can have side effects. Native adapter or
   connector retry policies still apply.
4. **Restartable cancellation and failures.** Preserve the independent UI deadline.
   Terminate/recycle a wedged worker rather than attempting unsafe Python-thread
   interruption. Treat cleanup failures as grounds to replace the worker. Local
   process termination still does not guarantee warehouse cancellation.
5. **Fresh configuration and bounded resources.** Restart on relevant environment,
   profile, `.env`, interpreter, or adapter changes. Revalidate project state using
   dbt's normal parsing rules. Retain row/output limits, safe diagnostics, and idle
   shutdown. Process mode retains artifact invocation-ID checks; worker mode reads
   compiled SQL directly from its native execution result. Direct manual edits to
   installed adapter Python source require an explicit worker stop.
6. **Private local transport.** Restrict socket/directory permissions and verify
   worker ownership. Do not put credentials in process arguments, logs, or public
   worker identifiers. Client disconnects during active work retire the session;
   stale sockets/crashes can be replaced before a new execution is sent. A failure
   after sending SQL is never automatically retried.

Acceptance should include real Snowflake RSA-auth tests on WSL: separate timings
for worker startup, compilation, connection/authentication, execution/fetch, and UI
rendering; evidence that the same warehouse session is actually reused; expiry
and credential rotation; errors/cancellation; repeated requests without leaking
threads/file descriptors; and project/target switching without stale state.

## Primary evidence

1. [dbt programmatic invocations](https://docs.getdbt.com/reference/programmatic-invocations):
   CLI-style arguments, global-state/concurrency warning, and unstable rich result
   objects. Current documentation mixes v1 and v2 guidance; its v2-only statements
   about manifest injection/callbacks do not describe the tested Core versions.
2. Core 1.10.22 [`cli/main.py`](https://github.com/dbt-labs/dbt-core/blob/v1.10.22/core/dbt/cli/main.py)
   and [`cli/requires.py`](https://github.com/dbt-labs/dbt-core/blob/v1.10.22/core/dbt/cli/requires.py):
   `dbtRunner`, result/exception handling, preflight and manifest setup.
3. Installed `dbt-adapters==1.24.5`, `dbt/adapters/factory.py`:
   `adapter_management`, `reset_adapters`, and `cleanup_connections`; verified
   directly against the installed package source.
4. Core 1.10.22 [`task/runnable.py`](https://github.com/dbt-labs/dbt-core/blob/v1.10.22/core/dbt/task/runnable.py)
   and [`task/show.py`](https://github.com/dbt-labs/dbt-core/blob/v1.10.22/core/dbt/task/show.py):
   native preview execution and task/connection cleanup.
5. Installed Core 1.12.6 `dbt/cli/main.py`: module-level and per-invocation
   `load_dotenv(..., override=False)`; verified directly against installed source.
6. [dbt-snowflake connection implementation](https://github.com/dbt-labs/dbt-snowflake/blob/main/dbt/adapters/snowflake/connections.py):
   private-key configuration, connector creation, adapter SQL handling, and
   connection release deferred only until Core resets adapters.
7. [Snowflake key-pair authentication](https://docs.snowflake.com/en/user-guide/key-pair-auth)
   and [dbt secret environment variables](https://docs.getdbt.com/reference/dbt-jinja-functions/env_var#secrets):
   secure key configuration and the README's non-interactive authentication example.
