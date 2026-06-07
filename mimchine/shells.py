from __future__ import annotations

import shlex
from importlib.resources import files

from .shell_state import SHELL_STATE_GUEST_DIR

AUTO_SHELL = "auto"
LAUNCHER_ARG0 = "mim-enter"
LAUNCHER_RESOURCE = "enter_shell.sh"


def _load_enter_shell_script() -> str:
    return (
        files(__package__)
        .joinpath(LAUNCHER_RESOURCE)
        .read_text(encoding="utf-8")
        .replace("__SHELL_STATE_GUEST_DIR__", SHELL_STATE_GUEST_DIR)
    )


ENTER_SHELL_SCRIPT = _load_enter_shell_script()
AUTO_ENTER_SHELL_SCRIPT = ENTER_SHELL_SCRIPT
AUTO_ENTER_SHELL_COMMAND = ("sh", "-lc", ENTER_SHELL_SCRIPT, LAUNCHER_ARG0)


def normalize_shell(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == AUTO_SHELL:
        return None
    return text


def enter_shell_command(value: str | None) -> tuple[str, ...]:
    shell = normalize_shell(value)
    if shell is None:
        return AUTO_ENTER_SHELL_COMMAND

    parts = tuple(shlex.split(shell))
    if not parts or any(not part for part in parts):
        raise ValueError("shell cannot be empty")
    return ("sh", "-lc", ENTER_SHELL_SCRIPT, LAUNCHER_ARG0, *parts)
