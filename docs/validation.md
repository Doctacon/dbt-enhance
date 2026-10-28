# Local validation record

Recorded 2026-10-28 for the **unreleased working-tree implementation**, based on
`02d1c97`. Package version remains `0.1.2`; this is not evidence that a previously
published tag contains these changes. No tag, release, publication, user tool
installation, or real user editor configuration was changed.

These are local engineering observations, **not support certification**. The
packaged certification matrix remains empty and `worker` remains the default.
The ordinary default-change notice/release gates have not occurred.

## Environments and outcomes

Local host: macOS 26.6.2, arm64. Tool Python 3.12.7; Textual 1.0.0;
Helix 25.07.1 (`a05c151b`); tmux 3.5a.

| Check | Actual outcome |
| --- | --- |
| Main suite: Core 1.12.6, dbt-duckdb 1.9.6, DuckDB 1.5.6 | **411 passed, 4 skipped**, 214.68 seconds |
| Isolated earlier-Core environment: Core 1.10.22, dbt-duckdb 1.9.6, DuckDB 2.0.0, Python 3.12.7 | **26 passed, 2 skipped** across process, worker, and standalone-session integration tests |
| Offline Snowflake: Core 1.12.5, dbt-snowflake 1.10.2, connector 3.18.1 | **8 passed** using synthetic connector handles; no warehouse connection |
| Opt-in real Helix/tmux/DuckDB smoke | **2 passed** in disposable editor/server/project environments |
| Separate project Python 3.10.20: Core 1.10.22, dbt-duckdb 1.9.6, DuckDB 1.5.6 | Process, cold worker, and warm worker queries passed; project Python could **not** import dbt-enhance |
| Installed-wheel smoke outside checkout | Version/help/config-validation protocol and malformed hidden transport rejection passed; package Python/CSS/JSON bytes matched source |
| Distribution | Source distribution and wheel built; four standalone helpers passed Python 3.10 syntax checks; actual project-Python smoke also passed using the installed wheel |
| Static checks | Ruff lint, Ruff format, and `git diff --check` passed |

The main-suite skips are the separately executed Snowflake module, two opt-in
editor tests, and the Linux-only deterministic parent-death-signal test. The
older-Core skips are two dotenv tests specific to Core 1.12. Its two warnings are
upstream Click parser deprecations. These skips are not claimed as passes.
The integration groups overlap; their counts must not be added into a purported
unique-test total.

The earlier-Core environment imports the locally built wheel for public-API
integration tests. Standalone-session tests deliberately load the checkout helper
by file path. Wheel/source byte equality and the separate installed-wheel/project-
Python smoke cover packaging and standalone loading separately.

All database projects/profiles were disposable. Test settings, state, worker
runtime, clipboard adapters, and editor sessions were isolated. The Python 3.10
smoke checked native target proof and date/boolean/number values in all three
runs, plus worker value-kind metadata. Process output correctly retained unknown
column-kind metadata rather than inventing warehouse types.

## Defects caught during final validation

- Setup previously published a bridge without checking the chosen source's
  capability. It now validates candidate settings schema, version, and bridge
  format before package installation/editor publication, and checks the installed
  version before publishing. Invalid capability/schema cases and partial-install
  version mismatch have regression coverage.
- Removing an exact owned bridge now also works when the editor config has
  disappeared, without recreating that config.
- Broader older-Core testing exposed a real rollback defect, not just a version
  mismatch: dbt could suppress rollback failure and clear `transaction_open`.
  dbt-duckdb's connection wrapper lacked the rollback method expected by that
  generic path. A stronger test demonstrated uncommitted data surviving cleanup
  even on DuckDB 1.5.6; DuckDB 2.0 instead exposed an aborted transaction on reuse.
  Cleanup now invokes the native driver rollback (the existing cursor for that
  DuckDB wrapper), propagates failures, and observes native `RollbackFailed`
  events. Unsafe sessions retire without replay. Both driver environments and
  offline direct/suppressed Snowflake rollback-failure cases passed afterward.

The final suite and installed-wheel checks above were run after these repairs.

## Helix transport evidence and limits

The current transport is V3, using a leading unmatched `)` parse-error guard;
bridge metadata/capability remains format **2**. This is not the superseded
`exec false` or `exit` fallback.

- **520 disposable shell probes** covered `/bin/sh`, system/Homebrew Bash,
  `/bin/zsh`, permitted argument forms, function/alias overrides, `execfail`, and
  newline/punctuation payloads. Each new-guard case failed closed without creating
  its marker. A positive control reproduced injection through the old exec guard.
