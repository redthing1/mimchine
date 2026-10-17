from __future__ import annotations

from typing import Protocol

from ..domain import (
    ContainerAllocation,
    EngineContext,
    ExecSpec,
    Machine,
    MachineObservation,
    MissingContainer,
)
from ..process import ProcessRunner


class Engine(Protocol):
    name: str

    def allocate(self, machine: Machine) -> ContainerAllocation: ...
    def context(self) -> EngineContext: ...
    def observe(self, container_id: str) -> MachineObservation | MissingContainer: ...
    def start(self, machine: Machine) -> None: ...
    def stop(self, machine: Machine) -> None: ...
    def delete(self, machine: Machine) -> None: ...
    def exec(self, machine: Machine, spec: ExecSpec) -> None: ...


def get_engine(name: str, process: ProcessRunner | None = None) -> Engine:
    from .containers import DockerEngine, PodmanEngine

    if name == "podman":
        return PodmanEngine(process or ProcessRunner())
    if name == "docker":
        return DockerEngine(process or ProcessRunner())
    raise ValueError(f"unsupported engine: {name}")
