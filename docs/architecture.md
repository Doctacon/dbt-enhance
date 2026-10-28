# Architecture

## Execution path

```text
Helix selection + saved buffer path
  → private popup handoff → tmux popup → Textual QueryApp
  → settings resolution and target safety policy
  → backend selection
      worker: project-Python supervisor → native retained dbt session
      process: public dbt show subprocess → invocation-matched run_results.json
  → QueryResult → ephemeral PreviewModel → table / actual compiled SQL / copies
  → optional metadata-only DiagnosticStore record
```

`query` uses the same TUI without tmux; `query --json` uses the same execution
contract without a screen. `demo` uses a precomputed result and does not import or
invoke dbt.

## Module responsibilities

- **`models.py`** — `QueryRequest`, `ResolvedContext`, `QueryResult`, status/error
  vocabulary, and the runner seam.
- **`settings.py`** — strict user-local TOML, typed values, precedence, canonical
  project matching, and immutable target-name policy.
- **`preflight.py` / `_project_helper.py`** — bounded native dbt configuration
  resolution in the project interpreter. The helper resolves target, adapter, and
  allowlisted versions before adapter registration, manifest parsing, compilation,
  connection, or execution. SQL and credentials are not in its protocol.
- **`support.py` / `support-matrix.json`** — strict evidence matrix, admission vs
  certification, backend choice, and explicit release-policy transition gates.
- **`dbt.py`** — discovery, executable resolution, backend dispatch, process-mode
  execution, exact artifact association, bounded output, cancellation, and the
  optional diagnostics seam.
- **`worker.py`** — private project slots, context fingerprints, authenticated IPC,
  bounded/cancellable queue wait, idle lifetime, inventory/disconnect/reset, and
  retirement metadata.
- **`_worker_helper.py` / `_wire.py`** — standalone project-Python supervisor and
  bounded JSON framing. The supervisor remains responsive when the native session
  is blocked.
- **`_session.py`** — version-gated private dbt integration: configuration/target
  guard, fresh manifest validation, native inline compilation/execution, retained
  adapter/session, rollback/cleanup, and no replay.
- **`preview.py`** — pure local search/filter/sort/size/hide/pinned-prefix transforms
  that retain source row/column identities for correct copies.
- **`actions.py`, `search.py`, `ui_dialogs.py`, `ui.py`** — keyboard-browsable
  executable actions, overlapping full-source literal find with source offsets,
  typed local transforms, width-safe pinned prefixes, one-use backend choice,
  context labels, UI-thread deadline, original-identity copy, confirmed
  project-worker reset, and separate local-error/strict-report review surfaces.
- **`diagnostics.py`** — private bounded state plus a second strict sanitizer for
  shareable reports.
- **`helix.py`, `install.py`, `lifecycle.py`** — explicit-consent owned Helix shell
  bridge, key-map merge/restoration/removal checks, persistent installation,
  immutable provenance, candidate validation, and preview/apply update, rollback,
  and removal.
- **`popup.py`** — literal filename transport, private handoff, and popup process
  lifecycle.
- **`cli.py`** — public command wiring. Imports UI/dbt only when needed.

## Safety and consistency decisions

### Local dbt remains authoritative

The tool does not bundle adapters or define credentials. Discovery begins at the
source buffer, and execution uses the project interpreter/environment. This permits
separate projects to use incompatible dbt/adapter stacks while keeping dbt global
state outside the viewer process.

### Native preflight before guarded execution

A safety allowlist names only a canonical project and exact dbt target names.
Worker mode resolves and checks it before manifest parse/compile. Process mode uses
the same native resolution where possible and pins the proven target on the public
command. If proof is unavailable, a guarded query is blocked. Only unguarded
explicit process mode can use an unverified public wrapper. This avoids pretending
that profile paths or destination details are policy fields.

### No silent fallback or replay

`auto` can select a certified worker only from an exact local matrix row. Without
one, the TUI asks before execution and JSON stops. Once execution might have begun,
failure/cancellation is never replayed on another backend. Native adapter/connector
retry behavior is separate.

### One execution supplies rows and SQL

Public process mode parses dbt JSON output and accepts only a `run_results.json`
artifact from the same invocation. Native worker mode reads rows, columns, and final
compiled code from the native result. Missing/incompatible evidence is an error,
not reconstructed SQL.

### Worker isolation and lifetime

One worker serializes one project/context. The UI waits on a private advisory lock
with a deadline; cancelling a waiter is isolated from the active owner. Fingerprints
include significant environment/profile/interpreter/package inputs. Context change,
fatal/uncertain failure, cancellation, or cleanup failure retires state. Idle expiry
is configurable but bounded. Closing a completed viewer does not itself close the
session.

