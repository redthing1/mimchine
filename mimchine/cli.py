import os
import posixpath
import shlex
import shutil
from typing import List, Optional

import typer
import sh
from minlog import logger

from . import __VERSION__
from .config import get_container_runtime

from .containers import (
    CONTAINER_CMD,
    FORMAT_CONTAINER_OUTPUT,
    get_containers,
    get_container_env,
    get_container_mounts,
    container_exists,
    container_is_running,
    container_is_mim,
    get_container_display_name,
    image_exists,
)
from .integration import (
    get_container_integration_mounts,
    get_home_integration_mount,
    get_home_integration_env,
    get_container_host_home_dir,
    get_home_dir,
    get_app_data_dir,
    map_host_path_to_container,
    CONTAINER_HOME_DIR,
)

CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])

APP_NAME = "mimchine"
app = typer.Typer(
    name=APP_NAME,
    help=f"{APP_NAME}: integrated mini machines",
    no_args_is_help=True,
    context_settings=CONTEXT_SETTINGS,
    pretty_exceptions_show_locals=False,
)

DATA_DIR = get_app_data_dir(APP_NAME)


def version_callback(value: bool):
    if value:
        logger.info(f"{APP_NAME} v{__VERSION__}")
        raise typer.Exit()


def _require_mim_container(container_name: str):
    if not container_exists(container_name):
        logger.error(f"container [{container_name}] does not exist")
        raise typer.Exit(1)

    if not container_is_mim(container_name):
        logger.error(f"container [{container_name}] is not a mim container")
        raise typer.Exit(1)


def _run_container_cmd(
    *args: str,
    error_action: str,
    format_output: bool = False,
    foreground: bool = False,
):
    cmd = CONTAINER_CMD.bake(*args)
    logger.debug(f"running command: {cmd}")
    try:
        if foreground:
            cmd(_fg=True)
        elif format_output:
            cmd(**FORMAT_CONTAINER_OUTPUT)
        else:
            cmd()
    except sh.ErrorReturnCode as e:
        logger.error(f"{error_action} failed with error code {e.exit_code}")
        raise typer.Exit(1)


def _container_has_command(container_name: str, command_name: str) -> bool:
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


def _is_zsh_command(command_args: List[str]) -> bool:
    if len(command_args) == 0:
        return False

    return os.path.basename(command_args[0]) == "zsh"


def _get_shell_home_dir(container_name: str, as_root: bool) -> str:
    if as_root:
        return CONTAINER_HOME_DIR

    container_env = get_container_env(container_name)

    container_home = container_env.get("HOME")
    if container_home:
        return container_home

    return "/tmp"


