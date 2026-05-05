from __future__ import annotations

import json
import os

from ..domain import (
    ExecSpec,
    IdentityMode,
    ImageSourceKind,
    MachineRecord,
    NetworkMode,
    RunnerCapabilities,
    RuntimeState,
    RuntimeStatus,
)
from ..process import ProcessRunner


KEEPALIVE_COMMAND = "trap 'exit 0' TERM INT; while :; do sleep 3600 & wait $!; done"


class _ContainerRunner:
    name: str
    binary: str

    capabilities = RunnerCapabilities(
        image_sources=(
            ImageSourceKind.OCI_REFERENCE,
        ),
        offline_oci_references=True,
        directory_mounts=True,
        file_mounts=True,
        published_ports=True,
        outbound_network=True,
        restricted_network=False,
        host_network=True,
        ssh_agent=True,
        gpu_vulkan=False,
        root_identity=True,
        host_identity=True,
    )

    def __init__(self, runner: ProcessRunner):
        self.runner = runner

    def create(self, record: MachineRecord) -> None:
        args = [
            self.binary,
            "create",
            "--name",
            record.backend_id,
            "--label",
            "mimchine=1",
        ]
        args.extend(self._identity_args(record))
        args.extend(self._network_args(record))
        for env in record.env:
            args.extend(["-e", env])
        if record.workdir:
            args.extend(["-w", record.workdir])
        for mount in record.mounts:
            args.extend(["-v", mount.volume_arg()])
        for port in record.ports:
            args.extend(["-p", port.arg()])
        if record.ssh_agent:
            args.extend(self._ssh_agent_args())
        args.extend([record.image.value, "sh", "-lc", KEEPALIVE_COMMAND])
        self.runner.run(args, foreground=True)

    def start(self, record: MachineRecord) -> None:
        self.runner.run([self.binary, "start", record.backend_id], foreground=True)

    def stop(self, record: MachineRecord) -> None:
        self.runner.run([self.binary, "stop", record.backend_id], foreground=True)

    def delete(self, record: MachineRecord) -> None:
        self.runner.run(
            [self.binary, "rm", "-f", record.backend_id],
            foreground=True,
            check=False,
        )

    def exec(self, record: MachineRecord, spec: ExecSpec) -> None:
        args = [self.binary, "exec"]
        if spec.interactive:
            args.append("-i")
        if spec.tty:
            args.append("-t")
        for env in spec.env:
            args.extend(["-e", env])
        if spec.workdir:
            args.extend(["-w", spec.workdir])
        args.append(record.backend_id)
        args.extend(spec.command)
        self.runner.run(args, foreground=True)

    def inspect(self, record: MachineRecord) -> RuntimeStatus:
        result = self.runner.run(
            self._inspect_args(record.backend_id),
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

        data = _parse_json_documents(result.stdout)
        state = _container_state(data[0]) if data else RuntimeState.UNKNOWN
        return RuntimeStatus(record.name, record.runner, record.backend_id, state)

    def _network_args(self, record: MachineRecord) -> list[str]:
        if record.network.mode is NetworkMode.NONE:
            return ["--network", "none"]
        if record.network.mode is NetworkMode.HOST:
            return ["--network", "host"]
        return []

    def _identity_args(self, record: MachineRecord) -> list[str]:
        if record.identity.mode is IdentityMode.ROOT:
            return ["--user", "0:0"]
        if record.identity.mode is IdentityMode.HOST:
            return self._host_identity_args()
        return []

    def _host_identity_args(self) -> list[str]:
        return ["--user", f"{os.getuid()}:{os.getgid()}"]

    def _ssh_agent_args(self) -> list[str]:
        host_socket = os.environ.get("SSH_AUTH_SOCK")
        if not host_socket:
            raise ValueError("SSH_AUTH_SOCK is not set")
        guest_socket = "/mim/ssh-agent.sock"
        return [
            "-v",
            f"{host_socket}:{guest_socket}:ro",
            "-e",
            f"SSH_AUTH_SOCK={guest_socket}",
        ]

    def _inspect_args(self, backend_id: str) -> list[str]:
        return [self.binary, "inspect", backend_id]


class PodmanRunner(_ContainerRunner):
    name = "podman"
    binary = "podman"

    def _host_identity_args(self) -> list[str]:
        return ["--userns", "keep-id"]

    def _inspect_args(self, backend_id: str) -> list[str]:
        return [self.binary, "inspect", "--format", "json", backend_id]


class DockerRunner(_ContainerRunner):
    name = "docker"
    binary = "docker"


def _parse_json_documents(text: str) -> list[dict[str, object]]:
    stripped = text.strip()
    if not stripped:
        return []
    data = json.loads(stripped)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _container_state(data: dict[str, object]) -> RuntimeState:
    state = data.get("State")
    if isinstance(state, dict):
        if state.get("Running") is True:
            return RuntimeState.RUNNING
        return RuntimeState.STOPPED
    return RuntimeState.UNKNOWN
