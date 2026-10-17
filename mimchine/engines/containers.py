from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass

from ..domain import (
    ContainerAllocation,
    EngineContext,
    ExecSpec,
    IdentityMode,
    IsolationMode,
    Machine,
    MachineObservation,
    MachineSpec,
    MissingContainer,
    Operation,
    RuntimeState,
    verify_observation,
)
from ..gpu import podman_gpu_args
from ..isolation import GVISOR_NOT_QUALIFIED
from ..job_limits import HELPER_PATH, SETUP_SCRIPT, helper_mount_source, require_host
from ..process import ProcessError, ProcessRunner
from .commands import create_command, validate_container_args

NEUTRAL_BACKEND_CWD = "/"


@dataclass(frozen=True)
class ImageIdentity:
    uid: int
    gid: int


class _ContainerEngine:
    name: str
    binary: str

    def __init__(self, process: ProcessRunner):
        self.process = process

    @property
    def prefix(self) -> tuple[str, ...]:
        # A Linux Podman binding is explicitly local, never redirected by a
        # default connection or CONTAINER_HOST. Docker retains daemon identity.
        if self.name == "podman" and sys.platform == "linux":
            return (self.binary, "--remote=false")
        return (self.binary,)

    def _info(self) -> dict:
        template = "json" if self.name == "podman" else "{{json .}}"
        result = self.process.run(
            [*self.prefix, "info", "--format", template],
            capture=True,
            cwd=NEUTRAL_BACKEND_CWD,
        )
        return _object(json.loads(result.stdout), f"{self.name} info response")

    def allocate(self, machine: Machine) -> ContainerAllocation:
        spec = machine.spec
        if spec.isolation is not IsolationMode.NATIVE:
            raise ValueError(GVISOR_NOT_QUALIFIED)
        validate_container_args(spec.container_args)
        context = self.context()
        if machine.creation_context is not None and machine.creation_context != context:
            raise ValueError("engine context changed during container allocation")
        job_args = self._job_limits_args(spec) if spec.job_limits else ()
        identity = tuple(self._identity_args(spec))
        features = tuple(self._ssh_agent_args()) if spec.ssh_agent else ()
        if spec.gpu:
            features += tuple(self._gpu_args())
        labels = (("mimchine", "1"), ("mimchine.id", machine.id))
        command = create_command(
            spec,
            prefix=self.prefix,
            identity_args=identity,
            engine_args=("--tz=local", "--pids-limit=-1")
            if self.name == "podman"
            else (),
            feature_args=(*features, *job_args),
            labels=(("mimchine.id", machine.id),),
        )
        result = self.process.run(command, capture=True, cwd=NEUTRAL_BACKEND_CWD)
        return ContainerAllocation(result.stdout.strip(), context, labels)

    def context(self) -> EngineContext:
        raise NotImplementedError

    def observe(self, container_id: str) -> MachineObservation | MissingContainer:
        context = self.context()
        if self.name == "docker" and not self._exists(container_id):
            return MissingContainer(context)
        result = self.process.run(
            [*self.prefix, "container", "inspect", container_id],
            capture=True,
            check=False,
            cwd=NEUTRAL_BACKEND_CWD,
        )
        if result.returncode:
            if (
                self.name == "docker"
                or result.returncode == 127
                or self._exists(container_id)
            ):
                raise ProcessError(result)
            return MissingContainer(context)
        data = json.loads(result.stdout)
        if (
            not isinstance(data, list)
            or len(data) != 1
            or not isinstance(data[0], dict)
        ):
            raise ValueError(f"invalid {self.name} container inspection response")
        item = data[0]
        state = _object(item.get("State"), "container state")
        host_config = _object(item.get("HostConfig", {}), "container host config")
        config = _object(item.get("Config"), "container config")
        running = state.get("Running")
        if not isinstance(running, bool):
            raise ValueError("container inspection omitted running state")
        runtime = (
            item.get("OCIRuntime")
            if self.name == "podman"
            else host_config.get("Runtime")
        )
        image = item.get("Image")
        actual_id = item.get("Id", item.get("ID"))
        if not all(
            isinstance(value, str) and value for value in (runtime, image, actual_id)
        ):
            raise ValueError("container inspection omitted ID, image or runtime")
        labels = config.get("Labels")
        labels = _object({} if labels is None else labels, "container ownership labels")
        if not all(
            isinstance(k, str) and isinstance(v, str) for k, v in labels.items()
        ):
            raise ValueError("container ownership labels must contain text")
        observed_state = RuntimeState.RUNNING if running else RuntimeState.STOPPED
        if state.get("Paused") or state.get("Restarting") or state.get("Dead"):
            observed_state = RuntimeState.UNKNOWN
        configuration = {
            "mounts": item.get("Mounts", []),
            "ports": host_config.get("PortBindings") or {},
            "network": host_config.get("NetworkMode"),
            "memory": host_config.get("Memory"),
            "cpu_quota": host_config.get("CpuQuota"),
            "cpu_period": host_config.get("CpuPeriod"),
            "nano_cpus": host_config.get("NanoCpus"),
            "user": config.get("User"),
        }
        return MachineObservation(
            actual_id,
            context,
            image,
            runtime,
            tuple(sorted(labels.items())),
            observed_state,
            json.dumps(configuration, sort_keys=True),
        )

    def _exists(self, container_id: str) -> bool:
        if self.name == "podman":
            result = self.process.run(
                [*self.prefix, "container", "exists", container_id],
                capture=True,
                check=False,
                cwd=NEUTRAL_BACKEND_CWD,
            )
            if result.returncode not in (0, 1):
                raise ProcessError(result)
            return result.returncode == 0
        # Migration and unfinished-allocation recovery can query one name.
        # Normal lifecycle queries always use the full bound ID.
        by_id = re.fullmatch(r"[a-f0-9]{64}", container_id) is not None
        result = self.process.run(
            [
                *self.prefix,
                "container",
                "ls",
                "--all",
                "--no-trunc",
                "--filter",
                f"{'id' if by_id else 'name'}={container_id}",
                "--format",
                "{{.ID}}" if by_id else "{{.Names}}",
            ],
            capture=True,
            cwd=NEUTRAL_BACKEND_CWD,
        )
        if by_id:
            return container_id in result.stdout.splitlines()
        return any(
            container_id == name.strip().lstrip("/")
            for line in result.stdout.splitlines()
            for name in line.split(",")
        )

    def _target(self, machine: Machine, *, payload: bool = False) -> str:
        if payload and machine.spec.isolation is not IsolationMode.NATIVE:
            raise ValueError(GVISOR_NOT_QUALIFIED)
        target = machine.target
        if target is None:
            raise ValueError("machine has no captured container ID; recover it first")
        if payload:
            if machine.operation is not Operation.READY:
                raise ValueError("payload execution requires a ready machine")
            if machine.binding is None:
                raise ValueError("payload execution requires a completed binding")
            observed = verify_observation(
                self.observe(target.container_id), target, machine.binding
            )
            if observed is None:
                raise ValueError("bound container is missing")
        elif self.context() != target.context:
            raise ValueError("engine context does not match the machine binding")
        return target.container_id

    def start(self, machine: Machine) -> None:
        target = self._target(machine, payload=True)
        self.process.run(
            [*self.prefix, "start", target],
            foreground=True,
            discard_stdout=True,
            cwd=NEUTRAL_BACKEND_CWD,
        )

    def stop(self, machine: Machine) -> None:
        self.process.run(
            [*self.prefix, "stop", self._target(machine)],
            foreground=True,
            discard_stdout=True,
            cwd=NEUTRAL_BACKEND_CWD,
        )

    def delete(self, machine: Machine) -> None:
        self.process.run(
            [*self.prefix, "rm", "-f", self._target(machine)],
            foreground=True,
            discard_stdout=True,
            cwd=NEUTRAL_BACKEND_CWD,
        )

    def exec(self, machine: Machine, spec: ExecSpec) -> None:
        if spec.memory_mib is not None and not machine.spec.job_limits:
            raise ValueError("command memory limits require creation with --job-limits")
        target = self._target(machine, payload=True)
        # Preparation is idempotent and belongs to dispatch, not startup:
        # an already-running or adopted container may never have completed it.
        if machine.spec.job_limits:
            self._prepare_jobs(machine)
        args = [*self.prefix, "exec"]
        if spec.interactive:
            args.append("-i")
        if spec.tty:
            args.append("-t")
        for env in spec.env:
            args.extend(("-e", env))
        if spec.workdir:
            args.extend(("-w", spec.workdir))
        args.append(target)
        command = spec.command
        if spec.memory_mib is not None:
            command = (HELPER_PATH, "--memory", str(spec.memory_mib), "--", *command)
        if machine.spec.job_limits:
            command = (
                "sh",
                "-c",
                'export PATH="/mim/bin:$PATH"; exec "$@"',
                "mim-exec",
                *command,
            )
        self.process.run([*args, *command], foreground=True, cwd=NEUTRAL_BACKEND_CWD)

    def _identity_args(self, spec: MachineSpec) -> list[str]:
        if spec.identity is IdentityMode.ROOT:
            return ["--user", "0:0"]
        if spec.identity is IdentityMode.HOST:
            return self._host_identity_args()
        return self._image_identity_args(spec)

    def _image_identity_args(self, spec: MachineSpec) -> list[str]:
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

    def _job_limits_args(self, spec: MachineSpec) -> tuple[str, ...]:
        raise ValueError(f"runner [{self.name}] does not support job limits")

    def _prepare_jobs(self, machine: Machine) -> None:
        raise ValueError(f"runner [{self.name}] does not support job limits")


