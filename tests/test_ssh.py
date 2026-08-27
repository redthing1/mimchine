from __future__ import annotations

import os
from pathlib import Path

import pytest

import mimchine.ssh as ssh_module
from mimchine.domain import ImageSource, MachineRecord, MachineSpec
from mimchine.process import ProcessError, ProcessResult
from mimchine.ssh import (
    INCLUDE_BEGIN,
    SshPaths,
    SshSetupManager,
    machine_name_from_ssh_host,
    ssh_client_command,
    ssh_proxy_command,
)
from mimchine.state import MachineStore


class SshSetupProcessRunner:
    def __init__(self, fail_command: str | None = None):
        self.calls: list[tuple[str, ...]] = []
        self.fail_command = fail_command

    def run(self, args, *, capture=False, foreground=False, check=True, cwd=None):
        command = tuple(str(arg) for arg in args)
        self.calls.append(command)
        if command[0] == self.fail_command:
            raise ProcessError(ProcessResult(command, 1, "", "invalid config"))
        if command[0] == "ssh-keygen" and "-f" in command and "-y" not in command:
            key = Path(command[command.index("-f") + 1])
            key.write_text("PRIVATE\n", encoding="utf-8")
            key.with_suffix(".pub").write_text("PUBLIC\n", encoding="utf-8")
        stdout = "ssh-ed25519 test-public-key" if "-y" in command else ""
        return ProcessResult(command, 0, stdout, "")


def _paths(tmp_path: Path, *, user_config: Path | None = None) -> SshPaths:
    config_dir = tmp_path / "config" / "mimchine"
    state_dir = tmp_path / "data" / "mimchine" / "ssh"
    return SshPaths(
        client_config=config_dir / "ssh_config",
        server_config=config_dir / "sshd_config",
        state_dir=state_dir,
        host_key=state_dir / "host_ed25519",
        client_key=state_dir / "client_ed25519",
        known_hosts=state_dir / "known_hosts",
        user_config=user_config or tmp_path / "home" / ".ssh" / "config",
        app_cache_dir=tmp_path / "cache" / "mimchine",
    )


def _manager(paths: SshPaths) -> tuple[SshSetupManager, SshSetupProcessRunner]:
    runner = SshSetupProcessRunner()
    manager = SshSetupManager(
        paths,
        runner=runner,
        ssh_path="ssh-test",
        sshd_path="sshd-test",
        mim_command=("mim test",),
        username="developer",
    )
    return manager, runner


