import os
import shlex
import shutil
from typing import List, Optional

import typer
import sh
from minlog import logger

from . import __VERSION__

from .containers import (
    CONTAINER_CMD,
    FORMAT_CONTAINER_OUTPUT,
    get_containers,
    container_exists,
    container_is_running,
    container_is_mim,
    get_container_display_name,
    image_exists,
)
from .integration import (
    get_os_integration_mounts,
    get_container_integration_mounts,
    get_os_integration_home_env,
    get_home_dir,
    get_app_data_dir,
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

    # Add platform option if specified
    if platform:
        build_cmd_args.extend(["--platform", platform])

    # Add build args if specified
    for build_arg in build_args:
        build_cmd_args.extend(["--build-arg", build_arg])

    # Add context directory
    build_cmd_args.append(context_dir)

    build_cmd = CONTAINER_CMD.bake(
        *build_cmd_args,
        **FORMAT_CONTAINER_OUTPUT,
    )
    logger.debug(f"running command: {build_cmd}")
    try:
        build_proc = build_cmd()
    except sh.ErrorReturnCode as e:
        logger.error(f"build failed with error code {e.exit_code}")
        raise typer.Exit(1)

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
        help="custom mount in format host_path:container_path (e.g., ~/Downloads/claude:/root/Downloads).",
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
):
    if container_name is None:
        container_name = image_name

    if container_exists(container_name):
        logger.error(f"container [{container_name}] already exists")
        raise typer.Exit(1)

    # ensuare an image exists
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

    if host_pid:
        container_create_opts.append("--pid=host")

    if privileged:
        container_create_opts.append("--privileged")

    for mount in get_os_integration_mounts():
        container_create_opts.extend(["-v", mount])

    for mount in get_container_integration_mounts(container_data_dir):
        # mount_source, mount_target = mount.split(":")
        # if not os.path.exists(mount.source_path):
        #     logger.warn(
        #         f"integration mount source [{mount.source_path}] does not exist, skipping"
        #     )
        #     continue

        if mount.is_file:
            # if the source is a file, but doesn't exist, create an empty file
            if not os.path.exists(mount.source_path):
                logger.trace(
                    f"integration mount source [{mount.source_path}] does not exist, creating empty file"
                )
                open(mount.source_path, "a").close()
                # change permissions to 777 so the container can write to it
                os.chmod(mount.source_path, 0o777)

        container_create_opts.extend(
            ["-v", f"{mount.source_path}:{mount.container_path}"]
        )

    user_home_dir = get_home_dir()
    for home_share in home_shares:
        # normalize the home share path
        home_share = os.path.abspath(os.path.expanduser(home_share))

        # ensure the home share exists and is under the user's home directory
        if not os.path.exists(home_share):
            logger.warning(f"home share [{home_share}] does not exist, skipping")
            continue

        if not home_share.startswith(user_home_dir):
            logger.warning(
                f"home share [{home_share}] is not under the user's home directory, skipping"
            )
            continue

        # mount the home share into the container's home directory
        home_share_src_abs = os.path.abspath(home_share)
        home_share_src_rel = os.path.relpath(home_share_src_abs, user_home_dir)
        home_share_target = os.path.join(CONTAINER_HOME_DIR, home_share_src_rel)
        container_create_opts.extend(
            ["-v", f"{home_share_src_abs}:{home_share_target}"]
        )

    # process custom mounts
    for custom_mount in custom_mounts:
        if ":" not in custom_mount:
            logger.error(
                f"invalid mount format [{custom_mount}], expected host_path:container_path"
            )
            raise typer.Exit(1)

        host_path, container_path = custom_mount.split(":", 1)

        # expand user path on host side (e.g., ~/Downloads -> /Users/user/Downloads)
        host_path_expanded = os.path.abspath(os.path.expanduser(host_path))

        # validate host path exists
        if not os.path.exists(host_path_expanded):
            logger.error(
                f"custom mount host path [{host_path_expanded}] does not exist"
            )
            raise typer.Exit(1)

        logger.debug(f"adding custom mount: {host_path_expanded}:{container_path}")
        container_create_opts.extend(["-v", f"{host_path_expanded}:{container_path}"])

    for port_bind in port_binds:
        container_create_opts.extend(["-p", port_bind])

    integration_home_env = get_os_integration_home_env()
    container_create_opts.extend(["-e", integration_home_env])

    keepalive_args = shlex.split(keepalive_command)
    if len(keepalive_args) == 0:
        logger.error("keepalive command cannot be empty")
        raise typer.Exit(1)

    logger.info(f"creating mim container [{container_name}] from image [{image_name}]")
    create_cmd = CONTAINER_CMD.bake(
        "create",
        *container_create_opts,
        image_name,
        *keepalive_args,
    )

    logger.debug(f"running command: {create_cmd}")
    try:
        create_cmd(**FORMAT_CONTAINER_OUTPUT)
        logger.info(f"container [{container_name}] created")
    except sh.ErrorReturnCode as e:
        logger.error(f"run failed with error code {e.exit_code}")
        raise typer.Exit(1)


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
    if not container_exists(container_name):
        logger.error(f"container [{container_name}] does not exist")
        raise typer.Exit(1)

    if not container_is_mim(container_name):
        logger.error(f"container [{container_name}] is not a mim container")
        raise typer.Exit(1)

    if container_is_running(container_name):
        if force:
            stop_cmd = CONTAINER_CMD.bake(
                "stop",
                "-t",
                "1",
                container_name,
            )
            logger.debug(f"running command: {stop_cmd}")
            try:
                stop_proc = stop_cmd()
            except sh.ErrorReturnCode as e:
                logger.error(f"stop failed with error code {e.exit_code}")
                raise typer.Exit(1)
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
    destroy_cmd = CONTAINER_CMD.bake(
        "rm",
        container_name,
    )

    logger.debug(f"running command: {destroy_cmd}")
    try:
        destroy_proc = destroy_cmd()
    except sh.ErrorReturnCode as e:
        logger.error(f"destroy failed with error code {e.exit_code}")
        raise typer.Exit(1)

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
        "/bin/zsh",
        "-s",
        "--shell",
        help="shell to run in the container.",
    ),
):
    if not container_exists(container_name):
        logger.error(f"container [{container_name}] does not exist")
        raise typer.Exit(1)

    if not container_is_mim(container_name):
        logger.error(f"container [{container_name}] is not a mim container")
        raise typer.Exit(1)

    if not container_is_running(container_name):
        logger.info(f"container [{container_name}] is not running, starting it")
        start_cmd = CONTAINER_CMD.bake(
            "start",
            container_name,
        )

        logger.debug(f"running command: {start_cmd}")
        try:
            start_proc = start_cmd()
        except sh.ErrorReturnCode as e:
            logger.error(f"start failed with error code {e.exit_code}")
            raise typer.Exit(1)

    if not container_is_running(container_name):
        logger.error(
            f"container [{container_name}] could not be started (startup command exited)"
        )
        raise typer.Exit(1)

    logger.info(f"getting shell in container [{container_name}]")
    shell_cmd = CONTAINER_CMD.bake(
        "exec",
        "-it",
        container_name,
        shell,
    )

    logger.debug(f"running command: {shell_cmd}")
    try:
        shell_proc = shell_cmd(_fg=True)
    except sh.ErrorReturnCode as e:
        logger.error(f"shell failed with error code {e.exit_code}")
        raise typer.Exit(1)


