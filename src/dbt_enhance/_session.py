"""A small, native dbt Core session used by the persistent worker.

This module is deliberately self contained. The worker loads it under a project
interpreter, where ``dbt_enhance`` itself need not be installed.  Consequently,
all dbt imports are lazy and this file only imports the Python standard library
at module import time.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import inspect
import io
import json
import os
import platform
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

_MAX_JSON_BYTES = 16 * 1024 * 1024
_SUPPORTED = "dbt-core 1.10.22 or 1.12.6"
_UNSUPPORTED_MESSAGE = (
    f"Persistent dbt sessions require {_SUPPORTED}. "
    "Install a supported dbt-core version or use the process backend."
)
_AUTHORITY_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s<>\"']+", re.IGNORECASE)


class UnsupportedSession(RuntimeError):
    """Raised when native persistent sessions are unsafe in this interpreter."""


class SessionError(RuntimeError):
    """A scrubbed error from a session that has been permanently retired."""


def _core_version() -> str:
    try:
        return importlib.metadata.version("dbt-core")
    except importlib.metadata.PackageNotFoundError:
        raise UnsupportedSession(_UNSUPPORTED_MESSAGE) from None


def _supported_version(version: str) -> bool:
    # Private APIs need execution/retention/retirement/teardown evidence, not
    # constructor inspection. New releases enter this list after that matrix passes.
    return version in {"1.10.22", "1.12.6"}


def _require_symbols() -> None:
    """Import every private dbt surface on which the implementation relies."""

    required = {
        "dbt.adapters.contracts.connection": ("ConnectionState",),
        "dbt.adapters.factory": ("get_adapter", "reset_adapters"),
        "dbt.cli.flags": ("Flags", "args_to_context"),
        "dbt.cli.requires": ("preflight", "profile", "project", "runtime_config", "setup_manifest"),
        "dbt.flags": ("set_flags",),
        "dbt.parser.manifest": ("ManifestLoader",),
        "dbt.task.show": ("ShowTask",),
        "dbt.tracking": ("initialize_from_flags",),
        "dbt_common.clients.system": ("get_env",),
        "dbt_common.context": ("set_invocation_context",),
        "dbt_common.events.contextvars": ("log_contextvars", "task_contextvars"),
        "dbt_common.events.event_manager_client": ("get_event_manager",),
        "dbt_common.exceptions": ("env_secrets", "scrub_secrets"),
        "dbt_common.invocation": ("reset_invocation_id",),
    }
    try:
        for module_name, names in required.items():
            module = importlib.import_module(module_name)
            for name in names:
                getattr(module, name)
        selected = importlib.import_module("dbt.selected_resources")
        _ = selected.set_selected_resources
        loader = importlib.import_module("dbt.parser.manifest").ManifestLoader
        _ = loader.get_full_manifest
        show_task = importlib.import_module("dbt.task.show").ShowTask
        for name in ("_runtime_initialize", "defer_to_manifest", "get_runner"):
            getattr(show_task, name)
    except (ImportError, AttributeError):
        raise UnsupportedSession(_UNSUPPORTED_MESSAGE) from None


def compatibility() -> dict[str, str]:
    """Return harmless version facts, or reject an untested dbt Core runtime.

    This check imports Python modules to feature-detect private APIs.  It does not
    locate/read a profile, initialize an adapter, or execute a query.
    """

    version = _core_version()
    if not _supported_version(version):
        raise UnsupportedSession(_UNSUPPORTED_MESSAGE)
    _require_symbols()
    return {"dbt_core": version, "python": platform.python_version()}


class _CappedTextBuffer(io.TextIOBase):
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.size = 0
        self.parts: list[str] = []

    def writable(self) -> bool:
        return True

    def write(self, value: str) -> int:
        encoded_size = len(value.encode("utf-8"))
        if self.size + encoded_size > self.maximum:
            raise SessionError(
                f"dbt preview output exceeds the {_MAX_JSON_BYTES // (1024 * 1024)} MiB limit"
            )
        self.parts.append(value)
        self.size += encoded_size
        return len(value)

    def getvalue(self) -> str:
        return "".join(self.parts)


def _scrub_error(error: BaseException | str) -> str:
    """Use dbt's secret registry and additionally remove interactive URLs."""

    text = str(error)
    try:
        exceptions = importlib.import_module("dbt_common.exceptions")
        text = str(exceptions.scrub_secrets(text, exceptions.env_secrets()))
    except Exception:
        # compatibility() gates these symbols.  Cleanup-time failures should not
        # replace the useful original error if dbt globals are already torn down.
        pass
    return _AUTHORITY_URL.sub("<redacted URL>", text)


