"""Explicit, preview-first installation lifecycle operations.

This module never checks releases in the background.  Update candidates are an
explicit Git ref or local artifact, are resolved to immutable provenance, and
must validate the existing dbt-enhance user settings before replacement.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import unquote, urlsplit

from dbt_enhance.helix import HELIX_BRIDGE_FORMAT, integration_status, remove_owned_binding
from dbt_enhance.models import EnhanceError

_STATE_SCHEMA = 1
_STATE_LIMIT = 256 * 1024
_COMMIT = re.compile(r"[0-9a-fA-F]{40,64}\Z")
_VERSION = re.compile(r"[^\x00-\x1f\x7f]{1,128}\Z")
_SHORTCUT = re.compile(r"[A-Za-z0-9_-]{1,32}(?:\.[A-Za-z0-9_-]{1,32}){0,3}\Z")


class CommandRunner(Protocol):
    """The subprocess seam used by all installation lifecycle commands."""

    def __call__(
        self, command: Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]: ...


WorkerInventory = Callable[[], tuple[dict[str, Any], ...]]
WorkerAction = Callable[..., bool]


@dataclass(frozen=True)
class InstallProvenance:
    """Real, immutable identity plus stable integration details for one install."""

    source: str
    kind: Literal["git", "artifact", "legacy"]
    identity: str
    version: str
    launcher: str
    helix_config: str | None = None
    shortcut: str = "space.x"
    helix_launcher: tuple[str, ...] = ()
    owned_modes: tuple[str, ...] = ("normal", "select")


@dataclass(frozen=True)
class InstallationState:
    current: InstallProvenance
    prior: InstallProvenance | None = None


@dataclass(frozen=True)
class LifecyclePlan:
    """A non-applying description that must be passed to an apply function."""

    operation: Literal["update", "rollback", "remove"]
    effects: tuple[str, ...]
    state_root: Path
    current: InstallProvenance | None = None
    candidate: InstallProvenance | None = None
    settings_config: Path | None = None
    required_bridge_format: int | None = None
    workers: tuple[dict[str, Any], ...] = ()
    purge: bool = False
    preferences: Path | None = None


@dataclass(frozen=True)
class LifecycleResult:
    """Completed effects and recoverable partial failures; no atomicity is implied."""

    operation: str
    completed: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


def default_state_root(environment: Mapping[str, str] | None = None) -> Path:
    """Return the private lifecycle state directory without creating it."""
    environment = os.environ if environment is None else environment
    home = environment.get("XDG_STATE_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".local" / "state"
    return base / "dbt-enhance"


def _run(command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, **kwargs)


def _command(runner: CommandRunner, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return runner(list(command), capture_output=True, text=True, check=False)
    except OSError as exc:
        raise EnhanceError(f"Could not run {command[0]}: {exc}") from exc


def _root(path: Path | None) -> Path:
    return (path or default_state_root()).expanduser().absolute()


@contextlib.contextmanager
def _state_directory(path: Path, *, create: bool) -> Iterator[int | None]:
    """Open the final state directory without following it as a symlink."""
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            yield None
            return
        raise
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise EnhanceError(
            f"Lifecycle state must be an owned, non-symlink directory with mode 0700: {path}"
        )
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if opened.st_ino != info.st_ino or opened.st_dev != info.st_dev:
            raise EnhanceError("Lifecycle state directory changed while it was being opened.")
        yield fd
    finally:
        os.close(fd)


def _open_state_file(directory: int, name: str, flags: int, *, mode: int = 0o600) -> int:
    fd = os.open(
        name,
        flags | getattr(os, "O_NOFOLLOW", 0),
        mode,
        dir_fd=directory,
    )
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        os.close(fd)
        raise EnhanceError("Lifecycle state files must be owned regular files with mode 0600.")
    return fd


def _provenance(raw: object) -> InstallProvenance:
    if not isinstance(raw, dict):
        raise ValueError
    expected = {
        "source",
        "kind",
        "identity",
        "version",
        "launcher",
        "helix_config",
        "shortcut",
        "helix_launcher",
        "owned_modes",
    }
    if set(raw) != expected:
        raise ValueError
    source = raw["source"]
    kind = raw["kind"]
    identity = raw["identity"]
    version = raw["version"]
    launcher = raw["launcher"]
    helix_config = raw["helix_config"]
    shortcut = raw["shortcut"]
    helix_launcher = raw["helix_launcher"]
    owned_modes = raw["owned_modes"]
    strings = (source, identity, version, launcher, shortcut)
    if (
        kind not in {"git", "artifact", "legacy"}
        or any(not isinstance(value, str) or not value for value in strings)
        or not _VERSION.fullmatch(version)
        or len(source) > 8192
        or any(character in source + identity + launcher for character in "\x00\r\n")
        or not Path(launcher).is_absolute()
        or (helix_config is not None and not isinstance(helix_config, str))
        or (helix_config is not None and not Path(helix_config).is_absolute())
        or not _SHORTCUT.fullmatch(shortcut)
        or not isinstance(helix_launcher, list)
        or not all(isinstance(value, str) and value for value in helix_launcher)
        or (helix_launcher and not Path(helix_launcher[0]).is_absolute())
        or not isinstance(owned_modes, list)
        or tuple(owned_modes) != ("normal", "select")
        or _has_url_credentials(source)
    ):
        raise ValueError
    if kind == "git":
        base = source.partition("#")[0]
        _repository, separator, ref = base.removeprefix("git+").rpartition("@")
        if not source.startswith("git+") or not separator or ref.casefold() != identity.casefold():
            raise ValueError
        if not _COMMIT.fullmatch(identity):
            raise ValueError
    elif kind == "artifact":
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", identity) or not source.startswith("file:"):
            raise ValueError
    return InstallProvenance(
        source=source,
        kind=kind,
        identity=identity,
        version=version,
        launcher=launcher,
        helix_config=helix_config,
        shortcut=shortcut,
        helix_launcher=tuple(helix_launcher),
        owned_modes=tuple(owned_modes),
    )


def load_state(*, state_root: Path | None = None) -> InstallationState | None:
    """Read and strictly validate private lifecycle bookkeeping."""
    root = _root(state_root)
    with _state_directory(root, create=False) as directory:
        if directory is None:
            return None
        try:
            fd = _open_state_file(directory, "manifest.json", os.O_RDONLY)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(_STATE_LIMIT + 1)
    if len(data) > _STATE_LIMIT:
        raise EnhanceError("Lifecycle manifest exceeds its 256 KiB safety limit.")
    try:
        raw = json.loads(data)
        if not isinstance(raw, dict) or set(raw) != {"schema", "current", "prior"}:
            raise ValueError
        if raw["schema"] != _STATE_SCHEMA:
            raise ValueError
        current = _provenance(raw["current"])
        prior = None if raw["prior"] is None else _provenance(raw["prior"])
    except (UnicodeError, json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
        raise EnhanceError(
            "Lifecycle manifest is malformed or incompatible; inspect it safely."
        ) from exc
    return InstallationState(current=current, prior=prior)


def _state_json(state: InstallationState) -> bytes:
    def encode(value: InstallProvenance | None) -> object:
        if value is None:
            return None
        raw = asdict(value)
        raw["helix_launcher"] = list(value.helix_launcher)
        raw["owned_modes"] = list(value.owned_modes)
        try:
            _provenance(raw)
        except ValueError as exc:
            raise EnhanceError("Refusing to record invalid installation provenance.") from exc
        return raw

    data = json.dumps(
        {"schema": _STATE_SCHEMA, "current": encode(state.current), "prior": encode(state.prior)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    if len(data) > _STATE_LIMIT:
        raise EnhanceError("Lifecycle manifest exceeds its 256 KiB safety limit.")
    return data


def record_install(state: InstallationState, *, state_root: Path | None = None) -> None:
    """Atomically record verified install provenance in private state."""
    for provenance in (state.current, state.prior):
        if provenance is not None and _has_url_credentials(provenance.source):
            raise EnhanceError(
                "Lifecycle state cannot store a source with embedded URL credentials."
            )
    root = _root(state_root)
    data = _state_json(state)
    with _state_directory(root, create=True) as directory:
        assert directory is not None
        # Refuse to replace a symlink, insecure file, or malformed existing manifest.
        try:
            _ = load_state(state_root=root)
        except FileNotFoundError:
            pass
        temporary = f".manifest-{os.getpid()}-{os.urandom(8).hex()}"
        fd = _open_state_file(directory, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, "manifest.json", src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def _has_url_credentials(source: str) -> bool:
    candidate = source.removeprefix("git+")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https", "ssh"}:
        return False
    if parsed.password is not None:
        return True
    return parsed.scheme in {"http", "https"} and parsed.username is not None


def _git_source(source: str, runner: CommandRunner) -> tuple[str, Literal["git"], str]:
    base, marker, fragment = source.partition("#")
    repository_and_ref = base.removeprefix("git+")
    repository, separator, ref = repository_and_ref.rpartition("@")
    if not separator or not repository or not ref:
        raise EnhanceError(
            "Git update sources must include an explicit tag, ref, or commit after '@'."
        )
    if ref.casefold() == "latest":
        raise EnhanceError(
            "Choose an explicit Git tag or commit; 'latest' is not an update target."
        )
    if _has_url_credentials("git+" + repository):
        raise EnhanceError("Git source URLs must not contain embedded credentials.")
    if any(character in source for character in "\x00\r\n"):
        raise EnhanceError("The update source must not contain control characters.")
    if _COMMIT.fullmatch(ref):
        commit = ref.lower()
    else:
        result = _command(
            runner,
            [
                "git",
                "ls-remote",
                repository,
                ref,
                f"refs/tags/{ref}",
                f"refs/tags/{ref}^{{}}",
                f"refs/heads/{ref}",
            ],
        )
        if result.returncode:
            message = result.stderr.strip() or "Git could not resolve the requested ref."
            raise EnhanceError(f"Could not resolve Git update source: {message}")
        candidates: list[tuple[str, str]] = []
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) == 2 and _COMMIT.fullmatch(fields[0]):
                candidates.append((fields[0].lower(), fields[1]))
        peeled = [commit for commit, name in candidates if name.endswith("^{}")]
        commits = peeled or [commit for commit, _ in candidates]
        if not commits or len(set(commits)) != 1:
            raise EnhanceError("The Git ref did not resolve to one unambiguous commit.")
        commit = commits[0]
    pinned = f"git+{repository}@{commit}"
    if marker:
        pinned += f"#{fragment}"
    return pinned, "git", commit


def _artifact_path(source: str) -> Path:
    if source.startswith("file:"):
        parsed = urlsplit(source)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise EnhanceError("Local artifacts must use a local file path or file:// URL.")
        candidate = Path(unquote(parsed.path))
    else:
        candidate = Path(source).expanduser()
    try:
        info = candidate.lstat()
    except OSError as exc:
        raise EnhanceError(f"Cannot read local update artifact {candidate}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise EnhanceError("A local update candidate must be a regular, non-symlink artifact file.")
    return candidate.absolute()


def _artifact_digest(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise EnhanceError("A local update candidate must be a regular artifact file.")
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise EnhanceError(f"Cannot hash local update artifact {path}: {exc}") from exc
    return digest.hexdigest()


def _resolve_source(
    source: str, runner: CommandRunner
) -> tuple[str, Literal["git", "artifact"], str]:
    if not isinstance(source, str) or not source.strip():
        raise EnhanceError("Update requires an explicit Git ref or local artifact.")
    source = source.strip()
    if source.startswith("git+"):
        return _git_source(source, runner)
    if source.casefold() == "latest" or source.startswith("dbt-enhance"):
        raise EnhanceError(
            "Update requires an explicit Git ref or supplied local artifact, not a registry name."
        )
    path = _artifact_path(source)
    digest = _artifact_digest(path)
    return path.as_uri(), "artifact", f"sha256:{digest}"


def resolve_setup_source(
    source: str, *, runner: CommandRunner = _run
) -> tuple[str, Literal["git", "artifact", "legacy"], str]:
    """Pin a setup source when possible while retaining legacy setup compatibility.

    Registry pins and local source directories remain installable for the existing
    setup flow, but are marked ``legacy`` and can never be rollback candidates.
    """
    if not isinstance(source, str) or not source.strip():
        raise EnhanceError("Setup requires a non-empty install source.")
    source = source.strip()
    if _has_url_credentials(source):
        raise EnhanceError("Install source URLs must not contain embedded credentials.")
    if source.startswith("git+"):
        return _git_source(source, runner)
    try:
        return _resolve_source(source, runner)
    except EnhanceError:
        if source.casefold() == "latest" or any(character in source for character in "\x00\r\n"):
            raise
        return source, "legacy", source


def _validation_command(source: str, settings_config: Path) -> list[str]:
    return [
        "uvx",
        "--isolated",
        "--from",
        source,
        "dbt-enhance",
        "config",
        "validate",
        "--config",
        str(settings_config),
        "--json",
    ]


def _validate_candidate(
    source: str,
    settings_config: Path,
    runner: CommandRunner,
    *,
    expected_version: str | None = None,
    required_bridge_format: int | None = None,
) -> str:
    result = _command(runner, _validation_command(source, settings_config))
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or "candidate validation failed"
        raise EnhanceError(
            "Candidate cannot safely read the current dbt-enhance settings; "
            f"no replacement was made: {message}"
        )
    try:
        report = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise EnhanceError(
            "Candidate validation did not return JSON; no replacement was made. "
            "Choose a release that supports `config validate --json`."
        ) from exc
    if (
        not isinstance(report, dict)
        or report.get("valid") is not True
        or type(report.get("schema")) is not int
        or report.get("schema") != 1
        or not isinstance(report.get("version"), str)
        or not _VERSION.fullmatch(report["version"])
    ):
        raise EnhanceError(
            "Candidate reported an incompatible capability protocol; no replacement was made."
        )
    candidate_bridge_format = report.get("helix_bridge_format")
    if required_bridge_format is not None and (
        type(candidate_bridge_format) is not int
        or candidate_bridge_format != required_bridge_format
    ):
        raise EnhanceError(
            "Candidate does not support the required Helix bridge format; no replacement was made."
        )

    version_result = _command(
        runner,
        ["uvx", "--isolated", "--from", source, "dbt-enhance", "--version"],
    )
    version = version_result.stdout.strip()
    if version_result.returncode or not _VERSION.fullmatch(version):
        raise EnhanceError("Candidate version could not be verified; no replacement was made.")
    if version != report["version"]:
        raise EnhanceError(
            "Candidate capability and executable versions disagree; no replacement was made."
        )
    if expected_version is not None and version != expected_version:
        raise EnhanceError(
            f"Candidate reports version {version!r}, not recorded version {expected_version!r}; "
            "no replacement was made."
        )
    return version


def validate_setup_candidate(
    source: str, *, settings_config: Path, runner: CommandRunner = _run
) -> str:
    """Validate settings and the new bridge contract before setup replaces a package."""
    return _validate_candidate(
        source, settings_config, runner, required_bridge_format=HELIX_BRIDGE_FORMAT
    )


def _require_state(state_root: Path) -> InstallationState:
    state = load_state(state_root=state_root)
    if state is None:
        raise EnhanceError(
            "No lifecycle provenance is recorded. Re-run explicit setup before update or rollback."
        )
    return state


def _require_nonlegacy_helix(current: InstallProvenance, root: Path) -> int | None:
    if not current.helix_config or not current.helix_launcher:
        return None
    status = integration_status(
        Path(current.helix_config),
        list(current.helix_launcher),
        state_root=root,
        shortcut=current.shortcut,
    )
    if status == "legacy":
        raise EnhanceError(
            "Package-only update is blocked because the recorded Helix mapping uses the unsafe "
            "legacy filename transport. Run setup --helix-shell-bridge, reload Helix, then retry."
        )
    return HELIX_BRIDGE_FORMAT


def preview_update(
    source: str,
    *,
    settings_config: Path,
    runner: CommandRunner = _run,
    state_root: Path | None = None,
) -> LifecyclePlan:
    """Resolve and validate an explicit candidate without changing installation or state.

    The isolated ``uvx`` checks may download and execute the selected candidate.
    ``settings_config`` is the dbt-enhance user settings file, not Helix TOML.
    """
    root = _root(state_root)
    state = _require_state(root)
    required_bridge_format = _require_nonlegacy_helix(state.current, root)
    pinned, kind, identity = _resolve_source(source, runner)
    version = _validate_candidate(
        pinned,
        settings_config,
        runner,
        required_bridge_format=required_bridge_format,
    )
    if kind == "artifact":
        path = _artifact_path(pinned)
        if f"sha256:{_artifact_digest(path)}" != identity:
            raise EnhanceError("Local artifact changed during validation; no replacement was made.")
    candidate = replace(
        state.current,
        source=pinned,
        kind=kind,
        identity=identity,
        version=version,
    )
    return LifecyclePlan(
        operation="update",
        effects=(
            f"install immutable {kind} candidate {identity}",
            "preserve dbt-enhance settings and Helix mappings",
            "record current provenance as rollback candidate",
        ),
        state_root=root,
        current=state.current,
        candidate=candidate,
        settings_config=settings_config.expanduser().absolute(),
        required_bridge_format=required_bridge_format,
    )


@contextlib.contextmanager
def _applied_source(candidate: InstallProvenance, root: Path) -> Iterator[str]:
    """Stage a digest-checked local artifact privately; Git commits need no staging."""
    if candidate.kind != "artifact":
        yield candidate.source
        return
    source = _artifact_path(candidate.source)
    expected = candidate.identity.removeprefix("sha256:")
    if _artifact_digest(source) != expected:
        raise EnhanceError("Local artifact changed since preview; no replacement was made.")
    suffix = "".join(source.suffixes)[-32:]
    name = f"candidate-{os.urandom(8).hex()}{suffix}"
    with _state_directory(root, create=True) as directory:
        assert directory is not None
        destination = _open_state_file(directory, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            with (
                os.fdopen(os.open(source, flags), "rb") as incoming,
                os.fdopen(destination, "wb") as outgoing,
            ):
                shutil.copyfileobj(incoming, outgoing)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            staged = root / name
            if _artifact_digest(staged) != expected:
                raise EnhanceError("Staged artifact digest did not match the previewed candidate.")
            yield staged.as_uri()
        finally:
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass


def _unchanged(plan: LifecyclePlan) -> InstallationState:
    state = _require_state(plan.state_root)
    if state.current != plan.current:
        raise EnhanceError(
            "Installation provenance changed after preview; preview the operation again."
        )
    return state


def apply_update(plan: LifecyclePlan, *, runner: CommandRunner = _run) -> LifecycleResult:
    """Revalidate and replace exactly the previewed immutable update candidate."""
    if plan.operation != "update" or plan.candidate is None or plan.settings_config is None:
        raise EnhanceError("apply_update requires an update preview plan.")
    state = _unchanged(plan)
    completed: list[str] = []
    errors: list[str] = []
    with _applied_source(plan.candidate, plan.state_root) as source:
        _validate_candidate(
            source,
            plan.settings_config,
            runner,
            expected_version=plan.candidate.version,
            required_bridge_format=plan.required_bridge_format,
        )
        result = _command(runner, ["uv", "tool", "install", "--force", source])
        if result.returncode:
            message = result.stderr.strip() or "uv tool install failed"
            return LifecycleResult("update", errors=(message,))
        completed.append("package installed")
        if not Path(plan.candidate.launcher).is_file():
            return LifecycleResult(
                "update",
                tuple(completed),
                (f"expected stable launcher is missing: {plan.candidate.launcher}",),
            )
    try:
        record_install(
            InstallationState(current=plan.candidate, prior=state.current),
            state_root=plan.state_root,
        )
        completed.append("provenance recorded")
    except (EnhanceError, OSError) as exc:
        errors.append(f"package was installed but provenance could not be recorded: {exc}")
    return LifecycleResult("update", tuple(completed), tuple(errors))


def preview_rollback(
    *,
    settings_config: Path,
    runner: CommandRunner = _run,
    state_root: Path | None = None,
) -> LifecyclePlan:
    """Validate the recorded immutable prior install without changing anything."""
    root = _root(state_root)
    state = _require_state(root)
    required_bridge_format = _require_nonlegacy_helix(state.current, root)
    prior = state.prior
    if prior is None:
        raise EnhanceError("No prior installation provenance is available for rollback.")
    if prior.kind not in {"git", "artifact"}:
        raise EnhanceError(
            "The prior install has no immutable commit or artifact digest; "
            "safe rollback is blocked."
        )
    if prior.kind == "artifact":
        path = _artifact_path(prior.source)
        if f"sha256:{_artifact_digest(path)}" != prior.identity:
            raise EnhanceError("The recorded rollback artifact is missing or has changed.")
    _validate_candidate(
        prior.source,
        settings_config,
        runner,
        expected_version=prior.version,
        required_bridge_format=required_bridge_format,
    )
    return LifecyclePlan(
        operation="rollback",
        effects=(
            f"install recorded {prior.kind} candidate {prior.identity}",
            "preserve dbt-enhance settings and Helix mappings",
            "swap current and prior provenance",
        ),
        state_root=root,
        current=state.current,
        candidate=prior,
        settings_config=settings_config.expanduser().absolute(),
        required_bridge_format=required_bridge_format,
    )


def apply_rollback(plan: LifecyclePlan, *, runner: CommandRunner = _run) -> LifecycleResult:
    """Revalidate settings and perform a version-only rollback."""
    if plan.operation != "rollback" or plan.candidate is None or plan.settings_config is None:
        raise EnhanceError("apply_rollback requires a rollback preview plan.")
    state = _unchanged(plan)
    if state.prior != plan.candidate:
        raise EnhanceError(
            "Rollback provenance changed after preview; preview the operation again."
        )
    completed: list[str] = []
    errors: list[str] = []
    with _applied_source(plan.candidate, plan.state_root) as source:
        _validate_candidate(
            source,
            plan.settings_config,
            runner,
            expected_version=plan.candidate.version,
            required_bridge_format=plan.required_bridge_format,
        )
        result = _command(runner, ["uv", "tool", "install", "--force", source])
        if result.returncode:
            return LifecycleResult(
                "rollback", errors=(result.stderr.strip() or "uv tool install failed",)
            )
        completed.append("package rolled back")
        if not Path(plan.candidate.launcher).is_file():
            return LifecycleResult(
                "rollback",
                tuple(completed),
                (f"expected stable launcher is missing: {plan.candidate.launcher}",),
            )
    try:
        record_install(
            InstallationState(current=plan.candidate, prior=state.current),
            state_root=plan.state_root,
        )
        completed.append("provenance recorded")
    except (EnhanceError, OSError) as exc:
        errors.append(f"package was rolled back but provenance could not be recorded: {exc}")
    return LifecycleResult("rollback", tuple(completed), tuple(errors))


def _default_inventory() -> tuple[dict[str, Any], ...]:
    from dbt_enhance import worker

    inventory = getattr(worker, "inventory", None)
    if inventory is None:
        raise EnhanceError(
            "This installation cannot inventory workers safely; update it before removal."
        )
    return inventory()


def _default_disconnect(project: Path) -> bool:
    from dbt_enhance import worker

    return worker.disconnect(project)


def _default_reset(project: Path, *, allow_active: bool = False) -> bool:
    from dbt_enhance import worker

    return worker.reset(project, allow_active=allow_active)


def _default_removal_guard() -> contextlib.AbstractContextManager[Any]:
    from dbt_enhance import worker

    return worker.removal_guard()


def _worker_project(item: Mapping[str, Any]) -> Path:
    raw = item.get("project", item.get("project_dir"))
    if not isinstance(raw, (str, Path)) or not str(raw):
        raise EnhanceError(
            "Worker inventory did not identify a project; package removal is blocked safely."
        )
    return Path(raw).expanduser().absolute()


def _worker_active(item: Mapping[str, Any]) -> bool:
    for key in ("active", "query_active", "busy"):
        if isinstance(item.get(key), bool):
            return bool(item[key])
    status = item.get("status")
    if isinstance(status, str):
        if status.casefold() in {"idle", "disconnected", "stopped"}:
            return False
        if status.casefold() in {"active", "busy", "querying", "running"}:
            return True
    # Unknown inventory revisions are handled conservatively.
    return True


def preview_remove(
    *,
    purge: bool = False,
    inventory: WorkerInventory = _default_inventory,
    state_root: Path | None = None,
    preferences: Path | None = None,
) -> LifecyclePlan:
    """Preview owned package, mapping, worker and optional preference effects."""
    root = _root(state_root)
    state = load_state(state_root=root)
    workers = tuple(dict(item) for item in inventory())
    for item in workers:
        _worker_project(item)
    current = state.current if state else None
    if purge and preferences is None:
        from dbt_enhance.settings import settings_path

        preferences = settings_path()
    effects: list[str] = []
    for item in workers:
        project = _worker_project(item)
        state_name = "active (confirmation required)" if _worker_active(item) else "idle"
        effects.append(f"disconnect {state_name} worker for {project}")
    if current and current.helix_config:
        effects.extend(
            (
                f"remove only exact owned {current.shortcut} mappings from {current.helix_config}",
                "restore editor.shell only if it still selects the exact owned bridge",
                "remove only the exact unmodified owned bridge script",
                "reload or restart open Helix instances after removal",
            )
        )
    effects.append("run uv tool uninstall dbt-enhance")
    effects.append("remove lifecycle manifest")
    if purge:
        effects.extend(("remove dbt-enhance preferences", "remove dbt-enhance diagnostics"))
    return LifecyclePlan(
        operation="remove",
        effects=tuple(effects),
        state_root=root,
        current=current,
        workers=workers,
        purge=purge,
        preferences=preferences.expanduser().absolute() if preferences else None,
    )


def _unlink_manifest(root: Path) -> None:
    with _state_directory(root, create=False) as directory:
        if directory is None:
            return
        try:
            fd = _open_state_file(directory, "manifest.json", os.O_RDONLY)
        except FileNotFoundError:
            return
        os.close(fd)
        os.unlink("manifest.json", dir_fd=directory)
        os.fsync(directory)


def _remove_regular(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise EnhanceError(f"Refusing to purge non-regular preference file: {path}")
    path.unlink()


def _remove_diagnostics(root: Path) -> None:
    path = root / "diagnostics"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise EnhanceError(f"Refusing to purge non-private diagnostics directory: {path}")
    shutil.rmtree(path)


def apply_remove(
    plan: LifecyclePlan,
    *,
    allow_active: bool = False,
    runner: CommandRunner = _run,
    inventory: WorkerInventory = _default_inventory,
    disconnect: WorkerAction = _default_disconnect,
    reset: WorkerAction = _default_reset,
    removal_guard: Callable[[], contextlib.AbstractContextManager[Any]] = (_default_removal_guard),
) -> LifecycleResult:
    """Recheck workers under exclusive admission, then remove owned effects."""
    if plan.operation != "remove":
        raise EnhanceError("apply_remove requires a removal preview plan.")

    with removal_guard() as guard:
        state = load_state(state_root=plan.state_root)
        current = state.current if state else None
        if current != plan.current:
            raise EnhanceError(
                "Installation provenance changed after preview; preview removal again."
            )

        # This is intentionally refreshed only after new execute admission is
        # excluded. Accepted work holds no admission lock and remains visible to
        # the supervisor as active before its acceptance acknowledgement.
        workers = tuple(dict(item) for item in inventory())
        projects = [(_worker_project(item), _worker_active(item)) for item in workers]
        active = [str(project) for project, is_active in projects if is_active]
        if active and not allow_active:
            raise EnhanceError(
                "Active workers block removal. Re-run with explicit active-worker confirmation: "
                + ", ".join(active)
            )

        completed: list[str] = []
        errors: list[str] = []
        for project, is_active in projects:
            try:
                stopped = (
                    reset(project, allow_active=allow_active) if is_active else disconnect(project)
                )
                if not stopped:
                    errors.append(f"worker supervisor did not disconnect {project}")
                else:
                    completed.append(f"worker disconnected: {project}")
            except Exception as exc:  # injected supervisor errors are partial failures
                errors.append(f"worker {project}: {exc}")
        if errors:
            return LifecycleResult("remove", tuple(completed), tuple(errors))

        if current and current.helix_config and current.helix_launcher:
            try:
                changed = remove_owned_binding(
                    Path(current.helix_config),
                    list(current.helix_launcher),
                    shortcut=current.shortcut,
                    state_root=plan.state_root,
                )
                completed.append(
                    "owned Helix mappings removed" if changed else "Helix mappings left unchanged"
                )
            except (EnhanceError, OSError) as exc:
                errors.append(f"Helix mapping removal failed: {exc}")
                # Keep both the executable and its ownership record for a safe retry.
                # Removing either would strand a mapping we can no longer identify.
                return LifecycleResult("remove", tuple(completed), tuple(errors))

        # Worker retirement and owned integration cleanup succeeded. Advance the
        # persistent epoch before uninstall so requests queued on a project lock
        # under the old installation fail before sending SQL.
        guard.invalidate()
        result = _command(runner, ["uv", "tool", "uninstall", "dbt-enhance"])
        if result.returncode:
            errors.append(result.stderr.strip() or "uv tool uninstall failed")
            return LifecycleResult("remove", tuple(completed), tuple(errors))
        completed.append("package uninstalled")

        try:
            _unlink_manifest(plan.state_root)
            completed.append("lifecycle manifest removed")
        except (EnhanceError, OSError) as exc:
            errors.append(f"lifecycle manifest removal failed: {exc}")
        if plan.purge:
            if plan.preferences is not None:
                try:
                    _remove_regular(plan.preferences)
                    completed.append("preferences removed")
                except (EnhanceError, OSError) as exc:
                    errors.append(f"preference purge failed: {exc}")
            try:
                _remove_diagnostics(plan.state_root)
                completed.append("diagnostics removed")
            except (EnhanceError, OSError) as exc:
                errors.append(f"diagnostics purge failed: {exc}")
        return LifecycleResult("remove", tuple(completed), tuple(errors))
