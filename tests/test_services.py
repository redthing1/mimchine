from __future__ import annotations

from pathlib import Path

import pytest
from support import FakeEngine

from mimchine.config import AppConfig, Defaults
from mimchine.domain import (
    ExecSpec,
    IdentityMode,
    NetworkMode,
    Operation,
    ResourceSpec,
    RuntimeState,
)
from mimchine.services import CreateOptions, MachineService
from mimchine.shell_state import ShellStateManager
from mimchine.shells import AUTO_ENTER_SHELL_COMMAND
from mimchine.state import MachineStore


@pytest.fixture(autouse=True)
def clear_host_terminal_environment(monkeypatch) -> None:
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("COLORTERM", raising=False)


def test_create_merges_profile_and_cli_into_record(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = FakeEngine()
    service = _service(
        tmp_path,
        runner,
        profiles={
            "dev": {
                "image": "fedora:latest",
                "runner": "podman",
                "workspace": str(workspace),
                "env": ["PROFILE=1"],
                "network": "none",
                "identity": "host",
                "shell": "bash -l",
                "container_args": ["--device=vendor.example/gpu=all"],
            }
        },
    )

    record = service.create(
        CreateOptions(
            name="dev",
            profile="dev",
            env=("CLI=1",),
            ports=("8080:80",),
            container_args=("--cap-drop=all",),
        )
    )

    assert [m.id for m in runner.created] == [record.id]
    assert service.store.load("dev") == record
    assert record.spec.image == "fedora:latest"
    assert record.spec.network is NetworkMode.NONE
    assert record.spec.identity is IdentityMode.HOST
    assert record.spec.env == ("PROFILE=1", "CLI=1")
    assert record.spec.container_args == (
        "--device=vendor.example/gpu=all",
        "--cap-drop=all",
    )
    assert [mount.kind for mount in record.spec.mounts] == ["workspace", "shell_state"]
    assert record.spec.shell == "bash -l"


def test_explicit_empty_image_does_not_fall_back_to_profile(tmp_path: Path) -> None:
    runner = FakeEngine()
    service = _service(
        tmp_path,
        runner,
        profiles={"dev": {"image": "alpine"}},
    )

    with pytest.raises(ValueError, match="image cannot be empty"):
        service.create(CreateOptions(name="dev", image=" ", profile="dev"))

    assert runner.created == []


def test_create_merges_resource_defaults_profile_and_cli(tmp_path: Path) -> None:
    runner = FakeEngine()
    config = AppConfig(
        defaults=Defaults(
            resources=ResourceSpec(
                cpus=2,
                memory_mib=4096,
                swap_mib=128,
                tasks=1024,
            )
        ),
        profiles={
            "vm": {
                "image": "fedora:latest",
                "cpus": 4,
                "memory": 8192,
                "swap": 64,
                "tasks": 512,
            }
        },
    )
    service = MachineService(
        config,
        MachineStore(tmp_path / "machines"),
        ShellStateManager(tmp_path / "shell-state"),
        {"podman": runner},
    )

    record = service.create(
        CreateOptions(
            name="dev",
            profile="vm",
            cpus=6,
            swap_mib=0,
            tasks=256,
        )
    )

    assert record.spec.resources.cpus == 6
    assert record.spec.resources.memory_mib == 8192
    assert record.spec.resources.swap_mib == 0
    assert record.spec.resources.tasks == 256


def test_create_can_start_machine(tmp_path: Path) -> None:
    runner = FakeEngine()
    service = _service(tmp_path, runner)

    record = service.create(CreateOptions(name="dev", image="alpine", start=True))

    assert [m.id for m in runner.created] == [record.id]
    assert runner.started == [record]
    assert runner.state is RuntimeState.RUNNING


def test_start_rejects_machine_that_remains_stopped(tmp_path: Path) -> None:
    runner = FakeEngine(start_state=RuntimeState.STOPPED)
    service = _service(tmp_path, runner)
    service.create(CreateOptions(name="dev", image="alpine"))

    with pytest.raises(ValueError, match="failed to start"):
        service.start("dev")


def test_profile_can_disable_shell_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = FakeEngine()
    service = _service(
        tmp_path,
        runner,
        profiles={
            "isolated": {
                "image": "fedora:latest",
                "workspace": str(workspace),
                "shell_state": False,
            }
        },
    )

    record = service.create(CreateOptions(name="box", profile="isolated"))

    assert record.spec.shell_state is False
    assert [mount.kind for mount in record.spec.mounts] == ["workspace"]
    assert not (tmp_path / "shell-state" / "box").exists()


def test_cli_can_enable_shell_state_over_profile(tmp_path: Path) -> None:
    runner = FakeEngine()
    service = _service(
        tmp_path,
        runner,
        profiles={
            "isolated": {
                "image": "fedora:latest",
                "shell_state": False,
            }
        },
    )

    record = service.create(
        CreateOptions(name="box", profile="isolated", shell_state=True)
    )

    assert record.spec.shell_state is True
    assert [mount.kind for mount in record.spec.mounts] == ["shell_state"]
    assert (tmp_path / "shell-state" / "box").exists()


def test_create_expands_home_share_mounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_home = tmp_path / "home" / "fed"
    dev = host_home / "Dev"
    work = host_home / "Downloads" / "Work"
    dev.mkdir(parents=True)
    work.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(host_home))

    runner = FakeEngine()
    service = _service(
        tmp_path,
        runner,
        profiles={"binavibe": {"home_share": str(dev)}},
    )

    record = service.create(
        CreateOptions(
            name="binavibe",
            image="localhost/binavibe:latest",
            profile="binavibe",
            home_shares=(str(work),),
        )
    )

    assert [
        (mount.source, mount.target, mount.kind)
        for mount in record.spec.mounts
        if mount.kind == "home_share"
    ] == [
        (dev.resolve(), str(dev.resolve()), "home_share"),
        (dev.resolve(), "/home/user/Dev", "home_share"),
        (work.resolve(), str(work.resolve()), "home_share"),
        (work.resolve(), "/home/user/Downloads/Work", "home_share"),
    ]