def _normalize_host_path(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


@app.callback()
def app_callback(
    verbose: List[bool] = typer.Option([], "--verbose", "-v", help="Verbose output"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Quiet output"),
    version: Optional[bool] = typer.Option(
        None, "--version", "-V", callback=version_callback, is_eager=True
    ),
):
    if len(verbose) == 1:
        logger.be_verbose()
    elif len(verbose) == 2:
        logger.be_debug()
    elif quiet:
        logger.be_quiet()


@app.command(help="build an image from a dockerfile", no_args_is_help=True)
def build(
    dockerfile: str = typer.Option(
        ...,
        "-f",
        "--dockerfile",
        help="path to the dockerfile to build an image from.",
    ),
    image_name: str = typer.Option(
        ...,
        "-n",
        "--image-name",
        help="name of the image to build.",
    ),
    context_dir: str = typer.Option(
        ".",
        "-C",
        "--context-dir",
        help="path to context directory for the docker build.",
    ),
    platform: Optional[str] = typer.Option(
        None,
        "--platform",
        help="set target platform for build.",
    ),
    build_args: List[str] = typer.Option(
        [],
        "--build-arg",
        help="set build-time variables.",
    ),
):
    logger.info(f"building docker image from [{dockerfile}]")

    build_cmd_args = [
        "build",
        "-f",
        dockerfile,
        "-t",
        image_name,
    ]

    if platform:
        build_cmd_args.extend(["--platform", platform])

    for build_arg in build_args:
        build_cmd_args.extend(["--build-arg", build_arg])

    build_cmd_args.append(context_dir)

    _run_container_cmd(
        *build_cmd_args,
        error_action="build",
        format_output=True,
    )

    logger.info(f"build complete, image [{image_name}] created")


@app.command(help="create a container from an image", no_args_is_help=True)
def create(
    image_name: str = typer.Option(
        ...,
        "-n",
        "--image-name",
        help="name of the image to run.",
    ),
    container_name: str = typer.Option(
        None,
        "-c",
        "--container-name",
        help="name to give the container.",
    ),
    home_shares: List[str] = typer.Option(
        [],
        "-H",
        "--home-share",
        help="path to a directory to share with the container.",
    ),
    port_binds: List[str] = typer.Option(
        [],
        "-p",
        "--port-bind",
        help="port to bind from the host to the container.",
    ),
    custom_mounts: List[str] = typer.Option(
        [],
        "-M",
        "--mount",
        help="custom mount in format host_path:container_path (e.g., ~/Downloads/stuff:/work/stuff).",
    ),
    host_pid: bool = typer.Option(
        False,
        "--host-pid",
        help="share the host's PID namespace with the container.",
    ),
    privileged: bool = typer.Option(
        False,
        "--privileged",
        help="run the container in privileged mode.",
    ),
    keepalive_command: str = typer.Option(
        "sleep infinity",
        "--keepalive-command",
        help="command to run as pid 1 so the machine stays alive.",
    ),
    integrate_home: bool = typer.Option(
        False,
        "--integrate-home",
        help="mount full host home under /mim/home/<user> and set HOST_HOME.",
    ),
):
    if container_name is None:
        container_name = image_name

    if container_exists(container_name):
        logger.error(f"container [{container_name}] already exists")
        raise typer.Exit(1)

    if not image_exists(image_name):
        logger.error(f"image [{image_name}] does not exist")
        raise typer.Exit(1)

    container_data_dir = os.path.join(DATA_DIR, container_name)
    logger.info(f"creating data directory [{container_data_dir}]")
    os.makedirs(container_data_dir, exist_ok=True)

    container_create_opts = [
        "--name",
        container_name,
        "--init",
        "--label",
        f"mim=1",
    ]

    if get_container_runtime() == "podman":
        container_create_opts.extend(["--userns", "keep-id"])

    if host_pid:
        container_create_opts.append("--pid=host")

    if privileged:
        container_create_opts.append("--privileged")

    if integrate_home:
        container_create_opts.extend(["-v", get_home_integration_mount()])
        container_create_opts.extend(["-e", get_home_integration_env()])

    for mount in get_container_integration_mounts(container_data_dir):
        if mount.is_file:
            if not os.path.exists(mount.source_path):
                logger.trace(
                    f"integration mount source [{mount.source_path}] does not exist, creating empty file"
                )
                open(mount.source_path, "a").close()
                os.chmod(mount.source_path, 0o777)

        container_create_opts.extend(
            ["-v", f"{mount.source_path}:{mount.container_path}"]
        )

    user_home_dir = _normalize_host_path(get_home_dir())
    container_host_home = get_container_host_home_dir()
    for home_share in home_shares:
        home_share = _normalize_host_path(home_share)

        if not os.path.exists(home_share):
            logger.warning(f"home share [{home_share}] does not exist, skipping")
            continue

        if os.path.commonpath([home_share, user_home_dir]) != user_home_dir:
            logger.warning(
                f"home share [{home_share}] is not under the user's home directory, skipping"
            )
            continue

        home_share_src_abs = _normalize_host_path(home_share)
        home_share_src_rel = os.path.relpath(home_share_src_abs, user_home_dir)

        if home_share_src_rel == ".":
            home_share_target = container_host_home
        else:
            home_share_target = posixpath.join(
                container_host_home, home_share_src_rel.replace("\\", "/")
            )

        container_create_opts.extend(
            ["-v", f"{home_share_src_abs}:{home_share_target}"]
        )

    for custom_mount in custom_mounts:
        if ":" not in custom_mount:
            logger.error(
                f"invalid mount format [{custom_mount}], expected host_path:container_path"
            )
            raise typer.Exit(1)

        host_path, container_path = custom_mount.split(":", 1)

        host_path_expanded = _normalize_host_path(host_path)

        if not os.path.exists(host_path_expanded):
            logger.error(
                f"custom mount host path [{host_path_expanded}] does not exist"
            )
            raise typer.Exit(1)

        logger.debug(f"adding custom mount: {host_path_expanded}:{container_path}")
        container_create_opts.extend(["-v", f"{host_path_expanded}:{container_path}"])

    for port_bind in port_binds:
        container_create_opts.extend(["-p", port_bind])

    keepalive_args = shlex.split(keepalive_command)
    if len(keepalive_args) == 0:
        logger.error("keepalive command cannot be empty")
        raise typer.Exit(1)

    logger.info(f"creating mim container [{container_name}] from image [{image_name}]")
    _run_container_cmd(
        "create",
        *container_create_opts,
        image_name,
        *keepalive_args,
        error_action="create",
        format_output=True,
    )
    logger.info(f"container [{container_name}] created")


@app.command(help="destroy a container", no_args_is_help=True)
def destroy(
    container_name: str = typer.Option(
        ...,
        "-c",
        "--container-name",
        help="name of the container to destroy.",
    ),
    force: bool = typer.Option(
        False,
        "-f",
        "--force",
        help="force destroy the container.",
    ),
):
    _require_mim_container(container_name)

    if container_is_running(container_name):
        if force:
            _run_container_cmd(
                "stop",
                "-t",
                "1",
                container_name,
                error_action="stop",
            )
        else:
            logger.error(f"container [{container_name}] is running")
            raise typer.Exit(1)

    logger.info(f"destroying data directory for container [{container_name}]")
    container_data_dir = os.path.join(DATA_DIR, container_name)
    try:
        shutil.rmtree(container_data_dir)
    except FileNotFoundError:
        logger.debug(f"data directory [{container_data_dir}] already absent")

    logger.info(f"destroying mim container [{container_name}]")
    _run_container_cmd(
        "rm",
        container_name,
        error_action="destroy",
    )

    logger.info(f"container [{container_name}] destroyed")


@app.command(help="get a shell in a running container", no_args_is_help=True)
def shell(
    container_name: str = typer.Option(
        ...,
        "-c",
        "--container-name",
        help="name of the container to get a shell in.",
    ),
    shell: str = typer.Option(
        "zsh -l",
        "-s",
        "--shell",
        help="shell command to run in the container.",
    ),
    as_root: bool = typer.Option(
        False,
        "--as-root",
        help="run shell as root inside the container.",
    ),
):
    _require_mim_container(container_name)

    if not container_is_running(container_name):
        logger.info(f"container [{container_name}] is not running, starting it")
        _run_container_cmd(
            "start",
            container_name,
            error_action="start",
        )

    if not container_is_running(container_name):
        logger.error(
            f"container [{container_name}] could not be started (startup command exited)"
        )
        raise typer.Exit(1)

    host_cwd = os.getcwd()
    container_mounts = get_container_mounts(container_name)
    container_cwd = map_host_path_to_container(host_cwd, container_mounts)
    shell_home_dir = _get_shell_home_dir(container_name, as_root)
    shell_command_args = shlex.split(shell)
    if len(shell_command_args) == 0:
        logger.error("shell command cannot be empty")
        raise typer.Exit(1)

    if _is_zsh_command(shell_command_args):
        if not _container_has_command(container_name, "zsh"):
            logger.error(f"container [{container_name}] does not have zsh installed")
            raise typer.Exit(1)

    logger.info(f"getting shell in container [{container_name}]")
    shell_args = ["exec", "-it"]

    if as_root:
        logger.debug("running shell as root")
        shell_args.extend(["--user", "0:0"])
    else:
        host_uid = os.getuid()
        host_gid = os.getgid()
        shell_args.extend(["--user", f"{host_uid}:{host_gid}"])
        shell_args.extend(["-e", f"HOME={shell_home_dir}"])

    if container_cwd is not None:
        logger.debug(f"mapped cwd [{host_cwd}] -> [{container_cwd}]")
        shell_args.extend(["-w", container_cwd])
    else:
        logger.debug(
            f"cwd [{host_cwd}] is not under mounted paths, using [{shell_home_dir}]"
        )
        shell_args.extend(["-w", shell_home_dir])

    shell_args.append(container_name)
    shell_args.extend(shell_command_args)

    _run_container_cmd(
        *shell_args,
        error_action="shell",
        foreground=True,
    )


@app.command(help="start a container", no_args_is_help=True)
def start(
    container_name: str = typer.Option(
        ...,
        "-c",
        "--container-name",
        help="name of the container to start.",
    ),
):
    _require_mim_container(container_name)

    if container_is_running(container_name):
        logger.info(f"container [{container_name}] is already running")
        return

    logger.info(f"starting container [{container_name}]")
    _run_container_cmd(
        "start",
        container_name,
        error_action="start",
        format_output=True,
    )
    logger.info(f"container [{container_name}] started")


@app.command(help="stop a container", no_args_is_help=True)
def stop(
    container_name: str = typer.Option(
        ...,
        "-c",
        "--container-name",
        help="name of the container to stop.",
    ),
    timeout: int = typer.Option(
        10,
        "-t",
        "--timeout",
        help="seconds to wait before forcefully stopping the container.",
    ),
):
    _require_mim_container(container_name)

    if not container_is_running(container_name):
        logger.info(f"container [{container_name}] is already stopped")
        return

    logger.info(f"stopping container [{container_name}]")
    _run_container_cmd(
        "stop",
        "-t",
        str(timeout),
        container_name,
        error_action="stop",
        format_output=True,
    )
    logger.info(f"container [{container_name}] stopped")


@app.command(help="list all mim containers")
def list():
    logger.info("listing all mim containers")

    containers = get_containers(only_mim=True)
    if len(containers) == 0:
        logger.info("no mim containers found")
        return

    logger.info(f"mim containers[{len(containers)}]:")
    for container in containers:
        container_name = get_container_display_name(container)
        container_state = container["State"]

        logger.info(
            f"  [{container_name}] ({container_state})",
        )
