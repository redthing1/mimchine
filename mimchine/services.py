from __future__ import annotations

import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from platformdirs import user_data_dir

from .builders import Builder, get_builder
from .config import AppConfig, load_config, validate_builder, validate_runner
from .domain import (
    BuildSpec,
    ExecSpec,
    IdentityMode,
    IdentitySpec,
    ImageSource,
    ImageSourceKind,
    MachineRecord,
    MachineSpec,
    MountSpec,
    NetworkMode,
    NetworkSpec,
    PortBind,
    ResourceSpec,
    RuntimeState,
    RuntimeStatus,
    ShellStateSpec,
)
from .log import logger
from .mounts import map_host_path_to_guest, parse_mount_spec, parse_workspace_spec
from .parsing import parse_env, parse_network_mode, parse_port_bind
from .profiles import Profile, load_profile
from .runners import Runner, get_runner
from .shell_state import ShellStateManager
from .state import MachineStore


APP_NAME = "mimchine"


@dataclass(frozen=True)
class BuildOptions:
    image: str
    file: Path
    context: Path
    builder: str | None = None
    platform: str | None = None
    build_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class CreateOptions:
    name: str
    image: str | None = None
    runner: str | None = None
    profile: str | None = None
    workspaces: tuple[str, ...] = ()
    mounts: tuple[str, ...] = ()
    ports: tuple[str, ...] = ()
    env: tuple[str, ...] = ()
    workdir: str | None = None
    shell: str | None = None
    network: NetworkMode | None = None
    allow_hosts: tuple[str, ...] = ()
    allow_cidrs: tuple[str, ...] = ()
    ssh_agent: bool | None = None
    gpu: bool | None = None
    resources: ResourceSpec = ResourceSpec()
    identity: IdentitySpec | None = None
    shell_state: bool = True


@dataclass(frozen=True)
class MachineView:
    record: MachineRecord
    status: RuntimeStatus


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
            builder=builder_name,
            platform=options.platform,
            build_args=options.build_args,
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
        data_dir = get_data_dir()
        return cls(
            load_config(),
            MachineStore(data_dir / "machines"),
            ShellStateManager(data_dir / "shell-state"),
        )

    def create(self, options: CreateOptions) -> MachineRecord:
        if self.store.exists(options.name):
            raise ValueError(f"machine [{options.name}] already exists")

        profile = load_profile(self.config, options.profile)
        shell_state_preexisted = (
            options.shell_state and self.shell_state.path_for(options.name).exists()
        )
        record = self._record_from_options(options, profile)
        runner = self._runner(record.runner)

        try:
            if record.shell_state.enabled:
                self.shell_state.ensure(record.name)
            _validate_runner_support(record, runner)
            _validate_image_source(record)
            runner.create(record)
        except Exception:
            _delete_created_shell_state(self.shell_state, record, shell_state_preexisted)
            raise

        try:
            self.store.save(record)
        except Exception:
            _try_delete_backend(runner, record)
            _delete_created_shell_state(self.shell_state, record, shell_state_preexisted)
            raise
        return record

    def start(self, name: str) -> RuntimeStatus:
        record = self.store.load(name)
        runner = self._runner(record.runner)
        return _ensure_running(record, runner)

    def stop(self, name: str) -> RuntimeStatus:
        record = self.store.load(name)
        runner = self._runner(record.runner)
        status = runner.inspect(record)
        if status.state is RuntimeState.RUNNING:
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

        shell_command = _split_shell(shell or record.shell or self.config.defaults.shell)
        exec_env = self.shell_state.env_for_shell(shell_command)
        workdir = _mapped_cwd(record.mounts) or record.workdir
        runner.exec(
            record,
            _exec_spec(
                ExecSpec(
                    command=shell_command,
                    interactive=True,
                    tty=True,
                    env=exec_env,
                    workdir=workdir,
                )
            ),
        )

    def exec(self, name: str, spec: ExecSpec) -> None:
        record = self.store.load(name)
        runner = self._runner(record.runner)
        _ensure_running(record, runner)
        runner.exec(record, _exec_spec(spec))

    def inspect(self, name: str) -> MachineView:
        record = self.store.load(name)
        return MachineView(
            record=record,
            status=self._runner(record.runner).inspect(record),
        )

    def list(self) -> list[MachineView]:
        rows: list[MachineView] = []
        for record in self.store.list():
            rows.append(
                MachineView(
                    record=record,
                    status=self._runner(record.runner).inspect(record),
                )
            )
        return rows

    def _runner(self, name: str) -> Runner:
        runner_name = validate_runner(name)
        return self.runners.get(runner_name) or get_runner(runner_name)

    def _record_from_options(
        self,
        options: CreateOptions,
        profile: Profile | None,
    ) -> MachineRecord:
        image = options.image or _profile_value(profile, "image")
        if image is None:
            raise ValueError("image is required")

        runner = validate_runner(
            options.runner
            or _profile_value(profile, "runner")
            or self.config.defaults.runner
        )
        network = _network_spec(options, profile, self.config.defaults.network)
        resources = _resource_spec(options, profile)
        mounts = _mounts(options, profile)
        if options.shell_state:
            mounts = (*mounts, self.shell_state.mount_for(options.name))

        spec = MachineSpec(
            name=options.name,
            image=ImageSource.from_cli(image),
            runner=runner,
            mounts=mounts,
            ports=_ports(options, profile),
            env=_env(options, profile),
            workdir=options.workdir or _profile_value(profile, "workdir"),
            shell=options.shell
            or _profile_value(profile, "shell")
            or self.config.defaults.shell,
            network=network,
            identity=options.identity
            or _profile_value(profile, "identity")
            or IdentitySpec(),
            resources=resources,
            shell_state=ShellStateSpec(enabled=options.shell_state),
            ssh_agent=_bool_option(options.ssh_agent, profile, "ssh_agent", False),
            gpu=_bool_option(options.gpu, profile, "gpu", False),
        )
        return MachineRecord.from_spec(spec, created_at=_now())


