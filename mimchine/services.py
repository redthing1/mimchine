from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, TypeVar

from .builders import Builder, get_builder
from .config import AppConfig, Defaults, load_config, validate_builder, validate_runner
from .domain import (
    BuildSpec,
    ExecSpec,
    IdentityMode,
    MachineRecord,
    MountSpec,
    NetworkMode,
    PortBind,
    ResourceSpec,
    RuntimeState,
)
from .log import logger
from .mounts import (
    map_host_path_to_guest,
    parse_home_share_spec,
    parse_mount_spec,
    parse_workspace_spec,
)
from .paths import data_dir
from .profiles import Profile, load_profile
from .runners import Runner, get_runner
from .shells import enter_shell_command, normalize_shell
from .shell_state import ShellStateManager
from .state import MachineStore


T = TypeVar("T")


@dataclass(frozen=True)
class BuildOptions:
    image: str
    file: Path
    context: Path
    builder: str | None = None
    platform: str | None = None
    build_args: tuple[str, ...] = ()
    no_cache: bool = False


@dataclass(frozen=True)
class CreateOptions:
    name: str
    image: str | None = None
    runner: str | None = None
    profile: str | None = None
    workspaces: tuple[str, ...] = ()
    home_shares: tuple[str, ...] = ()
    mounts: tuple[str, ...] = ()
    ports: tuple[str, ...] = ()
    env: tuple[str, ...] = ()
    workdir: str | None = None
    shell: str | None = None
    network: NetworkMode | None = None
    ssh_agent: bool | None = None
    gpu: bool | None = None
    cpus: int | None = None
    memory_mib: int | None = None
    identity: IdentityMode | None = None
    shell_state: bool | None = None
    container_args: tuple[str, ...] = ()
    start: bool = False


@dataclass(frozen=True)
class MachineView:
    record: MachineRecord
    state: RuntimeState


class BuildService:
    def __init__(self, config: AppConfig, builders: dict[str, Builder] | None = None):
        self.config = config
        self.builders = builders or {}

    @classmethod
    def default(cls) -> "BuildService":
        return cls(load_config())

    def build(self, options: BuildOptions) -> None:
        builder_name = validate_builder(options.builder or self.config.defaults.builder)
        builder = self.builders.get(builder_name) or get_builder(builder_name)
        spec = BuildSpec(
            image=options.image,
            file=options.file,
            context=options.context,
            platform=options.platform,
            build_args=options.build_args,
            no_cache=options.no_cache,
        )
        builder.build(spec)


