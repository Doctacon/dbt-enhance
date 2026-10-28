# Roadmap

Status: **Unreleased**. This page describes implementation in the current checkout,
not a tagged release, publication, support certification, or completed external
validation.

## 1. Trust

Implemented interfaces in the checkout:

- strict user-local settings and non-bypassable project/target-name allowlists;
- native target preflight before guarded execution, with target pinning where
  proven and blocked guarded execution where proof is unavailable;
- explicit backend/support labels and no silent fallback or replay;
- bounded metadata-only diagnostics with explicit report and purge;
- preview-first lifecycle operations with immutable provenance and compatibility
  validation;
- local-only error text and strict shareable-report metadata.

Still required before trust claims expand:

- live non-interactive Snowflake validation;
- WSL2 handoff evidence;
- remote cancellation observations (local cancellation is not proof);
- release-owned evidence records before any support-matrix certification row.

## 2. Adoption and safety

Implemented interfaces in the checkout:

- short local/tagged-Git setup paths, explicit `--helix-shell-bridge` consent, and
  a configurable Helix shortcut in both modes;
- actual Helix 25.07+ version verification plus checked prerequisites without
  automatic dependency installation;
- bridge format/capability `2`, private `helix-shell-bridge-v2`, and a V3 leading
  parse-error marker that keeps the matching command opaque and reserved stale or
  legacy transports fail closed under a strict supported-shell argument whitelist;
- owned bridge restoration/removal checks, refusal of modified or ambiguous state,
  exact bridge cleanup when config disappeared, preservation of later edits, and
  explicit reload/restart guidance for open Helix instances;
- legacy worker-default compatibility plus opt-in `--backend auto` one-use choice;
- configurable bounded worker idle lifetime, inventory, idle disconnect, and
  confirmed active reset;
- isolated candidate settings-schema/version/bridge-capability validation before
  setup installation/publication, installed-version verification with honest
  partial-install reporting, retained-capability gates for update/rollback, and
  owned-effect removal;
- documented Snowflake non-interactive-authentication preference and dbt security
  boundaries.

Policy gates:

- the legacy default cannot move to the certified policy until an ordinary tagged
  release carries the change notice, two subsequent tagged releases are recorded,
  and at least 30 days have elapsed since that notice;
- neither the required tagged releases nor the 30-day ordinary-change notice gate
  has occurred;
- releases should be small and evidence-gated rather than bundling unsupported
  certification claims.

Adoption evidence target: observations from 3–5 practitioners. None is recorded as
completed for this roadmap.

## 3. Inspection

Implemented interfaces in the checkout:

- target/backend/support/session context labels;
- a complete keyboard-browsable `Ctrl+P` `OptionList` (arrows, Tab, paging,
  Home/End, Enter), discoverable recovery controls without guessed search text, and
  usable 50×24/80×24 layouts;
- literal full-source focused table/SQL/cell find with overlapping matches, Unicode
  source offsets, SQL tab coordinates, rendered wrapped-line scrolling, and
  `n`/`N` navigation;
- interactive ascending/descending/clear sort, typed AND filters and NULL
  predicates, column hide/restore/sizing, real pinned prefixes, and resettable
  ephemeral preview state without arbitrary reorder;
- source row/column identity and full original cell/SQL sources retained through
  transforms, wrapping, truncation, and duplicate names for exact copies;
- width-safe pinning: oversized prefixes are refused without losing a valid prior
  pin, while terminal shrink or changed widths can unpin to keep columns reachable;
- semantic explicit light/dark palettes plus terminal-hint `auto` selection;
- separate local native-error review that cannot copy via `y`/`Y`, and reviewed
  strict metadata reports that copy the exact displayed snapshot;
- confirmed explicit project-wide worker reset/disconnect with Cancel as the
  default, no rerun/replay, and refusal to interrupt work that became active after
  an idle check;
- actual compiled/executed SQL copy, metadata-only diagnostic reports, and explicit
  provenance inspection.

Representative performance budgets remain pending broader environments and
approval. Current local process/worker and headless-viewer measurements are in the
[validation record](validation.md), alongside final repository counts and explicit
limitations. Historical measurements in [persistent runner evidence](persistent-dbt-runner.md)
remain design evidence, not current latency promises or remote-warehouse budgets.

## Explicit non-goals for this roadmap

No export, query history, AI feature, additional editor, hosted service, shared
repository settings, automatic rerun, target remapping, credential manager, or new
dbt log-retention system is included.
