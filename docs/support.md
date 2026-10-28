# Support and certification policy

## Current status: no certifications

The packaged support matrix contains **zero certification rows**. Therefore no
current OS, architecture, Python version, dbt Core patch, adapter patch, backend,
or dbt-enhance build is certified. Internal implementation and local tests are
engineering evidence, not certification.

Certification goals are:

- platforms: macOS, Linux, and WSL2;
- adapters: DuckDB and Snowflake;
- Snowflake evidence starts with non-interactive authentication;
- two stable dbt Core minor lines, currently 1.10 and 1.12.

Stable `dbt-core` 1.10.x and 1.12.x with stable DuckDB or Snowflake adapter metadata
are technically admitted to native worker preflight. Admission is not a statement
that every patch has been tested or certified. Other Core minor lines,
prereleases, suffixed builds, unsupported adapters, or missing required private
APIs are rejected by native worker mode.

A future certification row must identify one exact platform category,
architecture, OS release, native Python, dbt Core patch, adapter patch, backend,
dbt-enhance release, release-owned automation evidence, and real-environment
evidence. Installed versions never manufacture a row. The matrix is packaged,
local, immutable at runtime, and never updates from the network.

## Backend policy

| Backend | Meaning |
| --- | --- |
| `worker` | Native persistent project process/session; provisional when technically admitted, unsupported otherwise |
| `process` | Isolated public `dbt show` process; best-effort, including supported escape hatches for unguarded wrappers |
| `auto` | Opt-in evidence-gated selection; chooses worker only for an exact certified context |

The legacy built-in default remains `worker`. It is retained for compatibility,
not because worker is certified. Changing the default to the certified policy is
blocked until **both** conditions are recorded in release metadata:

1. an ordinary tagged release has carried the change notice and at least two
   subsequent tagged releases are recorded; and
2. at least 30 days have elapsed since that ordinary-change notice.

Neither the required tagged-release sequence nor the 30-day notice gate has
occurred. No promotion release or certified-default transition has happened for
this Unreleased work. Time passing alone cannot change the default and there is no
update check.

`--backend auto` is available now as an explicit opt-in. If no exact certified
worker default exists, the TUI presents a one-use backend choice before compilation
or execution. Machine-readable `--json` cannot ask interactively and exits with
`backend_choice_required` before execution. There is no silent backend fallback,
query replay, or automatic retry after SQL may have run.

For an admitted but uncertified native context, the TUI may offer provisional
worker and best-effort process. If the native context cannot be proven, auto can
offer only the public process path, and only when no target allowlist requires
proof. Explicit `worker` still enforces native admission. Explicit unguarded
`process` can run supported public dbt wrappers with the context labeled
unverified.

## Worker operations

One worker is scoped to a canonical project/context and serializes queries. Its
idle lifetime defaults to 600 seconds and is configurable to a positive value no
greater than 86,400 seconds. Closing a completed preview does not close the idle
session.

```sh
dbt-enhance worker list
dbt-enhance worker status --file /absolute/project/models/model.sql
dbt-enhance worker disconnect --file /absolute/project/models/model.sql
dbt-enhance worker reset --file /absolute/project/models/model.sql
dbt-enhance worker reset --file /absolute/project/models/model.sql --allow-active
```

`disconnect` is idle-only. `reset` requires explicit `--allow-active` confirmation
to interrupt active work. The TUI's `Ctrl+P` recovery action controls this same
project-wide worker, not only the current preview or target. Its confirmation opens
with Cancel focused, never replays SQL, and an idle reset refuses rather than
interrupting work that became active after inspection. Waiters use bounded,
cancellation-aware queueing; cancelling a waiter does not interrupt the query that
currently owns the worker. The older `dbt-enhance stop --file ...` command remains
an explicit active stop.

A timeout or cancellation requests local/adapter cancellation and retires unsafe
state, but `requested_unverified` means exactly that: a request was made, not that
the warehouse stopped. Configure a server-side statement timeout and check remote
query state when it matters.

## Evidence still required

External handoffs remain open for live Snowflake, WSL2, and remote cancellation
behavior. Current local process/worker and headless-viewer measurements are in the
[validation record](validation.md); representative performance budgets still await
broader environments and approval. Historical measurements are preserved in
[persistent runner evidence](persistent-dbt-runner.md) with their limitations.
No hard latency promise is supported yet. Practitioner observation target: 3–5
people; none is recorded as completed in this roadmap. Local checks and measurements
do not replace the missing external evidence.