class DbtSession:
    """Own one native dbt adapter and its thread connection until ``close``."""

    def __init__(
        self,
        context: dict[str, Any],
        on_config: Callable[[list[str]], None] | None = None,
    ) -> None:
        project_dir = context.get("project_dir")
        profiles_dir = context.get("profiles_dir")
        target = context.get("target")
        if not isinstance(project_dir, str) or not os.path.isabs(project_dir):
            raise ValueError("context.project_dir must be an absolute string")
        if profiles_dir is not None and not isinstance(profiles_dir, str):
            raise ValueError("context.profiles_dir must be a string or None")
        if target is not None and not isinstance(target, str):
            raise ValueError("context.target must be a string or None")
        if on_config is not None and not callable(on_config):
            raise TypeError("on_config must be callable or None")

        self._project_dir = project_dir
        self._profiles_dir = profiles_dir
        self._target = target
        self._on_config = on_config
        self._initialized = False
        self._closed = False
        self._thread_id: int | None = None
        self._ctx: Any = None
        self._ctx_entered = False
        self._config: Any = None
        self._adapter: Any = None
        self._manifest: Any = None
        self._warnings: list[str] = []
        self._event_callback: Callable[[Any], None] | None = None
        self._event_manager: Any = None

    def _args(self, sql: str, limit: int) -> list[str]:
        args = [
            "show",
            "--inline",
            sql,
            "--limit",
            str(limit),
            "--project-dir",
            self._project_dir,
            "--no-write-json",
            "--no-send-anonymous-usage-stats",
            "--no-upload-to-artifacts-ingest-api",
            "--no-use-colors",
            "--quiet",
        ]
        if self._profiles_dir is not None:
            args.extend(("--profiles-dir", self._profiles_dir))
        if self._target is not None:
            args.extend(("--target", self._target))
        return args

    def _capture_event(self, event: Any) -> None:
        try:
            if str(event.info.level).lower() != "warn":
                return
            message = event.info.msg
        except Exception:
            return
        self._warnings.append(_scrub_error(message))

    @staticmethod
    def _unsupported_flags(flags: Any) -> list[str]:
        bad: list[str] = []
        if getattr(flags, "POPULATE_CACHE", False):
            bad.append("POPULATE_CACHE=true (set DBT_POPULATE_CACHE=false)")
        if getattr(flags, "USE_V2_PARSER", False):
            bad.append("USE_V2_PARSER=true")
        if getattr(flags, "USE_CATALOGS_V2", False) or getattr(flags, "CATALOGS_V2", False):
            bad.append("catalogsv2=true")
        return bad

    def _new_flags(self, sql: str, limit: int) -> Any:
        flags_module = importlib.import_module("dbt.cli.flags")
        # The bootstrap Click context owns dbt's registered cleanup resources.
        # Updating its command params is the tested way to create request-local,
        # frozen Flags without nesting another dbt bootstrap lifecycle.
        self._ctx.params.update(inline=sql, limit=limit)
        flags = flags_module.Flags(self._ctx)
        self._ctx.obj["flags"] = flags
        self._config.args = flags
        importlib.import_module("dbt.flags").set_flags(flags)
        return flags

    def _reset_invocation(self, flags: Any) -> None:
        system = importlib.import_module("dbt_common.clients.system")
        common_context = importlib.import_module("dbt_common.context")
        environment = system.get_env()
        # Match preflight's legacy/new engine environment propagation when that
        # helper exists on the supported Core line.
        requires = importlib.import_module("dbt.cli.requires")
        cross_propagate = getattr(requires, "_cross_propagate_engine_env_vars", None)
        if cross_propagate is not None:
            cross_propagate(environment)
        common_context.set_invocation_context(environment)
        importlib.import_module("dbt_common.invocation").reset_invocation_id()
        importlib.import_module("dbt.tracking").initialize_from_flags(False, flags.PROFILES_DIR)

    def _credential_paths(self) -> list[str]:
        credentials = self._config.credentials
        paths: set[str] = set()
        # Snowflake's key file is the credential file currently supported by
        # the worker.  Deliberately inspect named path fields rather than
        # serializing credentials, which could disclose values.
        for field in ("private_key_path",):
            value = getattr(credentials, field, None)
            if isinstance(value, str) and value:
                # Keep the configured alias, not its current symlink target, so
                # key-file rotation through a symlink is visible to the supervisor.
                paths.add(str(Path(value).expanduser().absolute()))
        return sorted(paths)

    def _initialize(self, sql: str, limit: int) -> None:
        compatibility()
        flags_module = importlib.import_module("dbt.cli.flags")
        requires = importlib.import_module("dbt.cli.requires")

        ctx = flags_module.args_to_context(self._args(sql, limit))
        callback = self._capture_event
        ctx.obj = {"callbacks": [callback]}
        self._ctx = ctx
        self._event_callback = callback
        ctx.__enter__()
        self._ctx_entered = True
        client = importlib.import_module("dbt_common.events.event_manager_client")
        self._event_manager = client.get_event_manager()

        @requires.profile
        @requires.project
        @requires.runtime_config
        def load_config(context: Any) -> Any:
            return context.obj["runtime_config"]

        @requires.preflight
        def bootstrap(context: Any) -> Any:
            # Core may resolve this directory through .env, which the tool-side
            # environment has never loaded. Snapshot the actual profile before
            # dbt reads it, not after credentials may already have changed.
            if self._on_config is not None:
                directory = Path(context.obj["flags"].PROFILES_DIR).expanduser().absolute()
                self._on_config([str(directory / "profiles.yml")])
            return load_config(context)

        self._config = bootstrap(ctx)
        flags = self._new_flags(sql, limit)
        bad_flags = self._unsupported_flags(flags)
        if bad_flags:
            raise UnsupportedSession(
                "Persistent dbt sessions do not support "
                + ", ".join(bad_flags)
                + ". Use the process backend instead."
            )

        adapter_type = str(getattr(self._config.credentials, "type", "")).lower()
        if adapter_type not in {"duckdb", "snowflake"}:
            raise UnsupportedSession(
                f"Persistent dbt sessions do not support adapter {adapter_type or '<unknown>'}. "
                "Use the process backend instead."
            )

        self._reset_invocation(flags)
        if self._on_config is not None:
            self._on_config(self._credential_paths())

        requires.setup_manifest(ctx, write=False)
        self._manifest = ctx.obj["manifest"]
        self._adapter = importlib.import_module("dbt.adapters.factory").get_adapter(self._config)
        self._initialized = True

    def _prepare_request(self, sql: str, limit: int) -> None:
        if not self._initialized:
            self._initialize(sql, limit)
            return

        flags = self._new_flags(sql, limit)
        bad_flags = self._unsupported_flags(flags)
        if bad_flags:
            raise UnsupportedSession(
                "Persistent dbt sessions do not support "
                + ", ".join(bad_flags)
                + ". Use the process backend instead."
            )
        self._reset_invocation(flags)
        self._adapter.cache.clear()
        loader = importlib.import_module("dbt.parser.manifest").ManifestLoader
        self._manifest = loader.get_full_manifest(self._config, reset=True)

    def _rollback_open_transaction(self) -> None:
        if self._adapter is None:
            return
        connection = self._adapter.connections.get_if_exists()
        state = importlib.import_module("dbt.adapters.contracts.connection").ConnectionState
        # Inspect state before handle: reading Connection.handle resolves an INIT
        # LazyHandle and would accidentally open a connection during cleanup.
        if (
            connection is not None
            and connection.state == state.OPEN
            and connection.transaction_open
        ):
            self._adapter.connections.rollback_if_open()

    @staticmethod
    def _table_result(table: Any) -> tuple[list[str], list[list[Any]]]:
        columns = list(table.column_names)
        output = _CappedTextBuffer(_MAX_JSON_BYTES)
        table.to_json(output)
        records = json.loads(output.getvalue())
        rows = [[record.get(column) for column in columns] for record in records]
        return columns, rows

    def execute(self, prepared_sql: str, limit: int) -> dict[str, Any]:
        if self._closed:
            raise SessionError("dbt session is closed; create a fresh session")
        if not isinstance(prepared_sql, str):
            self._retire()
            raise TypeError("prepared_sql must be a string")
        if not isinstance(limit, int):
            self._retire()
            raise TypeError("limit must be an integer")

        current_thread = threading.get_ident()
        if self._thread_id is None:
            self._thread_id = current_thread
        elif self._thread_id != current_thread:
            self.close()
            raise SessionError("dbt session must be used from one OS thread")

        self._warnings = []
        task: Any = None
        runner: Any = None
        completed = False
        try:
            self._prepare_request(prepared_sql, limit)
            show = importlib.import_module("dbt.task.show")
            options = {}
            if "catalogs" in inspect.signature(show.ShowTask).parameters:
                options["catalogs"] = self._ctx.obj.get("catalogs")
            task = show.ShowTask(self._config.args, self._config, self._manifest, **options)

            contextvars = importlib.import_module("dbt_common.events.contextvars")
            with contextvars.task_contextvars(project_root=self._config.project_root):
                task._runtime_initialize()
                task.defer_to_manifest()
                node = self._manifest.nodes[task._inline_node_id]
                with contextvars.log_contextvars(node_info=node.node_info):
                    with self._adapter.connection_named(
                        node.unique_id, node, should_release_connection=False
                    ):
                        runner = task.get_runner(node)
                        compiled = runner.compile(self._manifest)
                        result = runner.execute(compiled, self._manifest)

            columns, rows = self._table_result(result.agate_table)
            response = {
                "columns": columns,
                "rows": rows,
                "compiled_sql": result.node.compiled_code,
                "warnings": list(self._warnings),
            }
            completed = True
            return response
        except Exception as error:
            message = _scrub_error(error)
            self._retire()
            if isinstance(error, UnsupportedSession):
                raise UnsupportedSession(message) from None
            raise SessionError(message) from None
        finally:
            try:
                if task is not None:
                    inline_id = getattr(task, "_inline_node_id", None)
                    if inline_id is not None and self._manifest is not None:
                        self._manifest.nodes.pop(inline_id, None)
                    compiler = getattr(task, "compiler", None)
                    if compiler is not None:
                        compiler.selected_node_ids = set()
                if runner is not None:
                    compiler = getattr(runner, "compiler", None)
                    if compiler is not None:
                        compiler.selected_node_ids = set()
                selected = importlib.import_module("dbt.selected_resources")
                selected.set_selected_resources(set())
                self._rollback_open_transaction()
            except Exception as cleanup_error:
                # An execute error has already retired the adapter; cleanup must
                # not replace it.  If the request otherwise completed, prevent a
                # cleanup failure from being reported as a successful response.
                if not self._closed:
                    self._retire()
                if completed:
                    raise SessionError(_scrub_error(cleanup_error)) from None

    def _retire(self) -> None:
        try:
            self.close()
        except Exception:
            # Retirement is best-effort and must never mask the triggering dbt
            # error with a cleanup error.
            self._closed = True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []
        if self._ctx is not None or self._adapter is not None:
            try:
                importlib.import_module("dbt.adapters.factory").reset_adapters()
            except Exception as error:
                errors.append(error)
        # dbt-duckdb defaults to keep_open=True: closing all named handles leaves
        # its process-global environment's root connection alive. This lifecycle
        # owns that environment too. Close it explicitly rather than waiting for
        # garbage collection/atexit (which can keep a database file locked).
        credentials = getattr(self._config, "credentials", None)
        if getattr(credentials, "type", None) == "duckdb":
            try:
                manager = importlib.import_module(
                    "dbt.adapters.duckdb.connections"
                ).DuckDBConnectionManager
                environment = getattr(manager, "_ENV", None)
                close_environment = getattr(environment, "close", None)
                if callable(close_environment):
                    close_environment()
                manager.close_all_connections()
            except Exception as error:
                errors.append(error)
        try:
            if self._ctx_entered and self._ctx is not None:
                self._ctx.__exit__(None, None, None)
                self._ctx_entered = False
        except Exception as error:
            errors.append(error)
        try:
            callbacks = getattr(self._event_manager, "callbacks", None)
            if callbacks is not None and self._event_callback in callbacks:
                callbacks.remove(self._event_callback)
        except Exception as error:
            errors.append(error)
        self._adapter = None
        self._manifest = None
        self._config = None
        if errors:
            raise SessionError(_scrub_error(errors[0])) from None