def get_data_dir() -> Path:
    path = Path(user_data_dir(APP_NAME))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _validate_runner_support(record: MachineRecord, runner: Runner) -> None:
    caps = runner.capabilities
    if record.image.kind not in caps.image_sources:
        supported = ", ".join(kind.value for kind in caps.image_sources)
        raise ValueError(
            f"runner [{runner.name}] does not support image source "
            f"[{record.image.kind.value}], expected one of: {supported}"
        )

    for mount in record.mounts:
        if mount.source.is_dir() and not caps.directory_mounts:
            raise ValueError(f"runner [{runner.name}] does not support directory mounts")
        if mount.source.is_file() and not caps.file_mounts:
            raise ValueError(f"runner [{runner.name}] does not support file mounts")

    if (
        record.image.kind is ImageSourceKind.OCI_REFERENCE
        and record.network.mode is NetworkMode.NONE
        and not caps.offline_oci_references
    ):
        raise ValueError(
            f"runner [{runner.name}] needs networking to start OCI references; "
            "enable networking or use a .smolmachine artifact"
        )
    if record.ports and not caps.published_ports:
        raise ValueError(f"runner [{runner.name}] does not support port publishing")
    if record.network.mode is NetworkMode.DEFAULT and not caps.outbound_network:
        raise ValueError(f"runner [{runner.name}] does not support outbound networking")
    if record.network.mode is NetworkMode.HOST and not caps.host_network:
        raise ValueError(f"runner [{runner.name}] does not support host networking")
    if (
        record.network.allow_hosts or record.network.allow_cidrs
    ) and not caps.restricted_network:
        raise ValueError(f"runner [{runner.name}] does not support restricted networking")
    if record.identity.mode is IdentityMode.ROOT and not caps.root_identity:
        raise ValueError(f"runner [{runner.name}] does not support root identity")
    if record.identity.mode is IdentityMode.HOST and not caps.host_identity:
        raise ValueError(f"runner [{runner.name}] does not support host identity")
    if record.ssh_agent and not caps.ssh_agent:
        raise ValueError(f"runner [{runner.name}] does not support SSH agent forwarding")
    if record.gpu and not caps.gpu_vulkan:
        raise ValueError(f"runner [{runner.name}] does not support Vulkan GPU forwarding")


