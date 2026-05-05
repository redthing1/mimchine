from __future__ import annotations

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
        offline_oci_references=False,
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
        for env in spec.env:
            args.extend(["-e", env])
        if spec.workdir:
            args.extend(["-w", spec.workdir])
        args.append("--")
        args.extend(spec.command)
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