def test_setup_ssh_generates_restricted_managed_configuration(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    manager, runner = _manager(paths)

    manager.setup()

    assert paths.client_key.read_text(encoding="utf-8") == "PRIVATE\n"
    assert paths.known_hosts.read_text(encoding="utf-8") == (
        "*.mim ssh-ed25519 test-public-key\n"
    )
    client = paths.client_config.read_text(encoding="utf-8")
    assert "Host *.mim" in client
    assert 'User "developer"' in client
    assert "XDG_CONFIG_HOME=" in client
    assert "XDG_DATA_HOME=" in client
    assert "XDG_CACHE_HOME=" in client
    assert "'mim test' _ssh-proxy %h" in client
    server = paths.server_config.read_text(encoding="utf-8")
    assert "ForceCommand" not in server
    assert "DisableForwarding yes" in server
    assert "AllowTcpForwarding no" in server
    assert "AllowAgentForwarding no" in server
    assert "PermitRootLogin prohibit-password" in server
    assert "MaxSessions 1" in server
    assert paths.user_config.read_bytes().startswith(INCLUDE_BEGIN)
    assert os.stat(paths.client_key).st_mode & 0o777 == 0o600
    assert os.stat(paths.state_dir).st_mode & 0o777 == 0o700
    assert os.stat(paths.user_config.parent).st_mode & 0o777 == 0o700
    assert any(call[:3] == ("sshd-test", "-t", "-f") for call in runner.calls)


def test_setup_ssh_is_idempotent_and_preserves_existing_config(tmp_path: Path) -> None:
    user_config = tmp_path / "home" / ".ssh" / "config"
    user_config.parent.mkdir(parents=True)
    original = b"Host private\r\n    HostName secret.example\r\n"
    user_config.write_bytes(original)
    paths = _paths(tmp_path, user_config=user_config)
    manager, _ = _manager(paths)

    manager.setup()
    private_key = paths.client_key.read_bytes()
    manager.setup()

    configured = user_config.read_bytes()
    assert configured.count(INCLUDE_BEGIN) == 1
    assert configured.endswith(original)
    assert paths.client_key.read_bytes() == private_key


def test_remove_ssh_restores_existing_config_and_removes_owned_files(
    tmp_path: Path,
) -> None:
    user_config = tmp_path / "home" / ".ssh" / "config"
    user_config.parent.mkdir(parents=True)
    original = b"Host existing\n    HostName existing.example\n"
    user_config.write_bytes(original)
    paths = _paths(tmp_path, user_config=user_config)
    manager, _ = _manager(paths)
    manager.setup()

    manager.remove()

    assert user_config.read_bytes() == original
    assert not paths.client_config.exists()
    assert not paths.server_config.exists()
    assert not paths.state_dir.exists()


def test_remove_ssh_deletes_empty_user_config_created_by_setup(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    manager, _ = _manager(paths)
    manager.setup()

    manager.remove()
    manager.remove()

    assert not paths.user_config.exists()


def test_setup_preserves_user_config_symlink(tmp_path: Path) -> None:
    target = tmp_path / "managed" / "ssh_config"
    target.parent.mkdir()
    target.write_text("Host existing\n", encoding="utf-8")
    user_config = tmp_path / "home" / ".ssh" / "config"
    user_config.parent.mkdir(parents=True)
    user_config.symlink_to(target)
    manager, _ = _manager(_paths(tmp_path, user_config=user_config))

    manager.setup()
    manager.remove()

    assert user_config.is_symlink()
    assert target.read_text(encoding="utf-8") == "Host existing\n"


def test_setup_escapes_openssh_tokens_in_managed_paths(tmp_path: Path) -> None:
    paths = _paths(tmp_path / "percent%root")
    manager, _ = _manager(paths)

    manager.setup()

    assert "percent%%root" in paths.client_config.read_text(encoding="utf-8")
    assert "percent%%root" in paths.user_config.read_text(encoding="utf-8")


def test_setup_validation_failure_does_not_activate_ssh_include(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    manager = SshSetupManager(
        paths,
        runner=SshSetupProcessRunner(fail_command="sshd-test"),
        ssh_path="ssh-test",
        sshd_path="sshd-test",
        mim_command=("mim-test",),
        username="developer",
    )

    with pytest.raises(ProcessError):
        manager.setup()

    assert not paths.user_config.exists()


def test_setup_validation_failure_preserves_working_managed_configs(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    manager, _ = _manager(paths)
    manager.setup()
    original = {
        path: path.read_bytes()
        for path in (paths.client_config, paths.server_config, paths.known_hosts)
    }
    failing_manager = SshSetupManager(
        paths,
        runner=SshSetupProcessRunner(fail_command="sshd-test"),
        ssh_path="ssh-test",
        sshd_path="sshd-test",
        mim_command=("replacement-mim",),
        username="developer",
    )

    with pytest.raises(ProcessError):
        failing_manager.setup()

    assert paths.user_config.read_bytes().startswith(INCLUDE_BEGIN)
    assert {path: path.read_bytes() for path in original} == original


@pytest.mark.parametrize(
    "host",
    ["dev", ".mim", "bad/name.mim", "Bad.mim", "bad%h.mim", "-bad.mim"],
)
def test_machine_name_from_ssh_host_rejects_invalid_hosts(host: str) -> None:
    with pytest.raises(ValueError):
        machine_name_from_ssh_host(host)


def test_current_mim_command_uses_the_invoked_launcher(monkeypatch) -> None:
    monkeypatch.setattr(ssh_module.sys, "argv", ["mim"])
    monkeypatch.setattr(
        ssh_module.shutil,
        "which",
        lambda command: "/example/current-mim" if command == "mim" else None,
    )

    assert ssh_module.current_mim_command() == ("/example/current-mim",)


def test_ssh_proxy_builds_one_shot_forced_session_command(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.server_config.parent.mkdir(parents=True)
    paths.server_config.write_text("LogLevel ERROR\n", encoding="utf-8")
    store = MachineStore(tmp_path / "machines")
    record = MachineRecord.from_spec(
        MachineSpec("dev", ImageSource.oci_reference("alpine"), "podman"),
        created_at="2026-01-01T00:00:00+00:00",
    )
    store.save(record)

    command = ssh_proxy_command(
        "dev.mim",
        paths=paths,
        store=store,
        sshd_path="sshd-test",
        mim_command=("mim-test",),
        environment={
            "PATH": "/opt/homebrew/bin:/usr/bin",
            "XDG_DATA_HOME": "/example/mim data",
        },
    )

    assert command[:7] == (
        "sshd-test",
        "-i",
        "-e",
        "-q",
        "-f",
        str(paths.server_config),
        "-o",
    )
    assert command[-1].startswith("ForceCommand=")
    assert "PATH=/opt/homebrew/bin:/usr/bin" in command[-1]
    assert "'XDG_DATA_HOME=/example/mim data'" in command[-1]
    assert command[-1].endswith("mim-test _ssh-session dev")


def test_ssh_client_uses_managed_config_and_mim_hostname(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.client_config.parent.mkdir(parents=True)
    paths.client_config.write_text("Host *.mim\n", encoding="utf-8")

    assert ssh_client_command(
        "dev",
        ("uname", "-a"),
        paths=paths,
        ssh_path="ssh-test",
    ) == (
        "ssh-test",
        "-F",
        str(paths.client_config),
        "dev.mim",
        "uname",
        "-a",
    )


def test_ssh_client_treats_dotted_name_as_exact_machine_name(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.client_config.parent.mkdir(parents=True)
    paths.client_config.write_text("Host *.mim\n", encoding="utf-8")

    command = ssh_client_command("dev.mim", paths=paths, ssh_path="ssh-test")

    assert command[-1] == "dev.mim.mim"
