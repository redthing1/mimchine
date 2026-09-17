from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .lifecycle import KEEPALIVE_COMMAND
from ..domain import (
    ExecSpec,
    IdentityMode,
    MachineRecord,
    MountSpec,
    NetworkMode,
    RuntimeState,
)
from ..process import ProcessError, ProcessRunner
from ..gpu import podman_gpu_args


NEUTRAL_BACKEND_CWD = "/"


@dataclass(frozen=True)
class ImageIdentity:
    uid: int
    gid: int


class _ContainerRunner:
    name: str
    binary: str
    supports_gpu = False

    def __init__(self, runner: ProcessRunner):
        self.runner = runner

    def create(self, record: MachineRecord) -> None:
        args = [
            self.binary,
            "create",
            "--name",
            record.name,
            "--label",
            "mimchine=1",
        ]
        args.extend(self._identity_args(record))
        args.extend(self._network_args(record))
        if record.resources.cpus:
            args.extend(("--cpus", str(record.resources.cpus)))
        if record.resources.memory_mib:
            args.extend(("--memory", f"{record.resources.memory_mib}m"))
        for env in record.env:
            args.extend(["-e", env])
        if record.workdir:
            args.extend(["-w", record.workdir])
        for mount in record.mounts:
            args.extend(["-v", _container_volume_arg(mount)])
        for port in record.ports:
            args.extend(["-p", port.arg()])
        if record.ssh_agent:
            args.extend(self._ssh_agent_args())
        if record.gpu:
            args.extend(self._gpu_args())
        args.extend(record.container_args)
        args.extend([record.image, "sh", "-lc", KEEPALIVE_COMMAND])
        self.runner.run(args, foreground=True, discard_stdout=True)

    def start(self, record: MachineRecord) -> None:
        self.runner.run(
            [self.binary, "start", record.name],
            foreground=True,
            discard_stdout=True,
            cwd=NEUTRAL_BACKEND_CWD,
        )

    def stop(self, record: MachineRecord) -> None:
        self.runner.run(
            [self.binary, "stop", record.name],
            foreground=True,
            discard_stdout=True,
            cwd=NEUTRAL_BACKEND_CWD,
        )

    def delete(self, record: MachineRecord) -> None:
        self.runner.run(
            [self.binary, "rm", "-f", record.name],
            foreground=True,
            discard_stdout=True,
            check=False,
            cwd=NEUTRAL_BACKEND_CWD,
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
        args.append(record.name)
        args.extend(spec.command)
        self.runner.run(args, foreground=True, cwd=NEUTRAL_BACKEND_CWD)

    def inspect(self, record: MachineRecord) -> RuntimeState:
        result = self.runner.run(
            self._inspect_args(record.name),
            capture=True,
            check=False,
            cwd=NEUTRAL_BACKEND_CWD,
        )
        if result.returncode == 127:
            raise ProcessError(result)
        if result.returncode != 0:
            return RuntimeState.MISSING

        data = _parse_json_documents(result.stdout)
        state = _container_state(data[0]) if data else RuntimeState.UNKNOWN
        return state

    def _network_args(self, record: MachineRecord) -> list[str]:
        if record.network is NetworkMode.NONE:
            return ["--network", "none"]
        if record.network is NetworkMode.HOST:
            return ["--network", "host"]
        return []

    def _identity_args(self, record: MachineRecord) -> list[str]:
        if record.identity is IdentityMode.ROOT:
            return ["--user", "0:0"]
        if record.identity is IdentityMode.HOST:
            return self._host_identity_args()
        return self._image_identity_args(record)

    def _image_identity_args(self, record: MachineRecord) -> list[str]:
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

    def _gpu_args(self) -> tuple[str, ...]:
        raise ValueError(f"runner [{self.name}] does not support GPU forwarding")

    def _inspect_args(self, name: str) -> list[str]:
        return [self.binary, "inspect", name]


class PodmanRunner(_ContainerRunner):
    name = "podman"
    binary = "podman"
    supports_gpu = True

    def _gpu_args(self) -> tuple[str, ...]:
        return podman_gpu_args()

    def _image_identity_args(self, record: MachineRecord) -> list[str]:
        identity = self._resolve_image_identity(record.image)
        return ["--userns", f"keep-id:uid={identity.uid},gid={identity.gid}"]

    def _host_identity_args(self) -> list[str]:
        return ["--userns", "keep-id"]

    def _inspect_args(self, name: str) -> list[str]:
        return [self.binary, "inspect", "--format", "json", name]

    def _resolve_image_identity(self, image: str) -> ImageIdentity:
        result = self.runner.run(
            [
                self.binary,
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "sh",
                image,
                "-lc",
                'printf "%s\\n%s\\n" "$(id -u)" "$(id -g)"',
            ],
            capture=True,
        )
        return _parse_image_identity(image, result.stdout)


class DockerRunner(_ContainerRunner):
    name = "docker"
    binary = "docker"


def _container_volume_arg(mount: MountSpec) -> str:
    if mount.kind != "shell_state" or {"z", "Z"} & set(mount.options):
        return mount.volume_arg()
    return f"{mount.source}:{mount.target}:{mount.mode},Z"


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


def _parse_image_identity(image: str, text: str) -> ImageIdentity:
    lines = [line.strip() for line in text.splitlines()]
    if len(lines) != 2:
        raise ValueError(f"image [{image}] returned unexpected identity probe output")

    uid_text, gid_text = lines

    try:
        uid = int(uid_text)
        gid = int(gid_text)
    except ValueError as exc:
        raise ValueError(
            f"image [{image}] reported invalid uid/gid [{uid_text}:{gid_text}]"
        ) from exc

    if uid < 0 or gid < 0:
        raise ValueError(f"image [{image}] reported negative uid/gid [{uid}:{gid}]")

    return ImageIdentity(uid=uid, gid=gid)
