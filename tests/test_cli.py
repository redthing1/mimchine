from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from mimchine import cli
from mimchine.domain import IdentityMode, NetworkMode


def test_build_cli_passes_canonical_options(monkeypatch, tmp_path: Path) -> None:
    dockerfile = tmp_path / "Containerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    captured = {}

    class FakeBuildService:
        def build(self, options):
            captured["options"] = options

    monkeypatch.setattr(
        cli.BuildService,
        "default",
        staticmethod(lambda: FakeBuildService()),
    )

    result = CliRunner().invoke(
        cli.app,
        ["build", "example:dev", "-f", str(dockerfile), "-C", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert captured["options"].image == "example:dev"
    assert captured["options"].file == dockerfile
    assert captured["options"].context == tmp_path


def test_create_cli_passes_machine_intent(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    captured = {}

    class FakeMachineService:
        def create(self, options):
            captured["options"] = options

    monkeypatch.setattr(
        cli.MachineService,
        "default",
        staticmethod(lambda: FakeMachineService()),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "create",
            "dev",
            "--image",
            "fedora:latest",
            "--runner",
            "podman",
            "-W",
            str(workspace),
            "--no-net",
            "--host-user",
        ],
    )

    assert result.exit_code == 0
    options = captured["options"]
    assert options.name == "dev"
    assert options.image == "fedora:latest"
    assert options.runner == "podman"
    assert options.workspaces == (str(workspace),)
    assert options.network is NetworkMode.NONE
    assert options.identity.mode is IdentityMode.HOST


def test_enter_cli_passes_name_and_shell(monkeypatch) -> None:
    captured = {}

    class FakeMachineService:
        def enter(self, name, shell):
            captured["name"] = name
            captured["shell"] = shell

    monkeypatch.setattr(
        cli.MachineService,
        "default",
        staticmethod(lambda: FakeMachineService()),
    )

    result = CliRunner().invoke(cli.app, ["enter", "dev", "-s", "bash -l"])

    assert result.exit_code == 0
    assert captured == {"name": "dev", "shell": "bash -l"}


def test_exec_cli_passes_command_spec(monkeypatch) -> None:
    captured = {}

    class FakeMachineService:
        def exec(self, name, spec):
            captured["name"] = name
            captured["spec"] = spec

    monkeypatch.setattr(
        cli.MachineService,
        "default",
        staticmethod(lambda: FakeMachineService()),
    )

    result = CliRunner().invoke(
        cli.app,
        ["exec", "-i", "-t", "-e", "A=B", "-w", "/work", "dev", "sh", "-lc", "true"],
    )

    assert result.exit_code == 0
    assert captured["name"] == "dev"
    assert captured["spec"].command == ("sh", "-lc", "true")
    assert captured["spec"].interactive is True
    assert captured["spec"].tty is True
    assert captured["spec"].env == ("A=B",)
    assert captured["spec"].workdir == "/work"


def test_exec_cli_accepts_separator_after_name(monkeypatch) -> None:
    captured = {}

    class FakeMachineService:
        def exec(self, name, spec):
            captured["name"] = name
            captured["spec"] = spec

    monkeypatch.setattr(
        cli.MachineService,
        "default",
        staticmethod(lambda: FakeMachineService()),
    )

    result = CliRunner().invoke(cli.app, ["exec", "dev", "--", "sh", "-lc", "true"])

    assert result.exit_code == 0
    assert captured["name"] == "dev"
    assert captured["spec"].command == ("sh", "-lc", "true")


def test_delete_cli_passes_keep_shell_state(monkeypatch) -> None:
    captured = {}

    class FakeMachineService:
        def delete(self, name, *, keep_shell_state):
            captured["name"] = name
            captured["keep_shell_state"] = keep_shell_state

    monkeypatch.setattr(
        cli.MachineService,
        "default",
        staticmethod(lambda: FakeMachineService()),
    )

    result = CliRunner().invoke(cli.app, ["delete", "dev", "-f", "--keep-shell-state"])

    assert result.exit_code == 0
    assert captured == {"name": "dev", "keep_shell_state": True}
