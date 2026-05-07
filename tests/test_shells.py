from __future__ import annotations

import pytest

from mimchine.shells import AUTO_ENTER_SHELL_COMMAND, enter_shell_command, normalize_shell


def test_auto_shell_resolves_to_guest_detection_command() -> None:
    command = enter_shell_command(None)

    assert command == AUTO_ENTER_SHELL_COMMAND
    assert command[:2] == ("sh", "-lc")
    assert "${SHELL:-}" in command[2]
    assert "/etc/passwd" in command[2]
    assert "command -v zsh" in command[2]


def test_shell_auto_normalizes_to_no_preference() -> None:
    assert normalize_shell(None) is None
    assert normalize_shell("") is None
    assert normalize_shell("auto") is None


def test_explicit_shell_splits_like_a_command() -> None:
    assert enter_shell_command("zsh -l") == ("zsh", "-l")
    assert normalize_shell(" zsh -l ") == "zsh -l"


def test_empty_shell_command_is_rejected_after_splitting() -> None:
    with pytest.raises(ValueError, match="shell cannot be empty"):
        enter_shell_command("''")