Authenticated control acts on a live supervisor, never a PID copied from stale
state. `disconnect` is idle-only; active reset is explicit. Local process retirement
and `requested_unverified` cancellation are not proof of remote cancellation.

### Preview is a projection, not a new query system

The result cap is applied by dbt. Search and transforms run only over fetched rows,
are ephemeral, and preserve source identity for full-value copying. Typed operations
are capability-gated; unknown metadata never enables ordered comparisons by guess.
There is no arbitrary reorder, rerun, export, or query history.

### Diagnostics have a narrow vocabulary

Diagnostics are best-effort and cannot replace a query outcome. State is HMAC-keyed
per project, bounded to 20 failures/seven days, and pruned on next use. Persisted and
shareable forms allowlist metadata independently. SQL, results, raw errors,
environment, credentials, names, and paths are excluded. Normal dbt logs/artifacts
remain outside this system and may contain SQL.

### Lifecycle is explicit and provenance-bound

Setup records immutable provenance where possible. The Helix shell bridge requires
explicit first-use/adoption consent and records the prior supported shell for
conditional restoration. Existing bridge metadata/content and ownership properties
are checked before setup overwrites or removal deletes it. Before tool installation
or Helix publication, setup executes the candidate with isolated `uvx` to validate
current settings schema, candidate version, and required bridge capability; it then
checks that the persistent launcher reports the same version. An installed-version
mismatch is an honestly reported partial installation, not an automatic rollback.
Update/rollback perform the same settings/version checks and gate any retained
bridge on a matching capability before package replacement, then recheck at apply.
Removal excludes new worker admission while inventorying workers, removes only
exact still-owned mappings and bridge state, then removes package/state and
optionally preferences/diagnostics. An exact owned bridge can still be removed if
the recorded config disappeared; user edits are preserved, while recognizable
bindings with missing bridge metadata block ambiguous cleanup. It never edits dbt
files. Open Helix instances must reload or restart after setup/removal. No operation
checks for updates silently or resolves an implicit latest release.

## Transport and resource bounds

Setup replaces Helix's `editor.shell` only after `--helix-shell-bridge` consent.
Bridge format/capability `2` is stored at the private filename
`helix-shell-bridge-v2`. The mapping sends exactly this V3 command shape plus the
raw buffer name:

```text
) # DBT_ENHANCE_BUFFER_V3:<fingerprint> %{buffer_name}
```

The leading unmatched `)` deliberately causes a noninteractive shell parse error
when an old/stale editor invokes the mapping without the bridge. It is not an
`exec false`, `exit`, or overridable shell-function guard. On the active matching
path, the bridge forwards the single command argument as opaque data to hidden
`_helix`; the helper validates the exact shape and rejects empty names or CR/LF/NUL
before popup launch. A stale or malformed `DBT_ENHANCE_BUFFER_*` namespace and a
recognizable legacy dbt-enhance heredoc fail closed instead of reaching the prior
shell.

Delegation is allowed only for a strict recorded `editor.shell`: exactly a
recognized `sh`, `bash`, `dash`, `ash`, `ksh`, `mksh`, or `zsh` executable followed
by `-c`; Bash may instead add exactly `--noprofile`, `--norc`, `--posix`, or
`--noprofile --norc`; zsh may add exactly `-f`. An explicit executable path must be
an executable file. The executable/PATH lookup and startup code still remain
trusted. Startup customization that deliberately evaluates command text is outside
the guarantee. This keeps matching buffer names out of evaluation; spaces, quotes,
dollar signs, backticks, and Unicode remain data. Newline-containing filenames are
unsupported. Popup handoff files and worker state use private permissions. Open
editors can retain stale shell/mapping state until reload or restart.

Selected SQL is limited to 1 MiB, process output to 16 MiB, worker frames/state and
preflight responses have smaller explicit bounds, and UI deadlines are independent
of backend cleanup. A daemon query thread prevents screen shutdown from waiting
forever; OS/native-extension failure modes and remote warehouse cancellation remain
outside a local UI guarantee.

## Evidence and limitations

Current code admits stable Core 1.10.x/1.12.x plus DuckDB/Snowflake metadata for
native worker use, with private API feature checks. The packaged certification
matrix is empty, so no admitted combination is certified. Historical local DuckDB
benchmarks and regression evidence are documented in
[persistent runner evidence](persistent-dbt-runner.md); they are not live
Snowflake/WSL evidence or measurements of all current changes. External Snowflake
and WSL proof, remote-cancellation proof, observations from 3–5 practitioners, and
representative changed-code performance budgets remain pending. Repository-owned
checks and measurements, without invented totals, belong in the
[validation record](validation.md).

Security boundaries and authentication guidance live in
[troubleshooting](troubleshooting.md); policy and status live in
[support](support.md).
