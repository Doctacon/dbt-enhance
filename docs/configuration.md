# Configuration

## Location and scope

The only dbt-enhance preference file is user-local TOML:

```text
$XDG_CONFIG_HOME/dbt-enhance/config.toml
```

If `XDG_CONFIG_HOME` is unset, the path is
`~/.config/dbt-enhance/config.toml`. Print it without creating it:

```sh
dbt-enhance config path
```

There is no repository-level dbt-enhance config. The file never contains warehouse
credentials and does not replace `profiles.yml`. Project entries and safety entries
use canonical absolute local paths, so the settings file can reveal project paths;
do not treat it as an anonymous support report.

## Resolution order

Operational preferences resolve in this order:

```text
CLI > environment > matching [[project]] > [global] > built-in
```

Safety policy is separate and cannot be weakened by CLI flags or environment
variables. Unknown TOML keys are errors rather than ignored typos. Environment
resolution consumes only the recognized names in the table below; unrelated names,
including other `DBT_ENHANCE_*` names used by a project or development workflow, are
ignored by settings resolution and remain unchanged in the inherited environment.

Supported operational keys and built-ins:

| TOML key | Environment | Type / values | Built-in |
| --- | --- | --- | --- |
| `limit` | `DBT_ENHANCE_LIMIT` | integer 1–10,000 | `500` |
| `timeout` | `DBT_ENHANCE_TIMEOUT` | positive finite seconds | `120` |
| `target` | `DBT_ENHANCE_TARGET` | non-empty string | dbt default |
| `profiles_dir` | `DBT_ENHANCE_PROFILES_DIR` | path | dbt discovery |
| `dbt` | `DBT_ENHANCE_DBT` | executable path | project/PATH discovery |
| `backend` | `DBT_ENHANCE_BACKEND` | `worker`, `process`, `auto` | `worker` |
| `worker_idle_seconds` | `DBT_ENHANCE_WORKER_IDLE_SECONDS` | positive, max 86,400 | `600` |
| `diagnostics` | `DBT_ENHANCE_DIAGNOSTICS` | boolean | `true` |
| `theme` | `DBT_ENHANCE_THEME` | `auto`, `dark`, `light` | `auto` |

Paths in TOML must be absolute. Environment and CLI paths are expanded by the
local process. Optional top-level `schema = 1` is accepted; any other schema is
rejected. `theme = "dark"` or `"light"` is definitive. `"auto"` uses the terminal's
conventional `COLORFGBG` background hint when it is present (7–15 selects light)
and otherwise uses the dark palette; it does not claim to detect the operating
system theme.

## Typed example

```toml
schema = 1

[global]
limit = 500
timeout = 120.0
backend = "worker"
worker_idle_seconds = 600.0
diagnostics = true
theme = "auto"

[[project]]
path = "/absolute/path/to/analytics"
limit = 1000
target = "dev"
profiles_dir = "/absolute/path/to/profiles"
dbt = "/absolute/path/to/analytics/.venv/bin/dbt"

[[safety.project_targets]]
project = "/absolute/path/to/analytics"
targets = ["dev", "ci"]
```

Each `[[project]]` path must identify an existing canonical dbt project root and
may occur once. Each safety project may also occur once.

### Absent, restricted, and deny-all safety policy

These are deliberately different:

```toml
# No [[safety.project_targets]] entry for this project:
# unrestricted by dbt-enhance target policy.

[[safety.project_targets]]
project = "/absolute/path/to/analytics"
targets = ["dev", "ci"] # only these exact target names

[[safety.project_targets]]
project = "/absolute/path/to/sensitive"
targets = []             # explicit deny-all
```

An allowlist authorizes only the canonical project root plus dbt's resolved target
**name**. It does not pin a profile file, account, database, schema, role,
warehouse, destination, or credential. Repointing an allowed target name in dbt
configuration remains approved by this policy; review dbt config changes through
your normal controls.

For a guarded project, execution is blocked unless native preflight can prove the
resolved target name and it is allowed. Flags and environment variables cannot
bypass this. Native worker execution also checks before manifest parsing,
compilation, or execution. A process invocation pins the target name proved during
preflight so the same invocation cannot drift to a changed default. If native
context cannot be verified, a guarded process is blocked; only an unguarded public
process invocation may proceed as best-effort with an unverified context.

## Validate and explain

These commands are local and do not import project dbt, authenticate, connect, or
execute SQL:

```sh
# Validate the default file
dbt-enhance config validate

# Validate a selected user-settings file
dbt-enhance config validate --config /absolute/path/to/config.toml

# Show effective values, provenance, and safety policy for one project
dbt-enhance config explain --file /absolute/path/to/project/models/orders.sql

# Structured forms
dbt-enhance config validate --json
dbt-enhance config explain --file /absolute/path/to/project/models/orders.sql --json
```

`setup --config` means the **Helix** config path. `config validate --config` and
lifecycle `--config` options mean the dbt-enhance **user settings** TOML; they are
not interchangeable.
