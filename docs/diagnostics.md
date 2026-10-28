# Diagnostics

Diagnostics are optional, local, bounded metadata. They are enabled by default and
can be disabled with `diagnostics = false`, `DBT_ENHANCE_DIAGNOSTICS=false`, or the
command's `--no-diagnostics` flag.

## What is retained

For each canonical project, dbt-enhance stores only:

- the latest sanitized outcome;
- at most 20 sanitized failures;
- records no older than seven days;
- allowlisted version, platform, backend/support, reason/stage, outcome, and timing
  fields.

Pruning happens on the **next diagnostics use** (record, read, report, or purge
sweep). There is no scheduler, background service, or secure-erasure promise.
Files are private user-owned state (`0700` directory, `0600` regular files) keyed by
an HMAC-derived project identifier, not a readable path.

Diagnostics do **not** store or report SQL, results, raw exception text,
environment variables, credentials, project/profile/target names, or paths. The
shareable report applies the strict allowlist again even after reading state.

The user settings file is different: it may contain absolute project, dbt, and
profiles paths. Shareable diagnostics exclude those paths, but you should still
review every report before sharing.

## Commands

```sh
# Inspect readiness and latest local metadata without connecting by default
dbt-enhance doctor --file /absolute/project/models/model.sql

# Build a strict metadata-only report
dbt-enhance diagnostics report --file /absolute/project/models/model.sql
dbt-enhance diagnostics report --file /absolute/project/models/model.sql --json

# Preview deletion, then explicitly apply it
dbt-enhance diagnostics purge --file /absolute/project/models/model.sql
dbt-enhance diagnostics purge --file /absolute/project/models/model.sql --apply
dbt-enhance diagnostics purge --all --apply
```

From the TUI, open `Ctrl+P` and browse the keyboard-operable action list to
**Review/copy support report**; no search phrase is required. Inspect the strict
metadata JSON before choosing its copy button or `y`/`Y`. Copy uses the exact
already-displayed snapshot rather than rebuilding a different report. SQL, values,
paths, and native errors are excluded. **Show local native error detail** opens a
separate local-only review; its text stays local and `y`/`Y` cannot copy from that
view. Error text is never automatically put into reports. Resetting preview view
state is unrelated to diagnostics, and purge never deletes project data or
preferences.

## dbt logs and artifacts are separate

dbt-enhance does not control dbt log/artifact retention. Normal dbt logs,
`manifest.json`, `run_results.json`, compiled files, and partial-parse data can
contain selected or compiled SQL and are written under the project's normal paths.
Keep them out of version control and manage their retention under your dbt/project
policy. A diagnostics-off setting does not disable dbt's own files.
