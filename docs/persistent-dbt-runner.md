# Persistent dbt process and connection reuse

Status: feasibility research, **not an implemented runtime feature**. The shipped
runner still starts dbt for each query. The offline probe below is not a daemon.

## Recommendation

Target both a warm Python/dbt process **and a live warehouse connection**, scoped
to the selected project, interpreter, profile, and target. Disk partial parsing
alone does not avoid repeated imports or connection setup. RSA key-pair auth makes
login non-interactive but does not keep a connection alive.

These are separate optimizations:

| Retained state | Avoided work | Current status |
| --- | --- | --- |
| `partial_parse.msgpack` | Full project parsing when the cache is valid | Implemented |
| Python process and imports | Python/dbt/adapter module cold starts | Locally demonstrated; not shipped |
| Warehouse connection/session | Repeated connection and authentication setup | Not implemented or benchmarked |
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

## What dbt supports, and what remains to design

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

To retain an actual connection, the next experiment must investigate an owned,
long-lived dbt adapter/runtime lifecycle, or dbt compilation followed by execution
on an owned connection. Neither has been validated here. Simply suppressing
cleanup globally is not a sound implementation: it risks stale configuration,
leaked resources, and incorrect credentials or targets. Direct connector execution
also has to preserve adapter SQL semantics, Jinja/ref behavior, preview limits,
result types, and error handling rather than become a Snowflake-only shortcut. [6]

## Proposed implementation shape

Keep the current `QueryRunner` interface: request, cancellation event, progress
callback, and `QueryResult`. A persistent adapter at that seam should own:

1. **A lazily started project-local process.** Use the interpreter/environment of
   the resolved dbt executable, not the uv tool's environment. Custom executable
   wrappers need explicit support or a clear fallback to the existing CLI runner.
   Workers continue using normal project target/log paths, not isolated artifacts.
2. **One active query per worker.** Serialize requests, with bounded waiting or an
   explicit busy response. Never let different requests mutate dbt globals in
   parallel. Separate project/profile/target/credential contexts must not share a
   live connection accidentally.
3. **An owned connection lifecycle.** Retain a healthy session for an idle period
   (for example, ten minutes), close it on expiry, and reconnect when necessary.
   Do not silently retry a query after an uncertain disconnect: it may already
   have executed, and trusted dbt macros can have side effects.
4. **Restartable cancellation and failures.** Preserve the independent UI deadline.
   Terminate/recycle a wedged worker rather than attempting unsafe Python-thread
   interruption. Treat cleanup failures as grounds to replace the worker. Local
   process termination still does not guarantee warehouse cancellation.
5. **Fresh configuration and bounded resources.** Restart on relevant environment,
   profile, `.env`, interpreter, or adapter changes. Revalidate project state using
   dbt's normal parsing rules. Retain the row/output limits, artifact invocation-ID
   checks, safe diagnostics, and idle shutdown.
6. **Private local transport.** Restrict socket/directory permissions and verify
   worker ownership. Do not put credentials in process arguments, logs, or public
   worker identifiers. Define client-disconnect, stale-socket, and crash recovery
   behavior before making the worker the default.

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
