from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace

from mimchine.domain import (
    ContainerAllocation,
    EngineContext,
    Machine,
    MachineBinding,
    MachineObservation,
    MachineSpec,
    MissingContainer,
    Operation,
    RuntimeState,
)
from mimchine.process import ProcessError, ProcessResult

CID = "a" * 64
IMAGE_ID = "sha256:" + "b" * 64
MACHINE_ID = "00000000-0000-4000-8000-000000000001"
PODMAN_INFO = {
    "host": {
        "hostname": "test-host",
        "security": {"rootless": True},
        "idMappings": {},
        "cgroupVersion": "v2",
        "cgroupManager": "systemd",
        "ociRuntime": {"name": "crun"},
    },
    "store": {"graphRoot": "/test/storage", "runRoot": "/test/run"},
}
PODMAN_CONTEXT = {
    "hostname": "test-host",
    "storage": "/test/storage",
    "run_root": "/test/run",
    "uid_map": "0 1000 1",
    "gid_map": "0 1000 1",
}
DOCKER_INFO = {
    "ID": "test-daemon",
    "DockerRootDir": "/test/docker",
    "DefaultRuntime": "runc",
}


def context(engine="podman"):
    fields = dict(PODMAN_CONTEXT, client_uid=str(os.getuid()), transport="local")
    if engine == "docker":
        fields = {"daemon_id": "test-daemon", "storage": "/test/docker"}
    return EngineContext(engine, tuple(fields.items()))


def make_machine(name="dev", image="alpine", engine="podman", **kwargs):
    spec = MachineSpec(name, image, engine, **kwargs)
    allocation = ContainerAllocation(
        CID, context(engine), (("mimchine", "1"), ("mimchine.id", MACHINE_ID))
    )
    binding = MachineBinding(
        allocation, IMAGE_ID, "crun" if engine == "podman" else "runc"
    )
    return Machine(
        MACHINE_ID, spec, "2026-01-01T00:00:00+00:00", Operation.READY, binding=binding
    )


def configuration(spec: MachineSpec) -> str:
    return json.dumps(
        {
            "mounts": [
                {
                    "Source": str(m.source),
                    "Destination": m.target,
                    "RW": not m.read_only,
                }
                for m in spec.mounts
            ],
            "ports": {
                f"{p.guest}/{p.protocol}": [
                    {"HostPort": str(p.host), "HostIp": p.address or ""}
                ]
                for p in spec.ports
            },
            "network": spec.network.value,
            "memory": None
            if spec.resources.memory_mib is None
            else spec.resources.memory_mib * 1024 * 1024,
            "cpu_quota": None
            if spec.resources.cpus is None
            else spec.resources.cpus * 100000,
            "cpu_period": 100000,
        },
        sort_keys=True,
    )


@dataclass
class FakeEngine:
    name: str = "podman"
    state: RuntimeState = RuntimeState.STOPPED
    start_state: RuntimeState = RuntimeState.RUNNING
    create_error: Exception | None = None
    delete_error: Exception | None = None
    observation_error: Exception | None = None
    created: list = field(default_factory=list)
    started: list = field(default_factory=list)
    execs: list = field(default_factory=list)
    deleted: list = field(default_factory=list)
    inspected: list = field(default_factory=list)
    observation: MachineObservation | None = None
    context_override: EngineContext | None = None

    def context(self):
        return self.context_override or (
            self.observation.context if self.observation else context(self.name)
        )

    def allocate(self, machine):
        if self.create_error:
            raise self.create_error
        self.created.append(machine)
        target = ContainerAllocation(
            CID, context(self.name), (("mimchine", "1"), ("mimchine.id", machine.id))
        )
        self.observation = MachineObservation(
            CID,
            target.context,
            IMAGE_ID,
            "crun" if self.name == "podman" else "runc",
            target.labels,
            self.state,
            configuration(machine.spec),
        )
        return target

    def observe(self, target):
        self.inspected.append(target)
        if self.observation_error:
            raise self.observation_error
        if self.state is RuntimeState.MISSING or self.observation is None:
            return MissingContainer(self.context())
        return replace(self.observation, state=self.state)

    def start(self, machine):
        self.started.append(machine)
        self.state = self.start_state

    def stop(self, machine):
        self.state = RuntimeState.STOPPED

    def delete(self, machine):
        self.deleted.append(machine)
        if self.delete_error:
            raise self.delete_error
        self.state = RuntimeState.MISSING

    def exec(self, machine, spec):
        self.execs.append((machine, spec))


class RecordingProcessRunner:
    def __init__(self, returncode=0, stdout=None, stderr="", *, engine="podman"):
        self.calls, self.cwd, self.discard_stdout = [], [], []
        self.returncode, self.stdout, self.stderr, self.engine = (
            returncode,
            stdout,
            stderr,
            engine,
        )
        self.inspect_stdout = None
        self.info = PODMAN_INFO if engine == "podman" else DOCKER_INFO
        self.exists = True

    def run(
        self,
        args,
        *,
        capture=False,
        foreground=False,
        discard_stdout=False,
        check=True,
        cwd=None,
    ):
        args = tuple(str(a) for a in args)
        self.calls.append(args)
        self.cwd.append(None if cwd is None else str(cwd))
        self.discard_stdout.append(discard_stdout)
        code, output = self.returncode, self.stdout or ""
        if "unshare" in args:
            output = json.dumps(PODMAN_CONTEXT)
        elif "info" in args:
            output = json.dumps(self.info)
        elif "inspect" in args:
            output = (
                self.inspect_stdout
                if self.inspect_stdout is not None
                else json.dumps(
                    [
                        {
                            "Id": CID,
                            "Image": IMAGE_ID,
                            "OCIRuntime": "crun",
                            "HostConfig": {"Runtime": "runc"},
                            "State": {"Running": True},
                            "Config": {
                                "Labels": {"mimchine": "1", "mimchine.id": MACHINE_ID}
                            },
                        }
                    ]
                )
            )
        elif "exists" in args:
            code = 0 if self.exists else 1
        elif "ls" in args:
            output = (
                (CID if any(arg.startswith("id=") for arg in args) else "dev")
                if self.exists
                else ""
            )
        elif "create" in args:
            output = CID
        elif "run" in args or "id -u; id -g" in args:
            output = self.stdout if self.stdout is not None else "1001\n1002\n"
        result = ProcessResult(args, code, output, self.stderr)
        if check and code:
            raise ProcessError(result)
        return result
