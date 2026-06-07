from __future__ import annotations

import pytest

from mimchine.shells import (
    AUTO_ENTER_SHELL_COMMAND,
    ENTER_SHELL_SCRIPT,
    LAUNCHER_ARG0,
    enter_shell_command,
    normalize_shell,
)


def test_auto_shell_resolves_to_guest_detection_command() -> None:
    command = enter_shell_command(None)

    assert command == AUTO_ENTER_SHELL_COMMAND
    assert command[:4] == ("sh", "-lc", ENTER_SHELL_SCRIPT, LAUNCHER_ARG0)
    assert "${SHELL:-}" in command[2]
    assert "/etc/passwd" in command[2]
    assert "command -v zsh" in command[2]
    assert "_mim_shell_state_append_zsh_history" in command[2]
    assert "HISTSIZE=" in command[2]
    assert "HISTFILESIZE=" in command[2]
    assert 'shell_state_enabled="${MIM_SHELL_STATE:-1}"' in command[2]
    assert "SAVEHIST:-10000" not in command[2]
    assert "HISTSIZE:-10000" not in command[2]
    assert "HISTFILESIZE:-20000" not in command[2]


def test_shell_auto_normalizes_to_no_preference() -> None:
    assert normalize_shell(None) is None
    assert normalize_shell("") is None
    assert normalize_shell("auto") is None


def test_explicit_shell_splits_like_a_command() -> None:
    assert enter_shell_command("zsh -l") == (
        "sh",
        "-lc",
        ENTER_SHELL_SCRIPT,
        LAUNCHER_ARG0,
        "zsh",
        "-l",
    )
    assert normalize_shell(" zsh -l ") == "zsh -l"


def test_empty_shell_command_is_rejected_after_splitting() -> None:
    with pytest.raises(ValueError, match="shell cannot be empty"):
        enter_shell_command("''")
