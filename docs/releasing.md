# Distribution and releases

Status: **Unreleased**. This checkout reports package version `0.1.2`. No tag,
release, publication, default-change notice, or support certification has happened
for the roadmap work described in these docs.

The project builds a standard wheel and source distribution with uv/Hatchling. The
`dbt-enhance` script, Textual CSS, and packaged support matrix are included. dbt and
warehouse adapters remain project dependencies, not runtime dependencies of the
viewer.

## Git distribution

The supported public repository is
[Doctacon/dbt-enhance](https://github.com/Doctacon/dbt-enhance). Official user
distribution is from an explicit published Git tag:

```sh
uvx --from 'git+https://github.com/Doctacon/dbt-enhance.git@<PUBLISHED-TAG>' \
  dbt-enhance setup --helix-shell-bridge
```

Do not replace the placeholder until an authorized tag actually exists. No such tag
was created for this roadmap work. `setup` resolves and records immutable Git
provenance and installs a persistent uv tool from that source. The explicit bridge
opt-in installs the stable Helix mapping and owned `editor.shell` bridge; reload or
restart every open Helix instance afterward. Re-run setup from a new explicit tag
to move an existing installation; there is no silent update check. Lifecycle
update may also target a deliberately selected full commit or local artifact, but
that does not turn an untagged commit into an official release.

A local checkout remains valid for development or private evaluation:

```sh
uvx --from . dbt-enhance setup --helix-shell-bridge
```

## Local artifact distribution

After the root release owner completes the repository's actual validation and build
steps, a wheel can be installed by explicit path:

```sh
uvx --from /absolute/path/to/dbt_enhance-0.1.2-py3-none-any.whl \
  dbt-enhance setup --helix-shell-bridge
```

Do not infer that a wheel exists merely because the package version is `0.1.2`.
Record and distribute its digest through the release process.

## No PyPI release

There is no PyPI release or publication plan. Do not document or advertise:

```text
uvx dbt-enhance setup
uvx dbt-enhance@latest setup
```

Lifecycle update intentionally rejects `latest` and registry-name candidates. It
accepts an explicit Git ref/commit or supplied local artifact, validates settings
compatibility and version before apply, and retains immutable rollback provenance.

## Release evidence checklist

For a future tagged release, the release owner should, at minimum:

1. select the exact commit and package version;
2. record the actual validation performed in the repository's
   [validation record](validation.md), without inferred counts or evidence;
3. build and inspect artifacts without changing source after validation;
4. create/push a tag and publish release artifacts only through an authorized
   release action;
5. update user install docs from `<PUBLISHED-TAG>` only after the tag exists;
6. add support-matrix certification rows only when both release-owned automation
   and real-environment evidence exist for each exact row.

A future backend-default notice must be an ordinary change in a tagged release.
Promotion remains blocked until two subsequent tagged releases are recorded **and**
at least 30 days have elapsed since that notice. Neither gate has occurred; the
calendar alone never changes runtime behavior.
