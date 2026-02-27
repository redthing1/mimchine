import os
import posixpath
import shlex
import shutil
from typing import List, Optional

import typer
import sh

from . import __VERSION__
from .config import get_container_runtime, set_container_runtime_override
from .log import configure_logging, logger
from . import output

from .containers import (
    CONTAINER_CMD,
    SHELL_USER_ROOT,
    get_containers,
    get_container_display_name,
    get_container_mounts,
    container_exists,
    container_is_mim,
    container_is_running,
    image_exists,
    resolve_container_shell_user,
    resolve_image_home,
)
from .integration import (
    get_container_integration_mounts,
    get_home_integration_mount,
    get_home_integration_env,
    get_home_dir,
    get_app_data_dir,
    map_host_path_to_container,
)
from .shell_helpers import (
    get_shell_home_dir,
    get_non_root_shell_identity_args,
    prepare_non_root_shell,
    normalize_host_path,
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
FORMAT_CONTAINER_OUTPUT = {
    "_out": output.stream_stdout,
    "_err": output.stream_stderr,
}


def version_callback(value: bool):
    if value:
        output.print_version(APP_NAME, __VERSION__)
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


def _resolve_shell_as_root(
    container_name: str,
    as_root: bool,
    as_user: bool,
) -> bool:
    if as_root and as_user:
        logger.error("cannot use --as-root and --as-user together")
        raise typer.Exit(1)

    if as_root:
        return True

    if as_user:
        return False

    shell_user = resolve_container_shell_user(container_name)
    if shell_user == SHELL_USER_ROOT:
        logger.debug(
            f"using root shell for container [{container_name}] from shell-user label"
        )
        return True

    if shell_user is not None:
        logger.debug(
            f"using non-root shell for container [{container_name}] from shell-user label"
        )

    return False


def _get_home_share_mount_pairs(
    home_shares: List[str],
    image_name: str,
) -> list[tuple[str, str]]:
    if len(home_shares) == 0:
        return []

    user_home_dir = normalize_host_path(get_home_dir())
    image_home_dir = resolve_image_home(image_name)
    mounted_pairs: set[tuple[str, str]] = set()
    mount_pairs: list[tuple[str, str]] = []

    for home_share_input in home_shares:
        home_share_src_abs = normalize_host_path(home_share_input)

        if not os.path.exists(home_share_src_abs):
            logger.warn(f"home share [{home_share_src_abs}] does not exist, skipping")
            continue

        if os.path.commonpath([home_share_src_abs, user_home_dir]) != user_home_dir:
            logger.warn(
                f"home share [{home_share_src_abs}] is not under the user's home directory, skipping"
            )
            continue

        home_share_pair = (home_share_src_abs, home_share_src_abs)
        if home_share_pair not in mounted_pairs:
            mount_pairs.append(home_share_pair)
            mounted_pairs.add(home_share_pair)

        home_share_src_rel = os.path.relpath(home_share_src_abs, user_home_dir)
        if home_share_src_rel == ".":
            home_share_tilde_target = image_home_dir
        else:
            home_share_tilde_target = posixpath.join(
                image_home_dir, home_share_src_rel.replace("\\", "/")
            )

        home_share_tilde_pair = (home_share_src_abs, home_share_tilde_target)
        if home_share_tilde_pair[1] != home_share_tilde_pair[0]:
            if home_share_tilde_pair not in mounted_pairs:
                mount_pairs.append(home_share_tilde_pair)
                mounted_pairs.add(home_share_tilde_pair)

    return mount_pairs


def _parse_custom_mount_specs(
    custom_mounts: List[str],
) -> list[tuple[str, str]]:
    parsed_mounts: list[tuple[str, str]] = []

    for custom_mount in custom_mounts:
        if ":" not in custom_mount:
            logger.error(
                f"invalid mount format [{custom_mount}], expected host_path:container_path"
            )
            raise typer.Exit(1)

        host_path, container_path = custom_mount.split(":", 1)
        host_path_expanded = normalize_host_path(host_path)
        container_path = container_path.strip()

        if not os.path.exists(host_path_expanded):
            logger.error(
                f"custom mount host path [{host_path_expanded}] does not exist"
            )
            raise typer.Exit(1)

        if len(container_path) == 0:
            logger.error(
                f"custom mount [{custom_mount}] has empty container path target"
            )
            raise typer.Exit(1)

        if not container_path.startswith("/"):
            logger.error(
                f"custom mount target [{container_path}] must be an absolute container path"
            )
            raise typer.Exit(1)

        parsed_mounts.append((host_path_expanded, container_path))

    return parsed_mounts


def _parse_device_specs(devices: List[str]) -> list[str]:
    parsed_devices: list[str] = []

    for device_spec_input in devices:
        device_spec = device_spec_input.strip()
        if len(device_spec) == 0:
            logger.error("device spec cannot be empty")
            raise typer.Exit(1)

        host_device_path = normalize_host_path(device_spec.split(":", 1)[0].strip())
        if not os.path.exists(host_device_path):
            logger.error(f"device path [{host_device_path}] does not exist")
            raise typer.Exit(1)

        parsed_devices.append(device_spec)

    return parsed_devices


def _parse_keepalive_args(keepalive_command: Optional[str]) -> list[str]:
    if keepalive_command is None:
        return []

    keepalive_args = shlex.split(keepalive_command)
    if len(keepalive_args) == 0:
        logger.error("keepalive command cannot be empty")
        raise typer.Exit(1)

    return keepalive_args


def _parse_create_inputs(
    image_name: str,
    home_shares: List[str],
    custom_mounts: List[str],
    devices: List[str],
    keepalive_command: Optional[str],
) -> tuple[list[str], list[tuple[str, str]], list[str], list[tuple[str, str]]]:
    keepalive_args = _parse_keepalive_args(keepalive_command)
    custom_mount_specs = _parse_custom_mount_specs(custom_mounts)
    device_specs = _parse_device_specs(devices)
    home_share_mount_pairs = _get_home_share_mount_pairs(home_shares, image_name)
    return keepalive_args, custom_mount_specs, device_specs, home_share_mount_pairs


@app.callback()
def app_callback(
    verbose: List[bool] = typer.Option([], "--verbose", "-v", help="Verbose output"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Quiet output"),
    runtime: Optional[str] = typer.Option(
        None,
        "--runtime",
        help="container runtime override for this command (podman or docker).",
    ),
    version: Optional[bool] = typer.Option(
        None, "--version", "-V", callback=version_callback, is_eager=True
    ),
):
    configure_logging(len(verbose), quiet)
    try:
        set_container_runtime_override(runtime)
    except ValueError as exc:
        logger.error(str(exc))
        raise typer.Exit(1)


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
        help="passthrough mount under host home; available at identical path and under container HOME.",
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
        help="custom mount in format host_path:container_path.",
    ),
    devices: List[str] = typer.Option(
        [],
        "-D",
        "--device",
        help="device passthrough in format host_device or host_device:container_device.",
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
    keepalive_command: Optional[str] = typer.Option(
        None,
        "--keepalive-command",
        help="override the image command used as pid 1.",
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

    keepalive_args, custom_mount_specs, device_specs, home_share_mount_pairs = (
        _parse_create_inputs(
            image_name,
            home_shares,
            custom_mounts,
            devices,
            keepalive_command,
        )
    )

    container_data_dir = os.path.join(DATA_DIR, container_name)
    logger.info(f"creating data directory [{container_data_dir}]")
    os.makedirs(container_data_dir, exist_ok=True)

    container_create_opts = [
        "--name",
        container_name,
        "--init",
        "--label",
        "mim=1",
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
            source_dir = os.path.dirname(mount.source_path)
            if source_dir:
                os.makedirs(source_dir, exist_ok=True)
            if not os.path.exists(mount.source_path):
                logger.trace(
                    f"integration mount source [{mount.source_path}] does not exist, creating empty file"
                )
                open(mount.source_path, "a").close()
                os.chmod(mount.source_path, 0o777)
        elif not os.path.exists(mount.source_path):
            logger.trace(
                f"integration mount source [{mount.source_path}] does not exist, creating directory"
            )
            os.makedirs(mount.source_path, exist_ok=True)

        container_create_opts.extend(
            ["-v", f"{mount.source_path}:{mount.container_path}"]
        )

    for home_share_src, home_share_target in home_share_mount_pairs:
        container_create_opts.extend(["-v", f"{home_share_src}:{home_share_target}"])

    for host_path_expanded, container_path in custom_mount_specs:
        logger.debug(f"adding custom mount: {host_path_expanded}:{container_path}")
        container_create_opts.extend(["-v", f"{host_path_expanded}:{container_path}"])

    for device_spec in device_specs:
        logger.debug(f"adding device passthrough: {device_spec}")
        container_create_opts.extend(["--device", device_spec])

    for port_bind in port_binds:
        container_create_opts.extend(["-p", port_bind])

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
    as_user: bool = typer.Option(
        False,
        "--as-user",
        help="run shell as non-root user inside the container.",
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
    runtime = get_container_runtime()
    shell_as_root = _resolve_shell_as_root(container_name, as_root, as_user)
    try:
        shell_home_dir = get_shell_home_dir(container_name, runtime, shell_as_root)
    except ValueError as exc:
        logger.error(str(exc))
        raise typer.Exit(1)
    shell_command_args = shlex.split(shell)
    if len(shell_command_args) == 0:
        logger.error("shell command cannot be empty")
        raise typer.Exit(1)
    shell_env: list[tuple[str, str]] = []

    if not shell_as_root:
        try:
            shell_home_dir, shell_env = prepare_non_root_shell(
                container_name,
                runtime,
                shell_home_dir,
                shell_command_args,
            )
        except ValueError as exc:
            logger.error(str(exc))
            raise typer.Exit(1)

    logger.info(f"getting shell in container [{container_name}]")
    shell_args = ["exec", "-it"]

    if shell_as_root:
        logger.debug("running shell as root")
        shell_args.extend(["--user", "0:0"])
    else:
        shell_args.extend(get_non_root_shell_identity_args(runtime))
        shell_args.extend(["-e", f"HOME={shell_home_dir}"])
        for key, value in shell_env:
            shell_args.extend(["-e", f"{key}={value}"])

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
    containers = get_containers(only_mim=True)
    rows = [
        (
            get_container_display_name(container),
            str(container.get("State", "unknown")),
        )
        for container in containers
    ]
    output.print_container_list(rows)
