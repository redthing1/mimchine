"""Render container creation without probing images, the host, or an engine."""

from __future__ import annotations

from dataclasses import replace

from ..domain import IsolationMode, MachineSpec, MountSpec, NetworkMode
from .lifecycle import KEEPALIVE_ARGV


def create_command(
    spec: MachineSpec,
    *,
    prefix: tuple[str, ...],
    identity_args: tuple[str, ...] = (),
    engine_args: tuple[str, ...] = (),
    feature_args: tuple[str, ...] = (),
    publish_address: str | None = None,
    relabel_shell_state: bool = True,
    labels: tuple[tuple[str, str], ...] = (),
    command: tuple[str, ...] = KEEPALIVE_ARGV,
) -> tuple[str, ...]:
    validate_container_args(spec.container_args)
    args = [*prefix, "create", "--name", spec.name, "--label", "mimchine=1"]
    for key, value in labels:
        args.extend(("--label", f"{key}={value}"))
    args.extend(identity_args)
    if spec.network is not NetworkMode.DEFAULT:
        args.extend(("--network", spec.network.value))
    args.extend(engine_args)
    if spec.resources.cpus:
        args.extend(("--cpus", str(spec.resources.cpus)))
    if spec.resources.memory_mib:
        args.extend(("--memory", f"{spec.resources.memory_mib}m"))
        # Engine defaults couple swap to memory. Mim leaves swap independent.
        swap = spec.resources.swap_mib
        total = "-1" if swap is None else f"{spec.resources.memory_mib + swap}m"
        args.append(f"--memory-swap={total}")
    elif spec.resources.swap_mib is not None and spec.isolation is IsolationMode.NATIVE:
        raise ValueError("native swap limits require a memory limit")
    args.append(
        f"--pids-limit={spec.resources.tasks if spec.resources.tasks is not None else -1}"
    )
    for env in spec.env:
        args.extend(("-e", env))
    if spec.workdir:
        args.extend(("-w", spec.workdir))
    for mount in spec.mounts:
        args.extend(("-v", _volume_arg(mount, relabel_shell_state)))
    for port in spec.ports:
        if publish_address is not None and port.address is None:
            port = replace(port, address=publish_address)
        published = port.arg()
        args.extend(("-p", published))
    args.extend(feature_args)
    args.extend(spec.container_args)
    args.extend(("--entrypoint", command[0], "--", spec.image, *command[1:]))
    return tuple(args)


def validate_container_args(args: tuple[str, ...]) -> None:
    owned = {
        "--name",
        "--replace",
        "--rm",
        "--cidfile",
        "--label",
        "--label-file",
        "--pod",
        "--pod-id-file",
    }
    if any(
        arg == "--" or arg.split("=", 1)[0] in owned or arg.startswith("-l")
        for arg in args
    ):
        raise ValueError(
            "container arguments cannot override machine identity or lifecycle ownership"
        )


def _volume_arg(mount: MountSpec, relabel_shell_state: bool) -> str:
    if (
        relabel_shell_state
        and mount.kind == "shell_state"
        and not {"z", "Z"} & set(mount.options)
    ):
        return f"{mount.volume_arg()},Z"
    return mount.volume_arg()