def _validate_image_source(record: MachineRecord) -> None:
    if record.image.kind is ImageSourceKind.SMOLMACHINE:
        path = Path(record.image.value)
        if not path.is_file():
            raise ValueError(f".smolmachine file does not exist: {path}")


def _ensure_running(record: MachineRecord, runner: Runner) -> RuntimeStatus:
    status = runner.inspect(record)
    if status.state is RuntimeState.RUNNING:
        return status
    if status.state is RuntimeState.STOPPED:
        runner.start(record)
        return runner.inspect(record)
    if status.state is RuntimeState.MISSING:
        raise ValueError(
            f"machine [{record.name}] backend [{record.backend_id}] is missing; "
            "delete and recreate it"
        )
    raise ValueError(
        f"machine [{record.name}] backend [{record.backend_id}] state is unknown"
    )


def _delete_created_shell_state(
    shell_state: ShellStateManager,
    record: MachineRecord,
    preexisted: bool,
) -> None:
    if record.shell_state.enabled and not preexisted:
        shell_state.delete(record.name)


def _try_delete_backend(runner: Runner, record: MachineRecord) -> None:
    try:
        runner.delete(record)
    except Exception as exc:
        logger.warning(
            "failed to clean up backend [%s] after create failure: %s",
            record.backend_id,
            exc,
        )


def _mounts(options: CreateOptions, profile: Profile | None) -> tuple[MountSpec, ...]:
    workspace_specs = _tuple_profile_value(profile, "workspaces") + options.workspaces
    mount_specs = _tuple_profile_value(profile, "mounts") + options.mounts
    return tuple(
        [parse_workspace_spec(value) for value in workspace_specs]
        + [parse_mount_spec(value) for value in mount_specs]
    )


def _ports(options: CreateOptions, profile: Profile | None) -> tuple[PortBind, ...]:
    values = _tuple_profile_value(profile, "ports") + options.ports
    return tuple(parse_port_bind(value) for value in values)


def _env(options: CreateOptions, profile: Profile | None) -> tuple[str, ...]:
    values = _tuple_profile_value(profile, "env") + options.env
    return tuple(parse_env(value) for value in values)


def _exec_spec(spec: ExecSpec) -> ExecSpec:
    return ExecSpec(
        command=spec.command,
        interactive=spec.interactive,
        tty=spec.tty,
        env=tuple(parse_env(value) for value in spec.env),
        workdir=spec.workdir,
        stream=spec.stream,
    )


def _network_spec(
    options: CreateOptions,
    profile: Profile | None,
    default: NetworkMode,
) -> NetworkSpec:
    mode = options.network or _profile_value(profile, "network") or default
    if not isinstance(mode, NetworkMode):
        mode = parse_network_mode(str(mode))
    return NetworkSpec(
        mode=mode,
        allow_hosts=options.allow_hosts,
        allow_cidrs=options.allow_cidrs,
    )


def _resource_spec(options: CreateOptions, profile: Profile | None) -> ResourceSpec:
    return ResourceSpec(
        cpus=options.resources.cpus or _profile_value(profile, "cpus"),
        memory_mib=options.resources.memory_mib or _profile_value(profile, "memory"),
        storage_gib=options.resources.storage_gib or _profile_value(profile, "storage"),
        overlay_gib=options.resources.overlay_gib or _profile_value(profile, "overlay"),
    )


def _profile_value(profile: Profile | None, name: str):
    if profile is None:
        return None
    return getattr(profile, name)


def _tuple_profile_value(profile: Profile | None, name: str) -> tuple[str, ...]:
    if profile is None:
        return ()
    return getattr(profile, name)


def _bool_option(
    value: bool | None,
    profile: Profile | None,
    profile_name: str,
    default: bool,
) -> bool:
    if value is not None:
        return value
    profile_value = _profile_value(profile, profile_name)
    if profile_value is not None:
        return profile_value
    return default


def _split_shell(value: str) -> tuple[str, ...]:
    parts = tuple(shlex.split(value))
    if not parts:
        raise ValueError("shell cannot be empty")
    return parts


def _mapped_cwd(mounts: Iterable[MountSpec]) -> str | None:
    return map_host_path_to_guest(Path.cwd(), tuple(mounts))


def _now() -> str:
    return datetime.now(UTC).isoformat()
