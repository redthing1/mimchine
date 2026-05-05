from __future__ import annotations

from pathlib import Path

from mimchine.shell_state import SHELL_STATE_GUEST_DIR, ShellStateManager


def test_shell_state_mount_uses_machine_directory(tmp_path: Path) -> None:
    manager = ShellStateManager(tmp_path)

    mount = manager.mount_for("dev")

    assert mount.source == tmp_path / "dev"
    assert mount.target == SHELL_STATE_GUEST_DIR
    assert mount.kind == "shell_state"
    assert not mount.source.exists()


def test_shell_state_sets_known_shell_history_files(tmp_path: Path) -> None:
    manager = ShellStateManager(tmp_path)

    assert manager.env_for_shell(("zsh", "-l")) == (
        "HISTFILE=/mim/shell-state/.zsh_history",
    )
    assert manager.env_for_shell(("/bin/bash", "-l")) == (
        "HISTFILE=/mim/shell-state/.bash_history",
    )
    assert manager.env_for_shell(("fish",)) == ()


def test_shell_state_delete_removes_state(tmp_path: Path) -> None:
    manager = ShellStateManager(tmp_path)
    state_dir = manager.ensure("dev")
    (state_dir / ".zsh_history").write_text("ls\n", encoding="utf-8")

    manager.delete("dev")

    assert not state_dir.exists()
