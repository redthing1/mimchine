from __future__ import annotations

from pathlib import Path

from mimchine.builders import PodmanBuilder
from mimchine.domain import (
    BuildSpec,
    IdentityMode,
    IdentitySpec,
    ImageSource,
    MachineRecord,
    MachineSpec,
    MountSpec,
    NetworkMode,
    NetworkSpec,
    PortBind,
    ResourceSpec,
    RuntimeState,
)
from mimchine.process import ProcessResult
from mimchine.runners import DockerRunner, PodmanRunner, SmolvmRunner


class RecordingProcessRunner:
    def __init__(self, returncode: int = 0, stdout: str = "[]"):
        self.calls: list[tuple[str, ...]] = []
        self.returncode = returncode
        self.stdout = stdout

    def run(self, args, *, capture=False, foreground=False, check=True):
        command = tuple(str(arg) for arg in args)
        self.calls.append(command)
        return ProcessResult(command, self.returncode, self.stdout, "")


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
            builder="podman",
            platform="linux/amd64",
            build_args=("A=B",),
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
            "--platform",
            "linux/amd64",
            "--build-arg",
            "A=B",
            str(context.resolve()),
        ),
    ]


def test_podman_runner_create_uses_record_as_command_source(tmp_path: Path) -> None:
    runner = RecordingProcessRunner()
    record = MachineRecord.from_spec(
        MachineSpec(
            name="dev",
            image=ImageSource.oci_reference("fedora:latest"),
            runner="podman",
            mounts=(MountSpec(tmp_path, "/work/dev"),),
            ports=(PortBind(8080, 80),),
            env=("MODE=dev",),
            workdir="/work/dev",
            network=NetworkSpec(NetworkMode.NONE),
            identity=IdentitySpec(IdentityMode.ROOT),
        ),
        created_at="2026-01-01T00:00:00+00:00",
    )

    PodmanRunner(runner).create(record)

    command = runner.calls[0]
    assert command[:8] == (
        "podman",
        "create",
        "--name",
        "dev",
        "--label",
        "mimchine=1",
        "--user",
        "0:0",
    )
    assert "--network" in command
    assert "none" in command
    assert f"{tmp_path.resolve()}:/work/dev:rw" in command
    assert command[-4:] == (
        "fedora:latest",
        "sh",
        "-lc",
        "trap 'exit 0' TERM INT; while :; do sleep 3600 & wait $!; done",
    )


def test_docker_runner_host_identity_uses_uid_gid() -> None:
    assert DockerRunner(RecordingProcessRunner())._host_identity_args()[0] == "--user"


def test_container_runner_inspect_uses_backend_json_shape() -> None:
    podman_process = RecordingProcessRunner(stdout='[{"State":{"Running":true}}]')
    docker_process = RecordingProcessRunner(stdout='[{"State":{"Running":true}}]')
    record = MachineRecord.from_spec(
        MachineSpec("dev", ImageSource.oci_reference("alpine"), "podman"),
        created_at="2026-01-01T00:00:00+00:00",
    )

    PodmanRunner(podman_process).inspect(record)
    DockerRunner(docker_process).inspect(record)

    assert podman_process.calls[0] == ("podman", "inspect", "--format", "json", "dev")
    assert docker_process.calls[0] == ("docker", "inspect", "dev")


def test_smolvm_runner_create_maps_machine_flags(tmp_path: Path) -> None:
    runner = RecordingProcessRunner()
    record = MachineRecord.from_spec(
        MachineSpec(
            name="vm",
            image=ImageSource.smolmachine(tmp_path / "tool.smolmachine"),
            runner="smolvm",
            mounts=(MountSpec(tmp_path, "/work/vm"),),
            ports=(PortBind(18080, 8080),),
            env=("MODE=dev",),
            workdir="/work/vm",
            network=NetworkSpec(NetworkMode.DEFAULT, allow_cidrs=("10.0.0.0/8",)),
            resources=ResourceSpec(cpus=2, memory_mib=1024),
            ssh_agent=True,
            gpu=True,
        ),
        created_at="2026-01-01T00:00:00+00:00",
    )

    SmolvmRunner(runner).create(record)

    command = runner.calls[0]
    assert command[:5] == ("smolvm", "machine", "create", "vm", "--from")
    assert "--net" in command
    assert "--allow-cidr" in command
    assert "-v" in command
    assert "--ssh-agent" in command
    assert "--gpu" in command


def test_smolvm_runner_status_parses_not_running_before_running() -> None:
    runner = RecordingProcessRunner(stdout="Machine 'vm': not running\n")
    record = MachineRecord.from_spec(
        MachineSpec("vm", ImageSource.oci_reference("alpine"), "smolvm"),
        created_at="2026-01-01T00:00:00+00:00",
    )

    status = SmolvmRunner(runner).inspect(record)

    assert status.state is RuntimeState.STOPPED
