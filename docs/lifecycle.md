# Installation lifecycle

All lifecycle changes are explicit and preview-first. There are no silent update
checks, background downloads, calendar-triggered promotion, or implicit `latest`
targets.

## Setup

```sh
# Local checkout
uvx --from . dbt-enhance setup --helix-shell-bridge

# Official distribution after an actual release tag exists
uvx --from 'git+https://github.com/Doctacon/dbt-enhance.git@<PUBLISHED-TAG>' \
  dbt-enhance setup --helix-shell-bridge

# Optional shortcut and Helix config path
dbt-enhance setup --helix-shell-bridge --shortcut space.x \
  --config /absolute/path/to/helix/config.toml
```

Setup defaults to `space.x` and installs matching mappings in Helix normal and
select modes. It locates `hx` and runs `hx --version`; setup stops unless the actual
reported version is 25.07 or newer. It also checks `uv`, tmux 3.3+, and user
settings, but never installs missing dependencies. Before tool installation or any
Helix config/bridge publication, setup runs the selected candidate through isolated
`uvx` to validate the current settings against candidate schema `1`, obtain and
verify the candidate version, and require Helix bridge format/capability `2`. After
`uv tool install`, it checks the persistent launcher's reported version against the
validated candidate before publishing the integration. If this installed-version
check fails, setup says that a partial installation exists and leaves Helix
unchanged; it does not claim or attempt an automatic rollback.

First setup requires `--helix-shell-bridge` because it changes Helix's
`editor.shell`; the actual editor must be Helix 25.07 or newer. Re-running setup
with an unchanged, selected owned bridge does not require the flag. If
`editor.shell` was changed afterward, explicit `--helix-shell-bridge` consent is
required again to adopt the current supported shell.

Bridge format/capability `2` is the private executable
`helix-shell-bridge-v2`. Its mapping supplies this V3 command shape (with the real
64-hex fingerprint and raw buffer name substituted):

```text
) # DBT_ENHANCE_BUFFER_V3:<fingerprint> %{buffer_name}
```

The unmatched leading `)` is a deliberate **noninteractive shell parse-error
fallback** for an old or stale editor that still has the mapping but not the active
bridge. It is not implemented with `exec false` or `exit`. The active bridge treats
the matching command as one opaque argument and does not evaluate the buffer name.
Stale/malformed `DBT_ENHANCE_BUFFER_*` commands and recognizable legacy heredoc
transports fail closed instead of being delegated.

The recorded prior shell has a strict argument whitelist. The plain form is exactly
a recognized `sh`, `bash`, `dash`, `ash`, `ksh`, `mksh`, or `zsh` executable plus
final `-c`, with no other arguments. Additionally, Bash accepts exactly one of
`--noprofile`, `--norc`, or `--posix`, or
exactly `--noprofile --norc`, before final `-c`; zsh accepts exactly `-f` before
final `-c`. Other flags, combined spellings, reordered flags, wrappers, and Fish are
rejected. An explicitly pathed executable must exist and be executable. The actual
executable/PATH result and shell startup code remain trusted; startup code that
deliberately evaluates command text is outside the bridge guarantee.

Existing TOML formatting/comments are preserved, config changes are atomic with a
backup, symlinks are followed to their real file, and an unrelated binding is not
replaced without `--force`. The bridge is stored in dbt-enhance's private lifecycle
state and setup accepts an existing bridge only when its file type, owner, mode,
link count, metadata, and generated contents match expectations. A modified or
insecure bridge is refused rather than overwritten. These are local ownership and
tamper safeguards, not a universal shell-security claim. Matching filenames may
contain spaces, quotes, dollar signs, backticks, percent syntax, and Unicode;
empty, CR/LF-containing, and NUL-containing names are rejected.

Reload every open Helix instance with `:config-reload` or restart it after setup;
open instances can otherwise retain the old shell command or mapping.

Setup does not authenticate or claim warehouse readiness. The only optional live
smoke is explicit and requires a project file:

