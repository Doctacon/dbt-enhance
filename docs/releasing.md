# Distribution and releases

The project already builds a standard wheel and source distribution with uv and
Hatchling. The `dbt-enhance` console script and Textual CSS are included. No dbt
adapter is a runtime dependency; DuckDB/dbt are development dependencies only.

## Share now via Git

Push the repository to a host you control, then share:

```sh
uvx --from 'git+https://github.com/YOUR-ORG/dbt-enhance.git' dbt-enhance setup
```

Prefer a tag or commit for reproducible releases:

```sh
uvx --from 'git+https://github.com/YOUR-ORG/dbt-enhance.git@v0.1.0' dbt-enhance setup
```

`setup` uses package provenance to install a persistent uv tool from the same
resolved commit. The keybinding calls its stable executable, not a temporary uvx
cache path. Re-run setup from a newer release to update; your existing matching
keybinding is retained without producing redundant backups.

## Share a wheel without a Git host

```sh
uv build
uvx --from /path/to/dbt_enhance-0.1.0-py3-none-any.whl dbt-enhance setup
```

Send the wheel from `dist/` to the recipient. uv installs its Python dependencies;
the recipient still needs Helix, tmux, and their working dbt project.

## Optional PyPI publication

No package, Git remote, tag, or release has been published automatically. Before
publishing, confirm that you control the PyPI package name `dbt-enhance`; rename
it if necessary. Add real repository URLs to `pyproject.toml`, review package
metadata, and increment the version for subsequent releases.

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
# Run only after configuring your own trusted publishing/token credentials:
uv publish
```

After successful publication under this name, the one-command installation is:

```sh
uvx dbt-enhance setup
```

Registry installs are pinned to the version that ran setup. To opt into a newer
published release, use `uvx --refresh dbt-enhance@latest setup`.