def test_enter_starts_machine_and_execs_shell_from_mapped_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = FakeEngine()
    service = _service(tmp_path, runner)
    service.create(
        CreateOptions(name="dev", image="alpine", workspaces=(str(workspace),))
    )
    monkeypatch.chdir(workspace)

    service.enter("dev")

    assert runner.started
    record, spec = runner.execs[0]
    assert record.name == "dev"
    assert spec.command == AUTO_ENTER_SHELL_COMMAND
    assert spec.interactive is True
    assert spec.tty is True
    assert spec.workdir == "/work/workspace"
    assert spec.env == (
        "MIM_MACHINE=dev",
        "MIM_RUNNER=podman",
        "MIM_SHELL_STATE=1",
    )


def test_enter_uses_explicit_shell_from_record(tmp_path: Path) -> None:
    runner = FakeEngine()
    service = _service(tmp_path, runner)
    service.create(CreateOptions(name="dev", image="alpine", shell="zsh -l"))

    service.enter("dev")

    assert runner.execs[0][1].command[-2:] == ("zsh", "-l")
    assert runner.execs[0][1].env == (
        "MIM_MACHINE=dev",
        "MIM_RUNNER=podman",
        "MIM_SHELL_STATE=1",
    )


def test_enter_does_not_set_shell_state_env_when_disabled(tmp_path: Path) -> None:
    runner = FakeEngine()
    service = _service(
        tmp_path,
        runner,
        profiles={
            "isolated": {
                "image": "alpine",
                "shell": "zsh -l",
                "shell_state": False,
            }
        },
    )
    service.create(CreateOptions(name="dev", profile="isolated"))

    service.enter("dev")

    assert runner.execs[0][1].command[-2:] == ("zsh", "-l")
    assert runner.execs[0][1].env == (
        "MIM_MACHINE=dev",
        "MIM_RUNNER=podman",
        "MIM_SHELL_STATE=0",
    )


