from __future__ import annotations

from ..domain import BuildSpec
from ..process import ProcessRunner


class _ContainerBuilder:
    name: str
    binary: str

    def __init__(self, runner: ProcessRunner):
        self.runner = runner

    def build(self, spec: BuildSpec) -> None:
        args = [
            self.binary,
            "build",
            "-f",
            str(spec.file),
            "-t",
            spec.image,
        ]
        if spec.platform:
            args.extend(["--platform", spec.platform])
        for build_arg in spec.build_args:
            args.extend(["--build-arg", build_arg])
        args.append(str(spec.context))
        self.runner.run(args, foreground=True)


class PodmanBuilder(_ContainerBuilder):
    name = "podman"
    binary = "podman"


class DockerBuilder(_ContainerBuilder):
    name = "docker"
    binary = "docker"
