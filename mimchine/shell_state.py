from __future__ import annotations

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

    def delete(self, machine_name: str) -> None:
        shutil.rmtree(self.path_for(machine_name), ignore_errors=True)