def test_enter_shell_flag_overrides_record_shell(tmp_path: Path) -> None:
    runner = FakeEngine()
    service = _service(tmp_path, runner)
    service.create(CreateOptions(name="dev", image="alpine", shell="zsh -l"))

    service.enter("dev", "auto")

    assert runner.execs[0][1].command == AUTO_ENTER_SHELL_COMMAND
    assert runner.execs[0][1].env == (
        "MIM_MACHINE=dev",
        "MIM_RUNNER=podman",
        "MIM_SHELL_STATE=1",
    )


def test_enter_uses_config_default_shell(tmp_path: Path) -> None:
    runner = FakeEngine()
    service = MachineService(
        AppConfig(defaults=Defaults(shell="bash -l"), profiles={}),
        MachineStore(tmp_path / "machines"),
        ShellStateManager(tmp_path / "shell-state"),
        {"podman": runner},
    )
    record = service.create(CreateOptions(name="dev", image="alpine"))

    service.enter("dev")

    assert record.spec.shell is None
    assert runner.execs[0][1].command[-2:] == ("bash", "-l")
    assert runner.execs[0][1].env == (
        "MIM_MACHINE=dev",
        "MIM_RUNNER=podman",
        "MIM_SHELL_STATE=1",
    )


def test_enter_forwards_host_terminal_capabilities(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    runner = FakeEngine()
    service = _service(tmp_path, runner)
    service.create(CreateOptions(name="dev", image="alpine"))

    service.enter("dev")

    assert runner.execs[0][1].env[-2:] == (
        "TERM=xterm-256color",
        "COLORTERM=truecolor",
    )


def test_exec_starts_machine_before_running_command(tmp_path: Path) -> None:
    runner = FakeEngine()
    service = _service(tmp_path, runner)
    service.create(CreateOptions(name="dev", image="alpine"))

    service.exec("dev", ExecSpec(("echo", "hello")))

    assert runner.started
    assert runner.execs[0][1].command == ("echo", "hello")


def test_ssh_session_runs_original_command_with_protocol_stdio(tmp_path: Path) -> None:
    runner = FakeEngine()
    service = _service(tmp_path, runner)
    service.create(CreateOptions(name="dev", image="alpine"))

    service.ssh_session(
        "dev",
        original_command="rsync --server -logDtpre.iLsfxCIvu . /work",
        tty=False,
    )

    assert runner.started
    spec = runner.execs[0][1]
    assert spec.command == (
        "sh",
        "-c",
        "rsync --server -logDtpre.iLsfxCIvu . /work",
    )
    assert spec.interactive is True
    assert spec.tty is False
    assert spec.env == (
        "MIM_MACHINE=dev",
        "MIM_RUNNER=podman",
        "MIM_SHELL_STATE=1",
    )


def test_ssh_session_uses_first_workspace_as_session_workdir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = FakeEngine()
    service = _service(tmp_path, runner)
    service.create(
        CreateOptions(name="dev", image="alpine", workspaces=(str(workspace),))
    )

    service.ssh_session("dev", original_command="pwd", tty=False)

    assert runner.execs[0][1].workdir == "/work/workspace"


def test_ssh_session_prefers_explicit_workdir_over_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = FakeEngine()
    service = _service(tmp_path, runner)
    service.create(
        CreateOptions(
            name="dev",
            image="alpine",
            workspaces=(str(workspace),),
            workdir="/custom",
        )
    )

    service.ssh_session("dev", original_command="pwd", tty=False)

    assert runner.execs[0][1].workdir == "/custom"


def test_ssh_session_enters_configured_shell_with_pty_and_term(tmp_path: Path) -> None:
    runner = FakeEngine()
    service = _service(tmp_path, runner)
    service.create(CreateOptions(name="dev", image="alpine", shell="zsh -l"))

    service.ssh_session(
        "dev",
        original_command=None,
        tty=True,
        term="xterm-256color",
    )

    spec = runner.execs[0][1]
    assert spec.command[-2:] == ("zsh", "-l")
    assert spec.interactive is True
    assert spec.tty is True
    assert spec.env[-1] == "TERM=xterm-256color"


def test_exec_rejects_missing_machine_with_clear_error(tmp_path: Path) -> None:
    runner = FakeEngine()
    service = _service(tmp_path, runner)
    service.create(CreateOptions(name="dev", image="alpine"))

    runner.state = RuntimeState.MISSING
    with pytest.raises(ValueError, match="machine \\[dev\\] is missing"):
        service.exec("dev", ExecSpec(("echo", "hello")))


def test_rejects_port_publishing_with_host_network(tmp_path: Path) -> None:
    service = _service(tmp_path, FakeEngine())

    with pytest.raises(ValueError, match="host networking"):
        service.create(
            CreateOptions(
                name="dev",
                image="alpine",
                network=NetworkMode.HOST,
                ports=("18812:18812",),
            )
        )


def test_rejects_gpu_on_unsupported_runner(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        FakeEngine(name="docker"),
    )

    with pytest.raises(ValueError, match="does not support GPU forwarding"):
        service.create(CreateOptions(name="dev", image="alpine", gpu=True))


def test_delete_can_preserve_shell_state(tmp_path: Path) -> None:
    runner = FakeEngine()
    service = _service(tmp_path, runner)
    record = service.create(CreateOptions(name="dev", image="alpine"))
    shell_state_path = tmp_path / "shell-state" / "dev"
    history = shell_state_path / ".zsh_history"
    history.write_text("kept\n", encoding="utf-8")

    service.delete("dev", keep_shell_state=True)

    assert [m.id for m in runner.deleted] == [record.id]
    assert not service.store.exists("dev")
    assert history.read_text(encoding="utf-8") == "kept\n"


def test_failed_binding_save_retains_captured_id_for_recovery(tmp_path):
    runner = FakeEngine()
    service = _service(tmp_path, runner)
    save = service.store.save
    calls = 0

    def fail_once(machine):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("save failed")
        save(machine)

    service.store.save = fail_once
    with pytest.raises(RuntimeError, match="save failed"):
        service.create(CreateOptions(name="dev", image="alpine"))
    interrupted = service.store.load("dev")
    assert interrupted.operation is Operation.CREATING
    assert interrupted.target is not None
    assert runner.deleted == []
    recovered = service.recover("dev")
    assert recovered.operation is Operation.READY
    assert recovered.target.container_id == interrupted.target.container_id


def test_create_failure_does_not_delete_unowned_backend(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = FakeEngine(create_error=RuntimeError("backend create failed"))
    service = _service(tmp_path, runner)

    with pytest.raises(RuntimeError, match="backend create failed"):
        service.create(
            CreateOptions(name="dev", image="alpine", workspaces=(str(workspace),))
        )

    assert runner.deleted == []
    assert service.store.load("dev").operation is Operation.CREATING
    assert (tmp_path / "shell-state" / "dev").exists()


def test_create_failure_preserves_existing_shell_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = FakeEngine(create_error=RuntimeError("backend create failed"))
    service = _service(tmp_path, runner)
    shell_state_path = tmp_path / "shell-state" / "dev"
    shell_state_path.mkdir(parents=True)
    history = shell_state_path / ".zsh_history"
    history.write_text("kept\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="backend create failed"):
        service.create(
            CreateOptions(name="dev", image="alpine", workspaces=(str(workspace),))
        )

    assert history.read_text(encoding="utf-8") == "kept\n"


def test_delete_failure_retains_binding_and_can_be_retried(tmp_path):
    runner = FakeEngine()
    service = _service(tmp_path, runner)
    machine = service.create(CreateOptions(name="dev", image="alpine"))
    runner.delete_error = RuntimeError("backend cleanup failed")
    with pytest.raises(RuntimeError, match="backend cleanup failed"):
        service.delete("dev")
    retained = service.store.load("dev")
    assert retained.operation is Operation.DELETING
    assert retained.binding == machine.binding
    assert service.shell_state.path_for("dev").exists()
    runner.delete_error = None
    service.delete("dev")
    assert not service.store.exists("dev")


def _service(tmp_path: Path, runner: FakeEngine, profiles=None) -> MachineService:
    config = AppConfig(
        defaults=Defaults(runner=runner.name),
        profiles=profiles or {},
    )
    return MachineService(
        config,
        MachineStore(tmp_path / "machines"),
        ShellStateManager(tmp_path / "shell-state"),
        {runner.name: runner},
    )
