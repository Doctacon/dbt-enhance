# Troubleshooting and security boundaries

Start with a non-executing readiness check:

```sh
dbt-enhance doctor --file /absolute/project/models/model.sql
```

Use `--mode query` when tmux/Helix popup readiness is irrelevant. `doctor` does not
read profiles, authenticate, or query by default. A live check is explicit:

```sh
dbt-enhance doctor --live-smoke --file /absolute/project/models/model.sql
```

## Common problems

**No popup.** Launch Helix/Yazi from inside tmux and ensure `TMUX` is present.
Reload every open Helix instance with `:config-reload` or restart it after setup,
removal, or a shell change. Check for project-local Helix config overriding the user
mapping. First setup requires explicit consent:

```sh
dbt-enhance setup --helix-shell-bridge
```

Setup requires the actual Helix 25.07+ and installs private
`helix-shell-bridge-v2` (format/capability `2`) as `editor.shell`. The mapping's V3
transport begins with the deliberately unmatched
`) # DBT_ENHANCE_BUFFER_V3:<fingerprint> %{buffer_name}`. That is a
noninteractive-shell parse-error fallback for stale editor state, not `exec false`
or `exit`. Reload or restart **every** open editor after setup, removal, or a shell
change.

The recorded prior command's plain form must be exactly a recognized shell
executable plus final `-c`, with no other arguments. Bash also permits exactly
`--noprofile`, `--norc`, `--posix`, or
`--noprofile --norc`; zsh permits exactly `-f`. Other flags, wrappers, and Fish are
unsupported, although the login shell may be Fish. The selected executable/PATH and
startup code remain trusted; startup code that deliberately evaluates command text
is outside the bridge guarantee. Matching bridge commands are opaque data, while
stale/malformed reserved namespaces and recognizable legacy heredocs are rejected
instead of delegated. If setup reports a modified, missing, unowned, or insecure
bridge, do not replace it by hand: restore the intended shell/configuration, inspect
the private lifecycle state, and retry setup or removal.

**Setup reports a partial installation.** Candidate schema/version/bridge checks
happen in isolated `uvx` before tool installation and Helix publication. A later
persistent installed-version mismatch leaves Helix unchanged but can leave the uv
tool installed. Inspect that partial installation and retry explicitly; no automatic
rollback occurred.

**Removal cannot prove bridge ownership.** A missing Helix config does not by itself
prevent deletion of an independently verified exact owned bridge, and later config
edits are preserved. Cleanup does block if a recognizable reserved mapping has
missing bridge metadata, `editor.shell` references a missing owned bridge, or the
bridge was modified/insecure. Restore enough exact owned state to disambiguate it;
do not delete metadata or mappings by guess.

**Wrong project.** Discovery begins at the saved source buffer and picks the nearest
ancestor `dbt_project.yml`, not the editor working directory. Scratch buffers and
filenames containing newlines are unsupported.

**Wrong dbt executable.** Inspect `doctor`, then use an absolute `--dbt`,
`DBT_ENHANCE_DBT`, or user setting. Native worker mode needs an ordinary Python dbt
console script. An unguarded explicit process backend is the best-effort escape
hatch for supported wrappers; a target-guarded project cannot use an unverified
wrapper.

**Config is rejected.** Run `dbt-enhance config validate`, then `config explain
--file ...`. TOML paths must be absolute, project roots must exist, and unknown TOML
keys are errors. Settings resolution reads only documented environment names;
unrecognized names, including unrelated `DBT_ENHANCE_*` variables, are ignored and
preserved for the inherited project environment. An empty target allowlist is
deny-all; an absent policy is unrestricted by dbt-enhance.

**`auto` asks for a backend.** This is expected while no exact certification exists.
The TUI choice is one-use and happens before SQL runs. JSON output cannot prompt;
choose `--backend worker` or `--backend process` explicitly after reviewing the
support status.

**Profile/dependency/Jinja error.** Use the same project dbt environment in a local
terminal:

```sh
cd /absolute/path/to/project
.venv/bin/dbt debug
.venv/bin/dbt deps       # only if your project needs packages installed
```

The tool never runs `dbt deps` automatically.

**Slow first query.** Worker startup still imports Python/dbt, parses, and connects.
Subsequent requests may reuse imports, `partial_parse.msgpack`, and a live session.
A changed profile/environment/package context restarts it. `DBT_POPULATE_CACHE=true`
can add relation discovery. Warehouse resume, authentication, execution, fetch,
and rendering remain workload-dependent.

