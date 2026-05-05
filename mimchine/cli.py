from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from . import __VERSION__
from .domain import ExecSpec, IdentityMode, IdentitySpec, NetworkMode, ResourceSpec
from .log import configure_logging, logger
from .output import (
    print_key_value_table,
    print_machine_list,
    print_table,
    print_version,
)
from .process import ProcessError
from .services import BuildOptions, BuildService, CreateOptions, MachineService
from .state import MachineNotFoundError

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}
APP_NAME = "mimchine"

app = typer.Typer(
    name="mim",
    help="Ergonomic local development machines.",
    no_args_is_help=True,
    context_settings=CONTEXT_SETTINGS,
    pretty_exceptions_show_locals=False,
)


def version_callback(value: bool) -> None:
    if value:
        print_version(APP_NAME, __VERSION__)
        raise typer.Exit()


@app.callback()
def app_callback(
    verbose: list[bool] = typer.Option([], "--verbose", "-v", help="Verbose output."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Quiet output."),
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        "-V",
        callback=version_callback,
        is_eager=True,
        help="Print version and exit.",
    ),
) -> None:
    configure_logging(len(verbose), quiet)


@app.command(no_args_is_help=True)
def build(
    image: str = typer.Argument(..., help="Image tag to build."),
    file: Path = typer.Option(
        ...,
        "--file",
        "-f",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        help="Containerfile or Dockerfile.",
    ),
    context: Path = typer.Option(
        Path("."),
        "--context",
        "-C",
        exists=True,
        file_okay=False,
        dir_okay=True,
        readable=True,
        help="Build context directory.",
    ),
    builder: Optional[str] = typer.Option(
        None,
        "--builder",
        "-b",
        help="Builder backend: podman or docker.",
    ),
    platform: Optional[str] = typer.Option(None, "--platform", help="Target platform."),
    build_args: list[str] = typer.Option(
        [],
        "--build-arg",
        help="Build-time variable, KEY=VALUE. May be repeated.",
    ),
) -> None:
    _run(
        lambda: BuildService.default().build(
            BuildOptions(
                image=image,
                file=file,
                context=context,
                builder=builder,
                platform=platform,
                build_args=tuple(build_args),
            )
        )
    )


@app.command(no_args_is_help=True)
def create(
    name: str = typer.Argument(..., help="Machine name."),
    image: Optional[str] = typer.Option(
        None,
        "--image",
        "-i",
        help="Image tag or .smolmachine path.",
    ),
    runner: Optional[str] = typer.Option(
        None,
        "--runner",
        "-r",
        help="Execution backend: podman, docker, or smolvm.",
    ),
    profile: Optional[str] = typer.Option(
        None, "--profile", "-P", help="Profile name."
    ),
    workspaces: list[str] = typer.Option(
        [],
        "--workspace",
        "-W",
        help="Mount host directory at /work/NAME, or HOST:GUEST[:ro].",
    ),
    mounts: list[str] = typer.Option(
        [],
        "--mount",
        "-M",
        help="Mount host path as HOST:GUEST[:ro].",
    ),
    ports: list[str] = typer.Option(
        [],
        "--port",
        "-p",
        help="Publish TCP port as HOST:GUEST.",
    ),
    env: list[str] = typer.Option(
        [],
        "--env",
        "-e",
        help="Environment variable, KEY=VALUE. May be repeated.",
    ),
    workdir: Optional[str] = typer.Option(
        None,
        "--workdir",
        "-w",
        help="Working directory inside the machine.",
    ),
    shell: Optional[str] = typer.Option(
        None,
        "--shell",
        "-s",
        help="Shell command used by enter.",
    ),
    net: bool = typer.Option(False, "--net", help="Enable outbound networking."),
    no_net: bool = typer.Option(False, "--no-net", help="Disable outbound networking."),
    host_net: bool = typer.Option(False, "--host-net", help="Use host networking."),
    allow_hosts: list[str] = typer.Option(
        [],
        "--allow-host",
        help="Allow outbound traffic to hostname. May be repeated.",
    ),
    allow_cidrs: list[str] = typer.Option(
        [],
        "--allow-cidr",
        help="Allow outbound traffic to CIDR. May be repeated.",
    ),
    ssh_agent: bool = typer.Option(
        False, "--ssh-agent", help="Forward the host SSH agent."
    ),
    no_ssh_agent: bool = typer.Option(
        False,
        "--no-ssh-agent",
        help="Disable SSH agent forwarding.",
    ),
    gpu: bool = typer.Option(False, "--gpu", help="Request backend GPU support."),
    no_gpu: bool = typer.Option(False, "--no-gpu", help="Disable backend GPU support."),
    cpus: Optional[int] = typer.Option(None, "--cpus", "-c", min=1, help="vCPU count."),
    mem: Optional[int] = typer.Option(
        None, "--mem", "--memory", min=1, help="Memory MiB."
    ),
    storage: Optional[int] = typer.Option(
        None, "--storage", min=1, help="Storage GiB."
    ),
    overlay: Optional[int] = typer.Option(
        None, "--overlay", min=1, help="Overlay GiB."
    ),
    root: bool = typer.Option(False, "--root", help="Run as root."),
    host_user: bool = typer.Option(False, "--host-user", help="Run as the host user."),
    shell_state: bool = typer.Option(
        True,
        "--shell-state/--no-shell-state",
        help="Persist shell history for enter.",
    ),
) -> None:
    _run(
        lambda: MachineService.default().create(
            CreateOptions(
                name=name,
                image=image,
                runner=runner,
                profile=profile,
                workspaces=tuple(workspaces),
                mounts=tuple(mounts),
                ports=tuple(ports),
                env=tuple(env),
                workdir=workdir,
                shell=shell,
                network=_network_from_flags(net, no_net, host_net),
                allow_hosts=tuple(allow_hosts),
                allow_cidrs=tuple(allow_cidrs),
                ssh_agent=_optional_bool_flag(ssh_agent, no_ssh_agent, "ssh-agent"),
                gpu=_optional_bool_flag(gpu, no_gpu, "gpu"),
                resources=ResourceSpec(
                    cpus=cpus,
                    memory_mib=mem,
                    storage_gib=storage,
                    overlay_gib=overlay,
                ),
                identity=_identity_from_flags(root, host_user),
                shell_state=shell_state,
            )
        )
    )