class PodmanEngine(_ContainerEngine):
    name = "podman"
    binary = "podman"

    def context(self) -> EngineContext:
        if sys.platform != "linux":
            raise ValueError(
                "Podman bindings require local Linux; use Docker for macOS development"
            )
        probe = (
            "import json,os,pathlib; "
            "print(json.dumps(dict(hostname=os.uname().nodename, "
            "storage=os.environ['CONTAINERS_GRAPHROOT'], "
            "run_root=os.environ['CONTAINERS_RUNROOT'], "
            "uid_map=' '.join(pathlib.Path('/proc/self/uid_map').read_text().split()), "
            "gid_map=' '.join(pathlib.Path('/proc/self/gid_map').read_text().split()))))"
        )
        result = self.process.run(
            [*self.prefix, "unshare", "--", sys.executable, "-I", "-c", probe],
            capture=True,
            cwd=NEUTRAL_BACKEND_CWD,
        )
        data = _object(json.loads(result.stdout), "Podman context response")
        identity = {
            key: data.get(key, "")
            for key in ("hostname", "storage", "run_root", "uid_map", "gid_map")
        }
        identity.update(client_uid=str(os.getuid()), transport="local")
        return EngineContext(self.name, tuple(identity.items()))

    def _gpu_args(self) -> tuple[str, ...]:
        return podman_gpu_args()

    def _job_limits_args(self, spec: MachineSpec) -> tuple[str, ...]:
        conflicting = (
            "--cgroupns",
            "--cgroups",
            "--cgroup-parent",
            "--runtime",
            "--annotation",
        )
        if any(arg.split("=", 1)[0] in conflicting for arg in spec.container_args):
            raise ValueError(
                "custom cgroup/runtime annotations cannot be combined with --job-limits"
            )
        require_host(self._info())
        source = helper_mount_source()
        return (
            "--cgroupns=private",
            "--security-opt=unmask=/sys/fs/cgroup",
            "--annotation=run.oci.systemd.subgroup=container",
            "--annotation=run.oci.delegate-cgroup=payload",
            "-v",
            f"{source}:{HELPER_PATH}:ro,z",
        )

    def _prepare_jobs(self, machine: Machine) -> None:
        result = self.process.run(
            [*self.prefix, "exec", self._target(machine), "sh", "-c", "id -u; id -g"],
            capture=True,
            cwd=NEUTRAL_BACKEND_CWD,
        )
        identity = _parse_image_identity(machine.spec.image, result.stdout)
        self.process.run(
            [
                *self.prefix,
                "exec",
                "--user",
                "0:0",
                self._target(machine),
                "sh",
                "-c",
                SETUP_SCRIPT,
                "mim-job-setup",
                str(identity.uid),
                str(identity.gid),
            ],
            foreground=True,
            cwd=NEUTRAL_BACKEND_CWD,
        )

    def _image_identity_args(self, spec: MachineSpec) -> list[str]:
        identity = self._resolve_image_identity(spec.image)
        return ["--userns", f"keep-id:uid={identity.uid},gid={identity.gid}"]

    def _host_identity_args(self) -> list[str]:
        return ["--userns", "keep-id"]

    def _resolve_image_identity(self, image: str) -> ImageIdentity:
        result = self.process.run(
            [
                *self.prefix,
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "sh",
                "--",
                image,
                "-lc",
                'printf "%s\\n%s\\n" "$(id -u)" "$(id -g)"',
            ],
            capture=True,
            cwd=NEUTRAL_BACKEND_CWD,
        )
        return _parse_image_identity(image, result.stdout)


class DockerEngine(_ContainerEngine):
    name = "docker"
    binary = "docker"

    def context(self) -> EngineContext:
        info = self._info()
        return EngineContext(
            self.name,
            (
                ("daemon_id", info.get("ID", "")),
                ("storage", info.get("DockerRootDir", "")),
            ),
        )


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


def _object(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"invalid {label}: expected an object")
    return value
