from __future__ import annotations

import os
import posixpath
import shutil
from pathlib import Path

from .domain import MountSpec, validate_machine_name


SHELL_STATE_GUEST_DIR = "/mim/shell-state"


class ShellStateManager:
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)

    def path_for(self, machine_name: str) -> Path:
        return self.base_dir / validate_machine_name(machine_name)

    def ensure(self, machine_name: str) -> Path:
        path = self.path_for(machine_name)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def mount_for(self, machine_name: str) -> MountSpec:
        return MountSpec(
            source=self.path_for(machine_name),
            target=SHELL_STATE_GUEST_DIR,
            read_only=False,
            kind="shell_state",
        )

    def env_for_shell(self, shell_command: tuple[str, ...]) -> tuple[str, ...]:
        if len(shell_command) == 0:
            return ()

        shell_name = os.path.basename(shell_command[0])
        history_files = {
            "zsh": ".zsh_history",
            "bash": ".bash_history",
        }
        history_file = history_files.get(shell_name)
        if history_file is None:
            return ()

        return (f"HISTFILE={posixpath.join(SHELL_STATE_GUEST_DIR, history_file)}",)

    def delete(self, machine_name: str) -> None:
        shutil.rmtree(self.path_for(machine_name), ignore_errors=True)
