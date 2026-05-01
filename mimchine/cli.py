import os
import shlex
from typing import List, Optional

import typer
import sh

from . import __VERSION__
from .config import get_container_runtime, set_container_runtime_override
from .create_config import (
    CreateConfig,
    apply_profile,
    get_namespace_create_opts,
    preflight_create_config,
    resolve_create_config,
)
from .log import configure_logging, logger
from . import output
from .profiles import load_profile

from .containers import (
    CONTAINER_CMD,
    SHELL_USER_ROOT,
    get_containers,
    get_container_display_name,
    get_container_mounts,
    get_container_inspect,
    container_exists,
    container_is_mim,
    container_is_running,
    image_exists,
    import_image_archive,
    export_image_archive,
    resolve_container_shell_user,
    resolve_image_identity,
    ensure_runtime_supports_containers,
)
from .integration import (
    destroy_container_data_dir,
    get_container_integration_mounts,
    get_home_integration_mount,
    get_home_integration_env,
    get_app_data_dir,
    map_host_path_to_container,
)
from .inspection import build_container_inspection
from .shell_helpers import (
    get_shell_home_dir,
    get_non_root_shell_identity_args,
    prepare_non_root_shell,
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


def _ensure_runtime_supports_containers_or_exit() -> None:
    try:
        ensure_runtime_supports_containers()
    except RuntimeError as exc:
        logger.error(str(exc))
        raise typer.Exit(1)


def _load_profile_or_exit(profile_name: str | None):
    if profile_name is None:
        return None

    try:
        return load_profile(profile_name)
    except ValueError as exc:
        logger.error(str(exc))
        raise typer.Exit(1)


def _build_create_config(
    profile_name: str | None,
    home_shares: List[str],
    mounts: List[str],
    workspaces: List[str],
    port_binds: List[str],
    devices: List[str],
    host_pid: bool,
    network: str | None,
    privileged: bool,
    keepalive_command: str | None,
    integrate_home: bool,
) -> CreateConfig:
    config = CreateConfig(
        home_shares=tuple(home_shares),
        mounts=tuple(mounts),
        workspaces=tuple(workspaces),
        port_binds=tuple(port_binds),
        devices=tuple(devices),
        host_pid=host_pid,
        network=network,
        privileged=privileged,
        keepalive_command=keepalive_command,
        integrate_home=integrate_home,
    )
    return apply_profile(config, _load_profile_or_exit(profile_name))


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
    _ensure_runtime_supports_containers_or_exit()

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


@app.command(help="export an image to a tar or zstd archive", no_args_is_help=True)
def export(
    image_name: str = typer.Option(
        ...,
        "-n",
        "--image-name",
        help="name of the image to export.",
    ),
    output_path: str = typer.Option(
        ...,
        "-o",
        "--output",
        help="path to the output archive (.tar or .zst).",
    ),
    force: bool = typer.Option(
        False,
        "-f",
        "--force",
        help="overwrite an existing output archive.",
    ),
):
    if not image_exists(image_name):
        logger.error(f"image [{image_name}] does not exist")
        raise typer.Exit(1)

    logger.info(f"exporting image [{image_name}] to [{output_path}]")
    try:
        export_image_archive(image_name, output_path, force=force)
    except (ValueError, RuntimeError) as exc:
        logger.error(str(exc))
        raise typer.Exit(1)

    logger.info(f"image [{image_name}] exported to [{output_path}]")


@app.command(
    name="import",
    help="import an image from a tar or zstd archive",
    no_args_is_help=True,
)
def import_image(
    input_path: str = typer.Option(
        ...,
        "-i",
        "--input",
        help="path to the input archive (.tar or .zst).",
    ),
):
    logger.info(f"importing image archive [{input_path}]")
    try:
        import_image_archive(input_path)
    except (ValueError, RuntimeError) as exc:
        logger.error(str(exc))
        raise typer.Exit(1)

    logger.info(f"image archive [{input_path}] imported")


def _ensure_integration_mount_source(mount) -> None:
    if mount.is_file:
        source_dir = os.path.dirname(mount.source_path)
        if source_dir:
            os.makedirs(source_dir, exist_ok=True)
        if not os.path.exists(mount.source_path):
            logger.trace(
                f"integration mount source [{mount.source_path}] does not exist, "
                "creating empty file"
            )
            open(mount.source_path, "a").close()
            os.chmod(mount.source_path, 0o777)
        return

    if not os.path.exists(mount.source_path):
        logger.trace(
            f"integration mount source [{mount.source_path}] does not exist, "
            "creating directory"
        )
        os.makedirs(mount.source_path, exist_ok=True)


def _append_integration_mounts(
    container_create_opts: list[str],
    container_data_dir: str,
) -> None:
    for mount in get_container_integration_mounts(container_data_dir):
        _ensure_integration_mount_source(mount)
        container_create_opts.extend(
            ["-v", f"{mount.source_path}:{mount.container_path}:rw"]
        )


def _create_container(
    image_name: str,
    container_name: str | None,
    config: CreateConfig,
) -> str:
    if container_name is None:
        container_name = image_name

    if container_exists(container_name):
        logger.error(f"container [{container_name}] already exists")
        raise typer.Exit(1)

    if not image_exists(image_name):
        logger.error(f"image [{image_name}] does not exist")
        raise typer.Exit(1)

    try:
        preflight_create_config(config)
        image_identity = resolve_image_identity(image_name)
        resolved_config = resolve_create_config(config, image_identity.home_dir)
    except (ValueError, RuntimeError) as exc:
        logger.error(str(exc))
        raise typer.Exit(1)

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
        container_create_opts.extend(
            [
                "--userns",
                f"keep-id:uid={image_identity.uid},gid={image_identity.gid}",
            ]
        )

    container_create_opts.extend(
        get_namespace_create_opts(resolved_config.host_pid, resolved_config.network)
    )

    if resolved_config.privileged:
        container_create_opts.append("--privileged")

    if resolved_config.integrate_home:
        container_create_opts.extend(["-v", f"{get_home_integration_mount()}:rw"])
        container_create_opts.extend(["-e", get_home_integration_env()])

    _append_integration_mounts(container_create_opts, container_data_dir)

    for mount in resolved_config.mounts:
        logger.debug(f"adding mount: {mount.volume_arg()}")
        container_create_opts.extend(["-v", mount.volume_arg()])

    for device_spec in resolved_config.device_specs:
        logger.debug(f"adding device passthrough: {device_spec}")
        container_create_opts.extend(["--device", device_spec])

    for port_bind in resolved_config.port_binds:
        container_create_opts.extend(["-p", port_bind])

    logger.info(f"creating mim container [{container_name}] from image [{image_name}]")
    _run_container_cmd(
        "create",
        *container_create_opts,
        image_name,
        *resolved_config.keepalive_args,
        error_action="create",
        format_output=True,
    )
    logger.info(f"container [{container_name}] created")
    return container_name


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
    profile_name: Optional[str] = typer.Option(
        None,
        "-P",
        "--profile",
        help="profile from the mim config to apply.",
    ),
    workspaces: List[str] = typer.Option(
        [],
        "-W",
        "--workspace",
        help=(
            "workspace mount: host_path[:container_path[:ro|rw]]; "
            "defaults to /work/<name>."
        ),
    ),
    home_shares: List[str] = typer.Option(
        [],
        "-H",
        "--home-share",
        help=(
            "passthrough mount under host home; available at identical path "
            "and under container HOME."
        ),
    ),
    port_binds: List[str] = typer.Option(
        [],
        "-p",
        "--port-bind",
        help="port to bind from the host to the container.",
    ),
    mounts: List[str] = typer.Option(
        [],
        "-M",
        "--mount",
        help="mount in format host_path:container_path[:ro|rw].",
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
    network: Optional[str] = typer.Option(
        None,
        "--network",
        help="network mode: default, none, or host.",
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
    config = _build_create_config(
        profile_name,
        home_shares,
        mounts,
        workspaces,
        port_binds,
        devices,
        host_pid,
        network,
        privileged,
        keepalive_command,
        integrate_home,
    )
    _create_container(image_name, container_name, config)


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
    keep_shell_state: bool = typer.Option(
        False,
        "--keep-shell-state",
        help="preserve persisted shell history/state",
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

    container_data_dir = os.path.join(DATA_DIR, container_name)
    if keep_shell_state:
        logger.info(
            f"destroying data directory for container [{container_name}], preserving shell state"
        )
    else:
        logger.info(f"destroying data directory for container [{container_name}]")
    try:
        destroy_container_data_dir(container_data_dir, keep_shell_state)
    except FileNotFoundError:
        logger.debug(f"data directory [{container_data_dir}] already absent")

    logger.info(f"destroying mim container [{container_name}]")
    _run_container_cmd(
        "rm",
        container_name,
        error_action="destroy",
    )

    logger.info(f"container [{container_name}] destroyed")


@app.command(name="inspect", help="inspect a mim container", no_args_is_help=True)
def inspect_container(
    container_name: str = typer.Option(
        ...,
        "-c",
        "--container-name",
        help="name of the container to inspect.",
    ),
):
    _require_mim_container(container_name)

    inspect_data = get_container_inspect(container_name)
    if inspect_data is None:
        logger.error(f"container [{container_name}] could not be inspected")
        raise typer.Exit(1)

    container_data_dir = os.path.join(DATA_DIR, container_name)
    container_inspection = build_container_inspection(
        container_name,
        get_container_runtime(),
        container_data_dir,
        inspect_data,
    )
    output.print_key_value_table("container", container_inspection.basics)
    output.print_table(
        "mounts",
        ["source", "target", "mode"],
        container_inspection.mounts,
    )
    output.print_table("ports", ["container", "host"], container_inspection.ports)
    output.print_table(
        "devices",
        ["host", "container", "permissions"],
        container_inspection.devices,
    )
    output.print_table("env", ["key"], container_inspection.env_keys)


def _shell_container(
    container_name: str,
    shell_command: str,
    as_root: bool,
    as_user: bool,
) -> None:
    _require_mim_container(container_name)

    if not container_is_running(container_name):
        logger.info(f"container [{container_name}] is not running, starting it")
        _ensure_runtime_supports_containers_or_exit()
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
    shell_command_args = shlex.split(shell_command)
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


@app.command(
    name="shell", help="get a shell in a running container", no_args_is_help=True
)
def shell_command(
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
    _shell_container(container_name, shell, as_root, as_user)


@app.command(
    name="enter", help="create if needed, then open a shell", no_args_is_help=True
)
def enter_container(
    image_name: Optional[str] = typer.Option(
        None,
        "-n",
        "--image-name",
        help="image to use if the container must be created.",
    ),
    container_name: Optional[str] = typer.Option(
        None,
        "-c",
        "--container-name",
        help="container to enter; defaults to the image name when creating.",
    ),
    profile_name: Optional[str] = typer.Option(
        None,
        "-P",
        "--profile",
        help="profile from the mim config to apply when creating.",
    ),
    workspaces: List[str] = typer.Option(
        [],
        "-W",
        "--workspace",
        help="workspace mount used when creating: host_path[:container_path[:ro|rw]].",
    ),
    home_shares: List[str] = typer.Option(
        [],
        "-H",
        "--home-share",
        help="home share used when creating.",
    ),
    port_binds: List[str] = typer.Option(
        [],
        "-p",
        "--port-bind",
        help="port bind used when creating.",
    ),
    mounts: List[str] = typer.Option(
        [],
        "-M",
        "--mount",
        help="mount used when creating: host_path:container_path[:ro|rw].",
    ),
    devices: List[str] = typer.Option(
        [],
        "-D",
        "--device",
        help="device passthrough used when creating.",
    ),
    host_pid: bool = typer.Option(
        False,
        "--host-pid",
        help="share the host's PID namespace when creating.",
    ),
    network: Optional[str] = typer.Option(
        None,
        "--network",
        help="network mode used when creating: default, none, or host.",
    ),
    privileged: bool = typer.Option(
        False,
        "--privileged",
        help="run the container in privileged mode when creating.",
    ),
    keepalive_command: Optional[str] = typer.Option(
        None,
        "--keepalive-command",
        help="override the image command used as pid 1 when creating.",
    ),
    integrate_home: bool = typer.Option(
        False,
        "--integrate-home",
        help="mount full host home when creating.",
    ),
    shell_command: str = typer.Option(
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
    if container_name is None:
        if image_name is None:
            logger.error("must provide --container-name or --image-name")
            raise typer.Exit(1)
        container_name = image_name

    if not container_exists(container_name):
        if image_name is None:
            logger.error(
                f"container [{container_name}] does not exist; provide --image-name to create it"
            )
            raise typer.Exit(1)

        config = _build_create_config(
            profile_name,
            home_shares,
            mounts,
            workspaces,
            port_binds,
            devices,
            host_pid,
            network,
            privileged,
            keepalive_command,
            integrate_home,
        )
        _create_container(image_name, container_name, config)

    _shell_container(container_name, shell_command, as_root, as_user)


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
    _ensure_runtime_supports_containers_or_exit()
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


@app.command(name="list", help="list all mim containers")
def list_containers():
    containers = get_containers(only_mim=True)
    rows = [
        (
            get_container_display_name(container),
            str(container.get("State", "unknown")),
        )
        for container in containers
    ]
    output.print_container_list(rows)