@app.command(no_args_is_help=True)
def enter(
    name: str = typer.Argument(..., help="Machine name."),
    shell: Optional[str] = typer.Option(
        None,
        "--shell",
        "-s",
        help="Shell command to run.",
    ),
) -> None:
    _run(lambda: MachineService.default().enter(name, shell))


@app.command(
    name="exec",
    no_args_is_help=True,
    context_settings={
        "allow_interspersed_args": False,
        "ignore_unknown_options": True,
    },
)
def exec_command(
    name: str = typer.Argument(..., help="Machine name."),
    command: list[str] = typer.Argument(..., help="Command to run."),
    interactive: bool = typer.Option(
        False, "--interactive", "-i", help="Keep stdin open."
    ),
    tty: bool = typer.Option(False, "--tty", "-t", help="Allocate a TTY."),
    env: list[str] = typer.Option(
        [],
        "--env",
        "-e",
        help="Environment variable, KEY=VALUE. May be repeated.",
    ),
    workdir: Optional[str] = typer.Option(
        None,
        "--workdir",
        "-w",
        help="Working directory inside the machine.",
    ),
    stream: bool = typer.Option(
        False, "--stream", help="Stream output when supported."
    ),
) -> None:
    guest_command = _guest_command(command)
    _run(
        lambda: MachineService.default().exec(
            name,
            ExecSpec(
                command=guest_command,
                interactive=interactive,
                tty=tty,
                env=tuple(env),
                workdir=workdir,
                stream=stream,
            ),
        )
    )


def _guest_command(command: list[str]) -> tuple[str, ...]:
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("command is required")
    return tuple(command)


@app.command(no_args_is_help=True)
def start(name: str = typer.Argument(..., help="Machine name.")) -> None:
    _run(lambda: MachineService.default().start(name))


@app.command(no_args_is_help=True)
def stop(name: str = typer.Argument(..., help="Machine name.")) -> None:
    _run(lambda: MachineService.default().stop(name))


@app.command(no_args_is_help=True)
def delete(
    name: str = typer.Argument(..., help="Machine name."),
    force: bool = typer.Option(
        False, "--force", "-f", help="Do not ask for confirmation."
    ),
    keep_shell_state: bool = typer.Option(
        False,
        "--keep-shell-state",
        help="Leave shell history/state on disk.",
    ),
) -> None:
    if not force and not typer.confirm(f"delete machine [{name}]?"):
        raise typer.Exit()
    _run(
        lambda: MachineService.default().delete(name, keep_shell_state=keep_shell_state)
    )


@app.command(name="list")
def list_machines() -> None:
    _run(lambda: print_machine_list(MachineService.default().list()))


@app.command(no_args_is_help=True)
def inspect(name: str = typer.Argument(..., help="Machine name.")) -> None:
    def action() -> None:
        view = MachineService.default().inspect(name)
        record = view.record
        print_key_value_table(
            f"mimchine {record.name}",
            [
                ("name", record.name),
                ("runner", record.runner),
                ("backend_id", record.backend_id),
                ("state", view.status.state.value),
                ("image", record.image.display()),
                ("network", record.network.mode.value),
                ("workdir", record.workdir or ""),
                ("shell", record.shell or ""),
                ("created", record.created_at),
            ],
        )
        print_table(
            "mounts",
            ["source", "target", "mode", "kind"],
            [
                (str(mount.source), mount.target, mount.mode, mount.kind)
                for mount in record.mounts
            ],
        )
        print_table(
            "ports",
            ["host", "guest"],
            [(str(port.host), str(port.guest)) for port in record.ports],
        )

    _run(action)


def _run(action) -> None:
    try:
        action()
    except MachineNotFoundError as exc:
        logger.error(f"machine [{exc.args[0]}] does not exist")
        raise typer.Exit(1) from exc
    except ProcessError as exc:
        message = exc.result.stderr.strip() or str(exc)
        logger.error(message)
        raise typer.Exit(exc.result.returncode) from exc
    except ValueError as exc:
        logger.error(str(exc))
        raise typer.Exit(1) from exc


def _network_from_flags(
    net: bool,
    no_net: bool,
    host_net: bool,
) -> NetworkMode | None:
    if net and no_net:
        raise ValueError("cannot use --net with --no-net")
    if host_net and no_net:
        raise ValueError("cannot use --host-net with --no-net")
    if host_net:
        return NetworkMode.HOST
    if net:
        return NetworkMode.DEFAULT
    if no_net:
        return NetworkMode.NONE
    return None


def _optional_bool_flag(enabled: bool, disabled: bool, label: str) -> bool | None:
    if enabled and disabled:
        raise ValueError(f"cannot use --{label} with --no-{label}")
    if enabled:
        return True
    if disabled:
        return False
    return None


def _identity_from_flags(root: bool, host_user: bool) -> IdentitySpec | None:
    if root and host_user:
        raise ValueError("cannot use --root with --host-user")
    if root:
        return IdentitySpec(IdentityMode.ROOT)
    if host_user:
        return IdentitySpec(IdentityMode.HOST)
    return None
