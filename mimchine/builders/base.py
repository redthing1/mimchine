from __future__ import annotations

from typing import Protocol

from ..config import validate_builder
from ..domain import BuildSpec
from ..process import ProcessRunner


class Builder(Protocol):
    name: str

    def build(self, spec: BuildSpec) -> None: ...


def get_builder(name: str, runner: ProcessRunner | None = None) -> Builder:
    from .containers import DockerBuilder, PodmanBuilder

    process_runner = runner or ProcessRunner()
    builder_name = validate_builder(name)
    if builder_name == "podman":
        return PodmanBuilder(process_runner)
    if builder_name == "docker":
        return DockerBuilder(process_runner)
    raise ValueError(f"unsupported builder [{name}]")