class MachineService:
    def __init__(
        self,
        config: AppConfig,
        store: MachineStore,
        shell_state: ShellStateManager,
        runners: dict[str, Runner] | None = None,
    ):
        self.config = config
        self.store = store
        self.shell_state = shell_state
        self.runners = runners or {}

    @classmethod
    def default(cls) -> "MachineService":
        app_data = data_dir()
        return cls(
            load_config(),
            MachineStore(app_data / "machines"),
            ShellStateManager(app_data / "shell-state"),
        )

    def create(self, options: CreateOptions) -> MachineRecord:
        if self.store.exists(options.name):
            raise ValueError(f"machine [{options.name}] already exists")

        profile = load_profile(self.config, options.profile)
        shell_state_enabled = _bool_option(
            options.shell_state,
            profile.shell_state,
            True,
        )
        shell_state_preexisted = (
            shell_state_enabled and self.shell_state.path_for(options.name).exists()
        )
        record = self._record_from_options(options, profile, shell_state_enabled)
        runner = self._runner(record.runner)
        _validate_create(record, runner)

        try:
            if record.shell_state:
                self.shell_state.ensure(record.name)
            runner.create(record)
        except Exception:
            _delete_created_shell_state(
                self.shell_state, record, shell_state_preexisted
            )
            raise

        try:
            self.store.save(record)
        except Exception:
            _try_delete_machine(runner, record)
            _delete_created_shell_state(
                self.shell_state, record, shell_state_preexisted
            )
            raise
        if options.start:
            _ensure_running(record, runner)
        return record

    def start(self, name: str) -> RuntimeState:
        record = self.store.load(name)
        runner = self._runner(record.runner)
        return _ensure_running(record, runner)

    def stop(self, name: str) -> RuntimeState:
        record = self.store.load(name)
        runner = self._runner(record.runner)
        state = runner.inspect(record)
        if state is RuntimeState.RUNNING:
            runner.stop(record)
        return runner.inspect(record)

    def delete(self, name: str, *, keep_shell_state: bool = False) -> None:
        record = self.store.load(name)
        self._runner(record.runner).delete(record)
        self.store.delete(name)
        if not keep_shell_state:
            self.shell_state.delete(name)

    def enter(self, name: str, shell: str | None = None) -> None:
        record = self.store.load(name)
        runner = self._runner(record.runner)
        _ensure_running(record, runner)

        selected_shell = shell or record.shell or self.config.defaults.shell
        shell_command = enter_shell_command(selected_shell)
        exec_env = [
            f"MIM_MACHINE={record.name}",
            f"MIM_RUNNER={record.runner}",
            f"MIM_SHELL_STATE={int(record.shell_state)}",
        ]
        for variable in ("TERM", "COLORTERM"):
            if value := os.environ.get(variable):
                exec_env.append(f"{variable}={value}")
        workdir = _mapped_cwd(record.mounts) or _session_workdir(record)
        runner.exec(
            record,
            ExecSpec(
                command=shell_command,
                interactive=True,
                tty=True,
                env=tuple(exec_env),
                workdir=workdir,
            ),
        )

    def exec(self, name: str, spec: ExecSpec) -> None:
        record = self.store.load(name)
        runner = self._runner(record.runner)
        _ensure_running(record, runner)
        runner.exec(record, spec)

    def ssh_session(
        self,
        name: str,
        *,
        original_command: str | None,
        tty: bool,
        term: str | None = None,
    ) -> None:
        record = self.store.load(name)
        runner = self._runner(record.runner)
        _ensure_running(record, runner)

        env = [
            f"MIM_MACHINE={record.name}",
            f"MIM_RUNNER={record.runner}",
            f"MIM_SHELL_STATE={int(record.shell_state)}",
        ]
        if term:
            env.append(f"TERM={term}")

        if original_command is None:
            selected_shell = record.shell or self.config.defaults.shell
            command = enter_shell_command(selected_shell)
        else:
            command = ("sh", "-c", original_command)

        runner.exec(
            record,
            ExecSpec(
                command=command,
                interactive=True,
                tty=tty,
                env=tuple(env),
                workdir=_session_workdir(record),
            ),
        )

    def inspect(self, name: str) -> MachineView:
        return self._view(self.store.load(name))

    def list(self) -> list[MachineView]:
        return [self._view(record) for record in self.store.list()]

    def _view(self, record: MachineRecord) -> MachineView:
        return MachineView(record, self._runner(record.runner).inspect(record))

    def _runner(self, name: str) -> Runner:
        runner_name = validate_runner(name)
        return self.runners.get(runner_name) or get_runner(runner_name)

    def _record_from_options(
        self,
        options: CreateOptions,
        profile: Profile,
        shell_state_enabled: bool,
    ) -> MachineRecord:
        image = _first(options.image, profile.image)
        if image is None:
            raise ValueError("image is required")

        runner = validate_runner(
            _first(
                options.runner,
                profile.runner,
                self.config.defaults.runner,
            )
        )
        network = _first(
            options.network,
            profile.network,
            self.config.defaults.network,
        )
        resources = _resource_spec(options, profile, self.config.defaults)
        mounts = _mounts(options, profile)
        if shell_state_enabled:
            mounts = (*mounts, self.shell_state.mount_for(options.name))

        return MachineRecord(
            name=options.name,
            image=image,
            runner=runner,
            created_at=_now(),
            mounts=mounts,
            ports=_ports(options, profile),
            env=_env(options, profile),
            workdir=_first(options.workdir, profile.workdir),
            shell=normalize_shell(_first(options.shell, profile.shell)),
            network=network,
            identity=_first(
                options.identity,
                profile.identity,
                IdentityMode.IMAGE,
            ),
            resources=resources,
            shell_state=shell_state_enabled,
            ssh_agent=_bool_option(
                options.ssh_agent,
                profile.ssh_agent,
                False,
            ),
            gpu=_bool_option(
                options.gpu,
                profile.gpu,
                False,
            ),
            container_args=_container_args(options, profile),
        )