@app.command(help="start a container", no_args_is_help=True)
def start(
    container_name: str = typer.Option(
        ...,
        "-c",
        "--container-name",
        help="name of the container to start.",
    ),
):
    if not container_exists(container_name):
        logger.error(f"container [{container_name}] does not exist")
        raise typer.Exit(1)

    if not container_is_mim(container_name):
        logger.error(f"container [{container_name}] is not a mim container")
        raise typer.Exit(1)

    if container_is_running(container_name):
        logger.info(f"container [{container_name}] is already running")
        return

    logger.info(f"starting container [{container_name}]")
    start_cmd = CONTAINER_CMD.bake(
        "start",
        container_name,
    )

    logger.debug(f"running command: {start_cmd}")
    try:
        start_cmd(**FORMAT_CONTAINER_OUTPUT)
        logger.info(f"container [{container_name}] started")
    except sh.ErrorReturnCode as e:
        logger.error(f"start failed with error code {e.exit_code}")
        raise typer.Exit(1)


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
    if not container_exists(container_name):
        logger.error(f"container [{container_name}] does not exist")
        raise typer.Exit(1)

    if not container_is_mim(container_name):
        logger.error(f"container [{container_name}] is not a mim container")
        raise typer.Exit(1)

    if not container_is_running(container_name):
        logger.info(f"container [{container_name}] is already stopped")
        return

    logger.info(f"stopping container [{container_name}]")
    stop_cmd = CONTAINER_CMD.bake(
        "stop",
        "-t",
        str(timeout),
        container_name,
    )

    logger.debug(f"running command: {stop_cmd}")
    try:
        stop_cmd(**FORMAT_CONTAINER_OUTPUT)
        logger.info(f"container [{container_name}] stopped")
    except sh.ErrorReturnCode as e:
        logger.error(f"stop failed with error code {e.exit_code}")
        raise typer.Exit(1)


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
