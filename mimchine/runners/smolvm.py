from __future__ import annotations

from shutil import get_terminal_size

from ..domain import (
    ExecSpec,
    ImageSourceKind,
    MachineRecord,
    NetworkMode,
    RunnerCapabilities,
    RuntimeState,
    RuntimeStatus,
)
from ..process import ProcessRunner


class SmolvmRunner:
    name = "smolvm"
    capabilities = RunnerCapabilities(
        image_sources=(
            ImageSourceKind.OCI_REFERENCE,
            ImageSourceKind.SMOLMACHINE,
        ),
        offline_oci_references=True,
        directory_mounts=True,
        file_mounts=False,
        published_ports=True,
        outbound_network=True,
        restricted_network=True,
        host_network=False,
        ssh_agent=True,
        gpu_vulkan=True,
        root_identity=False,
        host_identity=False,
    )

    def __init__(self, runner: ProcessRunner):
        self.runner = runner

    def create(self, record: MachineRecord) -> None:
        args = ["smolvm", "machine", "create", record.backend_id]
        if record.image.kind is ImageSourceKind.SMOLMACHINE:
            args.extend(["--from", record.image.value])
        elif record.image.kind is ImageSourceKind.OCI_REFERENCE:
            args.extend(["--image", record.image.value])
        else:
            raise ValueError(f"smolvm cannot run image source [{record.image.kind.value}]")

        args.extend(_resource_args(record))
        args.extend(_network_args(record))
        for env in record.env:
            args.extend(["-e", env])
        if record.workdir:
            args.extend(["-w", record.workdir])
        for mount in record.mounts:
            args.extend(["-v", mount.volume_arg()])
        for port in record.ports:
            args.extend(["-p", port.arg()])
        if record.ssh_agent:
            args.append("--ssh-agent")
        if record.gpu:
            args.append("--gpu")

        self.runner.run(args, foreground=True)

    def start(self, record: MachineRecord) -> None:
        self.runner.run(
            ["smolvm", "machine", "start", "--name", record.backend_id],
            foreground=True,
        )

    def stop(self, record: MachineRecord) -> None:
        self.runner.run(
            ["smolvm", "machine", "stop", "--name", record.backend_id],
            foreground=True,
        )

    def delete(self, record: MachineRecord) -> None:
        self.runner.run(
            ["smolvm", "machine", "delete", record.backend_id, "-f"],
            foreground=True,
            check=False,
        )

    def exec(self, record: MachineRecord, spec: ExecSpec) -> None:
        args = ["smolvm", "machine", "exec", "--name", record.backend_id]
        if spec.interactive:
            args.append("-i")
        if spec.tty:
            args.append("-t")
        if spec.stream:
            args.append("--stream")
        command = spec.command
        env_vars = spec.env
        if spec.tty:
            size = _terminal_size(env_vars)
            env_vars = _terminal_size_env(env_vars, size)
            command = _terminal_size_command(command, size)
        for env_var in env_vars:
            args.extend(["-e", env_var])
        if spec.workdir:
            args.extend(["-w", spec.workdir])
        args.append("--")
        args.extend(command)
        self.runner.run(args, foreground=True)

    def inspect(self, record: MachineRecord) -> RuntimeStatus:
        result = self.runner.run(
            ["smolvm", "machine", "status", "--name", record.backend_id],
            capture=True,
            check=False,
        )
        if result.returncode != 0:
            return RuntimeStatus(
                record.name,
                record.runner,
                record.backend_id,
                RuntimeState.MISSING,
                result.stderr.strip(),
        )

        text = result.stdout.lower()
        if "not running" in text or "stopped" in text:
            state = RuntimeState.STOPPED
        elif "running" in text:
            state = RuntimeState.RUNNING
        else:
            state = RuntimeState.UNKNOWN
        return RuntimeStatus(
            record.name,
            record.runner,
            record.backend_id,
            state,
            result.stdout.strip(),
        )


def _network_args(record: MachineRecord) -> list[str]:
    if record.network.mode is NetworkMode.HOST:
        raise ValueError("smolvm does not support host networking")
    args: list[str] = []
    if (
        record.network.mode is NetworkMode.DEFAULT
        or record.network.allow_hosts
        or record.network.allow_cidrs
    ):
        args.append("--net")
    for cidr in record.network.allow_cidrs:
        args.extend(["--allow-cidr", cidr])
    for host in record.network.allow_hosts:
        args.extend(["--allow-host", host])
    return args


def _resource_args(record: MachineRecord) -> list[str]:
    resources = record.resources
    args: list[str] = []
    if resources.cpus is not None:
        args.extend(["--cpus", str(resources.cpus)])
    if resources.memory_mib is not None:
        args.extend(["--mem", str(resources.memory_mib)])
    if resources.storage_gib is not None:
        args.extend(["--storage", str(resources.storage_gib)])
    if resources.overlay_gib is not None:
        args.extend(["--overlay", str(resources.overlay_gib)])
    return args


def _terminal_size(env: tuple[str, ...]) -> tuple[int, int]:
    fallback = get_terminal_size(fallback=(80, 24))
    columns = _positive_env_int(env, "COLUMNS") or max(fallback.columns, 1)
    lines = _positive_env_int(env, "LINES") or max(fallback.lines, 1)
    return columns, lines


def _terminal_size_env(
    env: tuple[str, ...],
    size: tuple[int, int],
) -> tuple[str, ...]:
    columns, lines = size
    additions: list[str] = []
    if _env_value(env, "COLUMNS") is None:
        additions.append(f"COLUMNS={columns}")
    if _env_value(env, "LINES") is None:
        additions.append(f"LINES={lines}")
    return (*env, *additions)


def _terminal_size_command(
    command: tuple[str, ...],
    size: tuple[int, int],
) -> tuple[str, ...]:
    columns, lines = size
    return (
        "sh",
        "-lc",
        'stty cols "$1" rows "$2" 2>/dev/null || true; shift 2; exec "$@"',
        "mimchine-tty",
        str(columns),
        str(lines),
        *command,
    )


def _positive_env_int(env: tuple[str, ...], key: str) -> int | None:
    value = _env_value(env, key)
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _env_value(env: tuple[str, ...], key: str) -> str | None:
    prefix = f"{key}="
    for item in env:
        if item.startswith(prefix):
            return item[len(prefix) :]
    return None