```sh
dbt-enhance setup --helix-shell-bridge --live-smoke \
  --file /absolute/project/models/model.sql
```

It executes literal `SELECT 1`; it does not auto-login or create credentials.

Successful setup records immutable provenance where possible: a Git commit or
local artifact SHA-256 plus version, launcher, Helix path, shortcut, and owned
modes. Registry/directory legacy provenance can remain installable for compatibility
but is not a safe rollback target. Sources with embedded URL credentials are
rejected.

## Inspect, update, and rollback

```sh
dbt-enhance provenance

# Preview only
dbt-enhance update --source \
  'git+https://github.com/Doctacon/dbt-enhance.git@<TAG-OR-COMMIT>'

# Preview again and apply
dbt-enhance update --source \
  'git+https://github.com/Doctacon/dbt-enhance.git@<TAG-OR-COMMIT>' --apply

# Preview/apply the recorded prior immutable source
dbt-enhance rollback
dbt-enhance rollback --apply
```

An update source must be an explicit Git ref/commit or a supplied local regular
artifact. `latest` and registry-name update candidates are rejected. Preview
resolves Git to one immutable commit or hashes the artifact, runs the candidate's
`config validate --json` against current user settings in isolated `uvx`, and
cross-checks that report's schema/version with a separate isolated candidate
`--version` run. If the retained Helix integration exists, the candidate must also
report bridge capability `2`; update is blocked for a legacy transport. Apply
rechecks the candidate, required retained-bridge capability, and current provenance
before replacing the uv tool. It preserves user settings and Helix mappings.

Rollback changes package code only. It preserves settings and mappings, performs
the same isolated settings/version checks and retained-bridge capability gate, and
is blocked if the prior source lacks immutable provenance, has changed, is missing,
or cannot read current settings safely.

Candidate validation for a Git source can require Git/network access and executes
the explicitly selected candidate through isolated `uvx`; preview is non-mutating,
not offline by definition.

## Worker control and removal

Inspect workers before package operations:

```sh
dbt-enhance worker list
dbt-enhance worker disconnect --file /absolute/project/models/model.sql
dbt-enhance worker reset --file /absolute/project/models/model.sql --allow-active
```

`disconnect` closes only an idle worker. Resetting active work requires explicit
confirmation.

```sh
# Preview owned effects
dbt-enhance remove

# Apply; preferences remain
dbt-enhance remove --apply

# Also remove dbt-enhance preferences and diagnostics
dbt-enhance remove --purge --apply

# Required if active workers must be interrupted
dbt-enhance remove --allow-active --apply
```

Removal excludes new worker admission while it re-inventories the project-wide
workers. It blocks on active workers without `--allow-active`, disconnects idle
workers, and resets active workers only after that explicit confirmation. No query
is replayed. A failure to clean up the owned Helix integration keeps the executable
and ownership record for a retry rather than continuing the uninstall.

Removal deletes only exact owned mappings. It restores the recorded prior
`editor.shell` (or removes the setting when it was originally absent) only while
Helix still selects the exact owned bridge, and removes only the exact unmodified
owned bridge script. If the recorded Helix config disappeared entirely, removal
can still delete that independently verifiable exact owned bridge without creating
a replacement config. Later user edits and unrelated settings are preserved. If a
recognizable reserved mapping remains but bridge metadata is missing, or
`editor.shell` still references a missing owned bridge, ownership is ambiguous and
cleanup blocks before uninstall. A modified/insecure bridge is also preserved and
reported rather than deleted. It then uninstalls the uv tool and removes lifecycle
bookkeeping. Preferences remain unless `--purge` is explicit. Removal never touches
`dbt_project.yml`, `profiles.yml`, model SQL, dbt packages, credentials, logs, or
artifacts.

Reload every open Helix instance with `:config-reload` or restart it after removal.
An already-running editor may otherwise retain the removed bridge as its active
shell command.

See the repository [validation record](validation.md) for the checks and
measurements actually run; this page does not imply a release or external
certification.
