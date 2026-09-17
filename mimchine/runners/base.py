from __future__ import annotations

from typing import Protocol

from ..config import validate_runner
from ..domain import ExecSpec, MachineRecord, RuntimeState
from ..process import ProcessRunner


class Runner(Protocol):
    name: str
    supports_gpu: bool

    def create(self, record: MachineRecord) -> None: ...

    def start(self, record: MachineRecord) -> None: ...

    def stop(self, record: MachineRecord) -> None: ...

    def delete(self, record: MachineRecord) -> None: ...

    def exec(self, record: MachineRecord, spec: ExecSpec) -> None: ...

    def inspect(self, record: MachineRecord) -> RuntimeState: ...


def get_runner(name: str, runner: ProcessRunner | None = None) -> Runner:
    from .containers import DockerRunner, PodmanRunner

    process_runner = runner or ProcessRunner()
    runner_name = validate_runner(name)
    if runner_name == "podman":
        return PodmanRunner(process_runner)
    if runner_name == "docker":
        return DockerRunner(process_runner)
    raise ValueError(f"unsupported runner [{name}]")