- The two real editor tests verified selected Jinja returns 42 without changing
  the buffer, custom-shell restoration, and rejection of a newline filename
  payload without a marker, popup, or query.
- Automated UI coverage includes light/dark semantic contrast, recovery modals,
  50×24 and 80×24 action discovery, width-safe pinning, original-value copying,
  full-source search, overlapping matches, Unicode offsets, tabs, and wrapping.

Shell probes do not certify every shell/version or startup configuration. The
original executable, PATH, and startup code remain trusted; startup code that
intentionally evaluates the command text is outside the guarantee. Headless UI
checks and two editor scenarios are not practitioner usability observations or a
claim about every terminal/clipboard implementation.

## Local measurements — not performance budgets

Three samples per workload on the host above. No representative-device or remote
warehouse budget has been approved. The raw, non-sensitive measurements are kept
alongside the reproducible scripts:

- [Process/worker script](benchmarks/dbt_worker.py) and
  [raw samples](benchmarks/results/2026-10-28-worker.json).
- [Headless viewer script](benchmarks/viewer.py) and
  [raw samples](benchmarks/results/2026-10-28-viewer.json).

### Explicit backend, disposable DuckDB

| Workload | Process median | Warm-worker median |
| --- | ---: | ---: |
| One row | 4.6232 s | 0.0289 s |
| 10,000 rows × 14 columns | 5.0826 s | 0.4318 s |

First worker invocation with warm on-disk cache: **2.2888 s**. Temporary-table
retention and partial-parse-cache checks passed. These timings include client IPC
and result decoding but exclude TUI rendering, popup startup, human backend
choice, remote authentication/network/execution, and ordinary user project size.
They do not predict opt-in auto-mode latency.

### Headless viewer, 100×30, dark theme, 14 numeric columns

| Rows | Initial render | Sort + repaint | Filter + repaint | Find + repaint |
| --- | ---: | ---: | ---: | ---: |
| 500 | 0.1863 s | 0.0960 s | 0.1046 s | 0.0235 s |
| 10,000 | 1.3638 s | 1.2384 s | 0.6939 s | 0.0491 s |

These medians include Textual headless event/layout settling, not real terminal
latency. Sorting/filtering are measured over the fetched data; the filter retains
half the rows. No SQL or clipboard operation occurs. The 10,000-row repaint costs
are visible trade-offs, not a claim of instant transforms. Long text, other
hardware, real terminals, and remote sessions need representative follow-up.

## Reproduce the repository checks

From an already provisioned development environment:

```sh
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
git diff --check
DBT_SEND_ANONYMOUS_USAGE_STATS=false uv run --no-sync pytest -q -rs
DBT_ENHANCE_EDITOR_SMOKE=1 DBT_SEND_ANONYMOUS_USAGE_STATS=false \
  uv run --no-sync pytest -q tests/test_editor_smoke.py
uv run --no-sync python docs/benchmarks/dbt_worker.py
uv run --no-sync python docs/benchmarks/viewer.py
```

Use disposable environments for the additional dependency combinations, rather
than replacing the project's interpreter or installing the tool into a dbt
project environment:

```sh
scratch=$(mktemp -d)
uv build --out-dir "$scratch/dist"
uv venv "$scratch/core110" --python 3.12
uv pip install --python "$scratch/core110/bin/python" \
  "$scratch/dist/dbt_enhance-0.1.2-py3-none-any.whl" \
  'dbt-core==1.10.22' 'dbt-duckdb==1.9.6' 'duckdb==2.0.0' \
  'pytest>=8,<9' 'pytest-asyncio>=0.25,<1'
DBT_SEND_ANONYMOUS_USAGE_STATS=false "$scratch/core110/bin/python" -m pytest -q \
  tests/test_integration.py tests/test_worker_integration.py tests/test_session.py
```

The existing CI includes Linux/macOS and earlier-Core/offline-Snowflake jobs;
this local record does **not** claim a remote CI run occurred.

## Outstanding external gates

- Live Snowflake non-interactive authentication and warehouse behavior.
- Real WSL2 and additional representative platform/terminal evidence.
- Remote cancellation observation; local process death is not proof.
- Observations from **3–5 practitioners**, including setup/recovery/clipboard use.
- Representative performance budgets and approval.
- Release-artifact automation plus real-environment evidence before adding any
  certification row; the separately required tagged-release/30-day notice policy
  before changing the legacy default.

No certification, release, default promotion, or completion of these external
handoffs follows from this record.
