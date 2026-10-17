from pathlib import Path

from support import RecordingProcessRunner

from mimchine.builders import PodmanBuilder
from mimchine.domain import BuildSpec


def test_podman_build_command(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Containerfile"
    context = tmp_path / "context"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    context.mkdir()
    runner = RecordingProcessRunner()

    PodmanBuilder(runner).build(
        BuildSpec(
            image="example:dev",
            file=dockerfile,
            context=context,
            platform="linux/amd64",
            build_args=("A=B",),
            no_cache=True,
        )
    )

    assert runner.calls == [
        (
            "podman",
            "build",
            "-f",
            str(dockerfile.resolve()),
            "-t",
            "example:dev",
            "--no-cache",
            "--platform",
            "linux/amd64",
            "--build-arg",
            "A=B",
            str(context.resolve()),
        ),
    ]
