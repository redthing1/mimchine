from __future__ import annotations

from os import terminal_size
from pathlib import Path

import pytest

from mimchine.builders import PodmanBuilder
from mimchine.domain import (
    BuildSpec,
    ExecSpec,
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
        self.cwd: list[str | None] = []
        self.returncode = returncode
        self.stdout = stdout

    def run(self, args, *, capture=False, foreground=False, check=True, cwd=None):
        command = tuple(str(arg) for arg in args)
        self.calls.append(command)
        self.cwd.append(None if cwd is None else str(cwd))
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


def test_podman_runner_create_relabels_shell_state_mount(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    runner = RecordingProcessRunner()
    record = MachineRecord.from_spec(
        MachineSpec(
            name="dev",
            image=ImageSource.oci_reference("fedora:latest"),
            runner="podman",
            mounts=(
                MountSpec(workspace, "/work/dev", kind="workspace"),
                MountSpec(state, "/mim/shell-state", kind="shell_state"),
            ),
            identity=IdentitySpec(IdentityMode.ROOT),
        ),
        created_at="2026-01-01T00:00:00+00:00",
    )

    PodmanRunner(runner).create(record)

    command = runner.calls[-1]
    assert f"{workspace.resolve()}:/work/dev:rw" in command
    assert f"{state.resolve()}:/mim/shell-state:rw,Z" in command


def test_podman_runner_create_passes_container_args_before_image() -> None:
    runner = RecordingProcessRunner()
    record = MachineRecord.from_spec(
        MachineSpec(
            name="gpu",
            image=ImageSource.oci_reference("fedora:latest"),
            runner="podman",
            identity=IdentitySpec(IdentityMode.ROOT),
            container_args=(
                "--security-opt=label=type:example.process",
                "--device=vendor.example/gpu=all",
                "--cap-drop=all",
            ),
        ),
        created_at="2026-01-01T00:00:00+00:00",
    )

    PodmanRunner(runner).create(record)

    command = runner.calls[0]
    image_index = command.index("fedora:latest")
    assert command[image_index - 3 : image_index] == (
        "--security-opt=label=type:example.process",
        "--device=vendor.example/gpu=all",
        "--cap-drop=all",
    )


def test_podman_runner_create_maps_image_identity_to_keep_id(tmp_path: Path) -> None:
    runner = RecordingProcessRunner(stdout="1001\n1002\n")
    record = MachineRecord.from_spec(
        MachineSpec(
            name="dev",
            image=ImageSource.oci_reference("example:dev"),
            runner="podman",
            mounts=(MountSpec(tmp_path, "/state"),),
        ),
        created_at="2026-01-01T00:00:00+00:00",
    )

    PodmanRunner(runner).create(record)

    assert runner.calls[0] == (
        "podman",
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "sh",
        "example:dev",
        "-lc",
        'printf "%s\\n%s\\n" "$(id -u)" "$(id -g)"',
    )
    assert runner.calls[1][:8] == (
        "podman",
        "create",
        "--name",
        "dev",
        "--label",
        "mimchine=1",
        "--userns",
        "keep-id:uid=1001,gid=1002",
    )


def test_podman_runner_rejects_invalid_image_identity_probe() -> None:
    runner = RecordingProcessRunner(stdout="user\ngroup\n")
    record = MachineRecord.from_spec(
        MachineSpec(
            name="dev",
            image=ImageSource.oci_reference("example:dev"),
            runner="podman",
        ),
        created_at="2026-01-01T00:00:00+00:00",
    )

    with pytest.raises(ValueError, match="invalid uid/gid"):
        PodmanRunner(runner).create(record)


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


def test_podman_runner_lifecycle_uses_neutral_host_cwd() -> None:
    runner = RecordingProcessRunner(stdout='[{"State":{"Running":true}}]')
    record = MachineRecord.from_spec(
        MachineSpec("dev", ImageSource.oci_reference("alpine"), "podman"),
        created_at="2026-01-01T00:00:00+00:00",
    )
    podman = PodmanRunner(runner)

    podman.start(record)
    podman.exec(record, ExecSpec(("pwd",)))
    podman.inspect(record)
    podman.stop(record)
    podman.delete(record)

    assert runner.cwd == ["/", "/", "/", "/", "/"]


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


def test_smolvm_runner_create_keeps_shell_state_mount_plain(tmp_path: Path) -> None:
    runner = RecordingProcessRunner()
    record = MachineRecord.from_spec(
        MachineSpec(
            name="vm",
            image=ImageSource.oci_reference("alpine"),
            runner="smolvm",
            mounts=(MountSpec(tmp_path, "/mim/shell-state", kind="shell_state"),),
        ),
        created_at="2026-01-01T00:00:00+00:00",
    )

    SmolvmRunner(runner).create(record)

    command = runner.calls[0]
    assert f"{tmp_path.resolve()}:/mim/shell-state:rw" in command
    assert f"{tmp_path.resolve()}:/mim/shell-state:rw,z" not in command
    assert f"{tmp_path.resolve()}:/mim/shell-state:rw,Z" not in command


def test_smolvm_runner_status_parses_not_running_before_running() -> None:
    runner = RecordingProcessRunner(stdout="Machine 'vm': not running\n")
    record = MachineRecord.from_spec(
        MachineSpec("vm", ImageSource.oci_reference("alpine"), "smolvm"),
        created_at="2026-01-01T00:00:00+00:00",
    )

    status = SmolvmRunner(runner).inspect(record)

    assert status.state is RuntimeState.STOPPED


def test_smolvm_runner_exec_sets_guest_tty_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "mimchine.runners.smolvm.get_terminal_size",
        lambda *, fallback: terminal_size((132, 43)),
    )
    runner = RecordingProcessRunner()
    record = MachineRecord.from_spec(
        MachineSpec("vm", ImageSource.oci_reference("alpine"), "smolvm"),
        created_at="2026-01-01T00:00:00+00:00",
    )

    SmolvmRunner(runner).exec(
        record,
        ExecSpec(("zsh", "-l"), interactive=True, tty=True),
    )

    assert runner.calls[0] == (
        "smolvm",
        "machine",
        "exec",
        "--name",
        "vm",
        "-i",
        "-t",
        "-e",
        "COLUMNS=132",
        "-e",
        "LINES=43",
        "--",
        "sh",
        "-lc",
        'stty cols "$1" rows "$2" 2>/dev/null || true; shift 2; exec "$@"',
        "mimchine-tty",
        "132",
        "43",
        "zsh",
        "-l",
    )
