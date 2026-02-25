import grp
import os
import posixpath
import pwd
import re
import shlex
from dataclasses import dataclass

import sh
from minlog import logger

from .containers import CONTAINER_CMD, get_container_env
from .integration import CONTAINER_HOME_DIR, CONTAINER_SHELL_HISTORY_DIR

MIM_ZDOTDIR_ENV_KEY = "MIM_ZDOTDIR"
_IDENTITY_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


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
    container_host_home = container_env.get("HOST_HOME")
    if container_host_home:
        return container_host_home

    container_home = container_env.get("HOME")
    if container_home:
        return container_home

    return "/tmp"


def get_non_root_shell_identity_args(runtime: str) -> list[str]:
    if runtime == "docker":
        return ["--user", f"{os.getuid()}:{os.getgid()}"]

    return []


@dataclass(frozen=True)
class HostIdentity:
    uid: int
    gid: int
    username: str
    groupname: str


def _sanitize_identity_name(name: str, fallback: str) -> str:
    if _IDENTITY_NAME_PATTERN.fullmatch(name):
        return name
    return fallback


def get_host_identity() -> HostIdentity:
    uid = os.getuid()
    gid = os.getgid()

    fallback_username = os.environ.get("USER", f"mimuser{uid}")
    fallback_groupname = os.environ.get("GROUP", f"mimgroup{gid}")

    try:
        username = pwd.getpwuid(uid).pw_name
    except KeyError:
        username = fallback_username

    try:
        groupname = grp.getgrgid(gid).gr_name
    except KeyError:
        groupname = fallback_groupname

    username = _sanitize_identity_name(username, f"mimuser{uid}")
    groupname = _sanitize_identity_name(groupname, f"mimgroup{gid}")

    return HostIdentity(
        uid=uid,
        gid=gid,
        username=username,
        groupname=groupname,
    )


def _build_docker_identity_script(
    identity: HostIdentity,
    shell_home_dir: str,
) -> str:
    fallback_username = f"mimuser{identity.uid}"
    fallback_groupname = f"mimgroup{identity.gid}"
    return f"""
set -eu

uid={identity.uid}
gid={identity.gid}
user={shlex.quote(identity.username)}
group={shlex.quote(identity.groupname)}
fallback_user={shlex.quote(fallback_username)}
fallback_group={shlex.quote(fallback_groupname)}
home={shlex.quote(shell_home_dir)}

group_for_gid="$(awk -F: -v gid="$gid" '$3 == gid {{ print $1; exit }}' /etc/group || true)"
if [ -z "$group_for_gid" ]; then
  if awk -F: -v name="$group" '$1 == name {{ found=1 }} END {{ exit(found ? 0 : 1) }}' /etc/group; then
    group="$fallback_group"
  fi
  printf '%s:x:%s:\\n' "$group" "$gid" >> /etc/group
fi

user_for_uid="$(awk -F: -v uid="$uid" '$3 == uid {{ print $1; exit }}' /etc/passwd || true)"
if [ -z "$user_for_uid" ]; then
  if awk -F: -v name="$user" '$1 == name {{ found=1 }} END {{ exit(found ? 0 : 1) }}' /etc/passwd; then
    user="$fallback_user"
  fi

  if command -v zsh >/dev/null 2>&1; then
    shell_path="$(command -v zsh)"
  elif command -v bash >/dev/null 2>&1; then
    shell_path="$(command -v bash)"
  else
    shell_path=/bin/sh
  fi

  printf '%s:x:%s:%s::%s:%s\\n' "$user" "$uid" "$gid" "$home" "$shell_path" >> /etc/passwd
fi

awk -F: -v uid="$uid" '$3 == uid {{ print $1; exit }}' /etc/passwd
"""


def ensure_docker_non_root_identity(
    container_name: str,
    shell_home_dir: str,
) -> str | None:
    identity = get_host_identity()
    script = _build_docker_identity_script(identity, shell_home_dir)
    cmd = CONTAINER_CMD.bake(
        "exec",
        "--user",
        "0:0",
        container_name,
        "sh",
        "-lc",
        script,
    )
    logger.debug(f"running command: {cmd}")
    try:
        output = str(cmd()).strip()
        resolved_usernames = [
            line.strip() for line in output.splitlines() if line.strip()
        ]
        if len(resolved_usernames) == 0:
            return None
        return resolved_usernames[-1]
    except sh.ErrorReturnCode as exc:
        logger.debug(
            f"could not ensure docker uid/gid identity entries (code {exc.exit_code}), continuing"
        )
        return None


def run_non_root_shell_probe(
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
        output = run_non_root_shell_probe(
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


def get_non_root_zsh_env(container_name: str) -> list[tuple[str, str]]:
    container_env = get_container_env(container_name)
    image_zdotdir = container_env.get(MIM_ZDOTDIR_ENV_KEY, "").strip()
    if len(image_zdotdir) == 0:
        logger.debug(
            f"{MIM_ZDOTDIR_ENV_KEY} not set in container [{container_name}], leaving ZDOTDIR unchanged"
        )
        return []

    logger.debug(
        f"using zsh config dir from {MIM_ZDOTDIR_ENV_KEY}: [{image_zdotdir}]"
    )
    return [("ZDOTDIR", image_zdotdir)]


def get_shell_history_env(shell_command_args: list[str]) -> list[tuple[str, str]]:
    if len(shell_command_args) == 0:
        return []

    shell_name = os.path.basename(shell_command_args[0])
    history_files = {
        "zsh": ".zsh_history",
        "bash": ".bash_history",
    }
    history_file_name = history_files.get(shell_name)
    if history_file_name is None:
        return []

    history_file = posixpath.join(CONTAINER_SHELL_HISTORY_DIR, history_file_name)

    logger.debug(f"using shell history file: [{history_file}]")
    return [("HISTFILE", history_file)]


def prepare_non_root_shell(
    container_name: str,
    runtime: str,
    shell_home_dir: str,
    shell_command_args: list[str],
) -> tuple[str, list[tuple[str, str]]]:
    shell_home_dir = resolve_non_root_shell_home(
        container_name,
        runtime,
        shell_home_dir,
    )

    shell_env: list[tuple[str, str]] = []
    shell_env.extend(get_shell_history_env(shell_command_args))
    if is_zsh_command(shell_command_args):
        if not container_has_command(container_name, "zsh"):
            raise ValueError(
                f"container [{container_name}] does not have zsh installed"
            )
        shell_env.extend(get_non_root_zsh_env(container_name))

    if runtime == "docker":
        resolved_username = ensure_docker_non_root_identity(
            container_name,
            shell_home_dir,
        )
        if resolved_username is not None:
            shell_env.extend(
                [
                    ("USER", resolved_username),
                    ("LOGNAME", resolved_username),
                ]
            )

    return shell_home_dir, shell_env


def normalize_host_path(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))