def _validate_create(record: MachineRecord, runner: Runner) -> None:
    if record.ports and record.network is NetworkMode.HOST:
        raise ValueError("port publishing cannot be used with host networking")
    if record.gpu and not runner.supports_gpu:
        raise ValueError(f"runner [{runner.name}] does not support GPU forwarding")


def _ensure_running(record: MachineRecord, runner: Runner) -> RuntimeState:
    state = runner.inspect(record)
    if state is RuntimeState.RUNNING:
        return state
    if state is RuntimeState.STOPPED:
        runner.start(record)
        state = runner.inspect(record)
        if state is not RuntimeState.RUNNING:
            raise ValueError(
                f"machine [{record.name}] failed to start; state is {state.value}"
            )
        return state
    if state is RuntimeState.MISSING:
        raise ValueError(f"machine [{record.name}] is missing; delete and recreate it")
    raise ValueError(f"machine [{record.name}] state is unknown")


def _delete_created_shell_state(
    shell_state: ShellStateManager,
    record: MachineRecord,
    preexisted: bool,
) -> None:
    if record.shell_state and not preexisted:
        shell_state.delete(record.name)


def _try_delete_machine(runner: Runner, record: MachineRecord) -> None:
    try:
        runner.delete(record)
    except Exception as exc:
        logger.warning(
            "failed to clean up machine [%s] after create failure: %s",
            record.name,
            exc,
        )


def _mounts(options: CreateOptions, profile: Profile) -> tuple[MountSpec, ...]:
    workspace_specs = profile.workspaces + options.workspaces
    home_share_specs = profile.home_shares + options.home_shares
    mount_specs = profile.mounts + options.mounts
    mounts: list[MountSpec] = []
    mounts.extend(parse_workspace_spec(value) for value in workspace_specs)
    for value in home_share_specs:
        mounts.extend(parse_home_share_spec(value))
    mounts.extend(parse_mount_spec(value) for value in mount_specs)
    return tuple(mounts)


def _ports(options: CreateOptions, profile: Profile) -> tuple[PortBind, ...]:
    values = profile.ports + options.ports
    return tuple(PortBind.parse(value) for value in values)


def _env(options: CreateOptions, profile: Profile) -> tuple[str, ...]:
    return profile.env + options.env


def _container_args(options: CreateOptions, profile: Profile) -> tuple[str, ...]:
    return profile.container_args + options.container_args


def _resource_spec(
    options: CreateOptions,
    profile: Profile,
    defaults: Defaults,
) -> ResourceSpec:
    return ResourceSpec(
        cpus=_first(
            options.cpus,
            profile.cpus,
            defaults.resources.cpus,
        ),
        memory_mib=_first(
            options.memory_mib,
            profile.memory,
            defaults.resources.memory_mib,
        ),
    )


def _first(*values: T | None) -> T | None:
    return next((value for value in values if value is not None), None)


def _bool_option(
    value: bool | None,
    profile_value: bool | None,
    default: bool,
) -> bool:
    if value is not None:
        return value
    if profile_value is not None:
        return profile_value
    return default


def _mapped_cwd(mounts: Iterable[MountSpec]) -> str | None:
    return map_host_path_to_guest(Path.cwd(), tuple(mounts))


def _session_workdir(record: MachineRecord) -> str | None:
    return record.workdir or next(
        (mount.target for mount in record.mounts if mount.kind == "workspace"),
        None,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()
