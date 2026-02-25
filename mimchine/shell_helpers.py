import os
import shlex

import sh
from minlog import logger

from .containers import CONTAINER_CMD, get_container_env
from .integration import CONTAINER_HOME_DIR


def container_has_command(container_name: str, command_name: str) -> bool:
    check_cmd = CONTAINER_CMD.bake(
        "exec",
        container_name,
        "sh",
        "-lc",
        f"command -v {shlex.quote(command_name)} >/dev/null 2>&1",
    )
    logger.debug(f"running command: {check_cmd}")
    try:
        check_cmd()
        return True
    except sh.ErrorReturnCode:
        return False


def is_zsh_command(command_args: list[str]) -> bool:
    if len(command_args) == 0:
        return False

    return os.path.basename(command_args[0]) == "zsh"


def get_shell_home_dir(container_name: str, as_root: bool) -> str:
    if as_root:
        return CONTAINER_HOME_DIR

    container_env = get_container_env(container_name)
    container_home = container_env.get("HOME")
    if container_home:
        return container_home

    return "/tmp"


def get_non_root_shell_identity_args(runtime: str) -> list[str]:
    if runtime == "docker":
        return ["--user", f"{os.getuid()}:{os.getgid()}"]

    return []


def run_non_root_shell_script(
    container_name: str,
    runtime: str,
    shell_home_dir: str,
    script: str,
) -> str:
    cmd = CONTAINER_CMD.bake(
        "exec",
        *get_non_root_shell_identity_args(runtime),
        "-e",
        f"HOME={shell_home_dir}",
        container_name,
        "sh",
        "-lc",
        script,
    )
    logger.debug(f"running command: {cmd}")
    return str(cmd()).strip()


def resolve_non_root_shell_home(
    container_name: str,
    runtime: str,
    shell_home_dir: str,
) -> str:
    script = """
if [ -z "$HOME" ]; then
  HOME=/tmp
fi

if ! (mkdir -p "$HOME" 2>/dev/null && [ -w "$HOME" ]); then
  HOME=/tmp
  mkdir -p "$HOME"
fi

printf "%s\\n" "$HOME"
"""
    try:
        output = run_non_root_shell_script(
            container_name,
            runtime,
            shell_home_dir,
            script,
        )
        resolved_home = [line.strip() for line in output.splitlines() if line.strip()]
        if len(resolved_home) == 0:
            return shell_home_dir
        return resolved_home[-1]
    except sh.ErrorReturnCode as exc:
        logger.debug(
            f"could not resolve writable shell home [{shell_home_dir}] (code {exc.exit_code}), using it as-is"
        )
        return shell_home_dir


def ensure_non_root_zshrc(
    container_name: str,
    runtime: str,
    shell_home_dir: str,
) -> None:
    script = """
mkdir -p "$HOME"
[ -f "$HOME/.zshrc" ] || : > "$HOME/.zshrc"
"""
    try:
        run_non_root_shell_script(
            container_name,
            runtime,
            shell_home_dir,
            script,
        )
    except sh.ErrorReturnCode as exc:
        logger.debug(
            f"could not ensure zshrc in [{shell_home_dir}] (code {exc.exit_code}), continuing"
        )


def normalize_host_path(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))