**Worker is busy or stale.** Waiting is bounded and cancellation of a waiter does
not interrupt the active owner. Inspect with `worker status`; use `worker disconnect`
for idle state or `worker reset --allow-active` only when interruption is intended.
The TUI's `Ctrl+P` worker action controls the project-wide shared worker, shows a
confirmation with Cancel focused, does not replay SQL, and refuses an idle reset if
the worker becomes active before the operation. DuckDB workers can hold a
database-file lock until disconnected/reset or idle expiry.

**Timeout or close.** The local deadline stops the UI and requests cleanup.
`requested_unverified` is not proof that a remote query stopped. Configure a
server-side statement timeout, inspect warehouse query history through approved
operations, and reset the worker if its session state is uncertain.

**Output limit.** Lower `--limit` or select fewer columns. SQL is capped at 1 MiB
and dbt output at 16 MiB.

## Snowflake: prefer non-interactive authentication

Use an organization-approved non-interactive method, such as RSA key-pair
authentication, especially on WSL. The popup is not an interactive login terminal.
If an existing dbt target already uses key-pair auth, dbt-enhance uses it unchanged;
there is no separate connection config.

A dbt Core profile can look like this (adapt every value to organization policy):

```yaml
your_profile:
  target: dev
  outputs:
    dev:
      type: snowflake
      account: your-org-your-account
      user: YOUR_DBT_USER
      role: YOUR_READ_ONLY_ROLE
      database: ANALYTICS
      warehouse: YOUR_WAREHOUSE
      schema: YOUR_SCHEMA
      threads: 4
      private_key_path: "{{ env_var('DBT_SNOWFLAKE_PRIVATE_KEY_PATH') }}"
      private_key_passphrase: "{{ env_var('DBT_ENV_SECRET_SNOWFLAKE_KEY_PASSPHRASE') }}"
```

- Register the public key and rotate it under organization policy; see
  [Snowflake key-pair authentication](https://docs.snowflake.com/en/user-guide/key-pair-auth).
- Keep the private key outside repositories, encrypted where required, with
  restrictive permissions. On WSL use a path readable from inside WSL.
- Supply secrets before starting Helix/Yazi. The encrypted key must be usable
  without a popup prompt. `DBT_ENV_SECRET_` log scrubbing is not secret storage.
- Remove `authenticator: externalbrowser` from a key-pair target. dbt-enhance never
  generates/registers keys, edits profiles, or changes account policy.

Key-pair auth removes browser interaction, not startup, connection, execution, or
fetch time.

### Browser SSO fallback

`authenticator: externalbrowser` can work when the callback reaches the connector,
but dbt runs without interactive stdin: the popup cannot accept a pasted redirect
URL. Recognized SSO states show fixed guidance; login URLs/tokens are not forwarded.
On failure, use the same environment in a normal terminal:

```sh
cd /absolute/path/to/project
.venv/bin/dbt debug
.venv/bin/dbt show --inline 'select 1 as ok' --limit 1 --no-populate-cache
```

Complete approved login there, then retry only if organization-approved token
caching works. Snowflake account `ALLOW_ID_TOKEN`, connector temporary-credential
configuration, and working OS keyring/secure local storage may all be required; see
[Snowflake connection caching](https://docs.snowflake.com/en/user-guide/admin-security-fed-auth-use#using-connection-caching-to-minimize-the-number-of-prompts-for-authentication-optional).
The tool does not cache credentials itself. Never paste auth URLs or tokens into a
bug report.

## Security and data boundaries

- Use trusted projects and least-privilege/read-only targets. dbt macros may execute
  SQL during compilation; neither inline compilation nor a row cap is a sandbox.
- A row limit does not bound warehouse scan cost. Use warehouse controls and
  server-side statement timeouts.
- Target allowlists authorize project + target **name**, not the destination behind
  that name. Normal change control must protect profile/target repointing.
- The editor environment is inherited. Worker mode keeps it in process memory;
  process mode can place SQL in dbt arguments. Worker IPC and popup handoff use
  private local files/sockets, but forced crashes can leave private temporary data.
- dbt anonymous usage statistics and artifact-ingest uploads are disabled for these
  executions, but adapters/macros can make other network calls.
- Normal dbt logs/artifacts can contain SQL. dbt-enhance diagnostics do not, and do
  not control dbt retention. Avoid overlapping process-backend dbt commands that
  write the same target path; an invocation-mismatch is reported rather than showing
  another command's compiled SQL.
- Clipboard copies preserve full original values, including off-screen content,
  tabs, and newlines. Review sensitive values before copying through terminal/tmux
  clipboard paths.
