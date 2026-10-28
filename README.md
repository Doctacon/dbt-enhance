# dbt-enhance

Run selected dbt/Jinja SQL from Helix in a keyboard-first terminal viewer. dbt-enhance
uses the dbt installation, project, profile, target, credentials, and environment that
already work in your local terminal; it does not provide a hosted service or separate
connection manager.

> **Release status:** this checkout reports package version `0.1.2`, but the roadmap
> work documented here is **Unreleased**. No tag, release, publication, or support
> certification is implied.

## Evaluate the unreleased checkout

Prerequisites: Python 3.12+ (uv can provision it),
[uv](https://docs.astral.sh/uv/), Helix 25.07+, tmux 3.3+, and an existing dbt Core
project with its adapter installed.

```sh
git clone https://github.com/Doctacon/dbt-enhance.git
cd dbt-enhance
uvx --from . dbt-enhance setup --helix-shell-bridge
```

This local-checkout path is for development or private evaluation. Official
releases are distributed from an explicit published Git tag, but no tag exists for
this roadmap work yet:

```sh
uvx --from 'git+https://github.com/Doctacon/dbt-enhance.git@<PUBLISHED-TAG>' \
  dbt-enhance setup --helix-shell-bridge
```

Setup verifies the actual Helix is 25.07 or newer and checks the other
prerequisites, but never installs Helix, tmux, dbt, adapters, packages, or warehouse
credentials. Before installing the tool or publishing Helix changes, isolated
`uvx` runs validate the selected candidate's settings schema, version, and required
Helix bridge capability. Setup then verifies the persistent installed version; a
failure at that point is reported honestly as a partial installation, without an
automatic-rollback claim.

The explicit `--helix-shell-bridge` opt-in installs the private
`helix-shell-bridge-v2`, sets Helix's `editor.shell` to it, and adds **Space-x** in
normal and select modes. Its format/capability is `2`; its V3 transport begins with
a deliberately unmatched `)`, so unsupported or stale editors get a
noninteractive-shell parse error rather than an `exec false`/`exit` fallback.
Reload every open Helix instance with `:config-reload` or restart it after setup.
See [installation lifecycle](docs/lifecycle.md) for the exact shell whitelist,
trust boundary, fail-closed behavior, ownership, restoration, and removal rules.

There is no PyPI release; do not use `uvx dbt-enhance setup` as an install
instruction. See [releasing](docs/releasing.md).

## Run a preview

```sh
tmux new-session -A -s work
hx path/to/dbt/project/models/orders.sql
```

1. Select one complete query. Helix `%` selects the buffer; `x` selects a line.
2. Press **Space**, then **x**.
3. Inspect the result and compiled SQL. Press `Ctrl+P`, then browse the action list
   with arrows, Tab, Page Down, or End and run an item with Enter; recovery actions
   are listed without requiring guessed search text. Press `q` or Escape to close.

The default preview cap is 500 rows (maximum 10,000), the local timeout is 120
seconds, and selected SQL/output are bounded to 1 MiB/16 MiB. For a standalone TUI
or structured output:

```sh
dbt-enhance query --file models/orders.sql --sql 'select 1 as ok'
dbt-enhance query --file models/orders.sql --sql 'select 1 as ok' --json
```

Explore the interface without dbt or a warehouse:

```sh
dbt-enhance demo
```

No current environment is certified. The legacy `worker` default remains in place;
`--backend auto` opts into the evidence-gated selection policy and may require a
one-use choice before any SQL runs. See [support policy](docs/support.md).

## Guides and reference

- [Usage and viewer keys](docs/usage.md)
- [User-local configuration and target allowlists](docs/configuration.md)
- [Support matrix, backends, and certification policy](docs/support.md)
- [Troubleshooting, Snowflake authentication, and security boundaries](docs/troubleshooting.md)
- [Diagnostics and shareable reports](docs/diagnostics.md)
- [Setup, updates, rollback, and removal](docs/lifecycle.md)
- [Architecture](docs/architecture.md) and [persistent runner evidence](docs/persistent-dbt-runner.md)
- [Roadmap](docs/roadmap.md), [validation record](docs/validation.md), and [release process](docs/releasing.md)

Use trusted dbt projects and least-privilege, preferably read-only targets. dbt
macros can execute SQL during compilation, a row cap does not cap warehouse scan
cost, and local cancellation is not proof of remote warehouse cancellation.
