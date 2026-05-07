from __future__ import annotations

import shlex
from textwrap import dedent

from .shell_state import SHELL_STATE_GUEST_DIR

AUTO_SHELL = "auto"

AUTO_ENTER_SHELL_SCRIPT = dedent(f"""\
    shell_is_missing() {{
      [ -z "$shell" ] || [ ! -x "$shell" ]
    }}

    shell="${{SHELL:-}}"

    if shell_is_missing; then
      user_name="$(id -un 2>/dev/null || true)"
      if [ -n "$user_name" ]; then
        shell="$(
          awk -F: -v user="$user_name" '
            $1 == user {{ print $7; exit }}
          ' /etc/passwd 2>/dev/null || true
        )"
      fi
    fi

    if shell_is_missing; then
      if command -v zsh >/dev/null 2>&1; then
        shell="$(command -v zsh)"
      elif command -v bash >/dev/null 2>&1; then
        shell="$(command -v bash)"
      else
        shell="$(command -v sh || printf /bin/sh)"
      fi
    fi

    shell_name="$(basename "$shell")"
    shell_state_dir="{SHELL_STATE_GUEST_DIR}"

    case "$shell_name" in
      zsh)
        if [ -d "$shell_state_dir" ]; then
          export HISTFILE="${{HISTFILE:-$shell_state_dir/.zsh_history}}"
        fi
        exec "$shell" -l
        ;;
      bash)
        if [ -d "$shell_state_dir" ]; then
          export HISTFILE="${{HISTFILE:-$shell_state_dir/.bash_history}}"
        fi
        exec "$shell" -l
        ;;
      *)
        exec "$shell"
        ;;
    esac
    """)

AUTO_ENTER_SHELL_COMMAND = ("sh", "-lc", AUTO_ENTER_SHELL_SCRIPT)


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
    return parts


def is_auto_shell(value: str | None) -> bool:
    return normalize_shell(value) is None
