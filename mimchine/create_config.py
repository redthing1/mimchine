import os
import posixpath
import shlex
from dataclasses import dataclass

from .integration import get_home_dir
from .log import logger
from .mounts import MountSpec, parse_mount_spec, parse_workspace_spec
from .paths import normalize_host_path
from .profiles import Profile

NETWORK_DEFAULT = "default"
NETWORK_NONE = "none"
NETWORK_HOST = "host"
SUPPORTED_NETWORK_MODES = (NETWORK_DEFAULT, NETWORK_NONE, NETWORK_HOST)
SUPPORTED_NETWORK_MODES_STR = ", ".join(SUPPORTED_NETWORK_MODES)


@dataclass(frozen=True)
class CreateConfig:
    home_shares: tuple[str, ...]
    mounts: tuple[str, ...]
    workspaces: tuple[str, ...]
    port_binds: tuple[str, ...]
    devices: tuple[str, ...]
    host_pid: bool
    network: str | None
    privileged: bool
    keepalive_command: str | None
    integrate_home: bool


@dataclass(frozen=True)
class ResolvedCreateConfig:
    keepalive_args: tuple[str, ...]
    mounts: tuple[MountSpec, ...]
    device_specs: tuple[str, ...]
    port_binds: tuple[str, ...]
    host_pid: bool
    network: str
    privileged: bool
    integrate_home: bool


def apply_profile(config: CreateConfig, profile: Profile | None) -> CreateConfig:
    if profile is None:
        return config

    return CreateConfig(
        home_shares=(*profile.home_shares, *config.home_shares),
        mounts=(*profile.mounts, *config.mounts),
        workspaces=(*profile.workspaces, *config.workspaces),
        port_binds=(*profile.port_binds, *config.port_binds),
        devices=(*profile.devices, *config.devices),
        host_pid=profile.host_pid or config.host_pid,
        network=config.network if config.network is not None else profile.network,
        privileged=profile.privileged or config.privileged,
        keepalive_command=(
            config.keepalive_command
            if config.keepalive_command is not None
            else profile.keepalive_command
        ),
        integrate_home=profile.integrate_home or config.integrate_home,
    )


def normalize_network_mode(network: str | None) -> str:
    if network is None:
        return NETWORK_DEFAULT

    normalized_network = network.strip().lower()
    if normalized_network not in SUPPORTED_NETWORK_MODES:
        raise ValueError(
            f"invalid network mode [{network}], expected one of: {SUPPORTED_NETWORK_MODES_STR}"
        )

    return normalized_network


def get_network_create_opts(network: str) -> list[str]:
    if network == NETWORK_DEFAULT:
        return []

    return [f"--network={network}"]


def validate_network_options(network: str, port_binds: tuple[str, ...]) -> None:
    if network != NETWORK_DEFAULT and len(port_binds) > 0:
        raise ValueError(
            f"cannot use --network {network} with --port-bind; "
            "port publishing only applies to default networking"
        )


def get_namespace_create_opts(host_pid: bool, network: str) -> list[str]:
    opts: list[str] = []

    if host_pid:
        opts.append("--pid=host")

    opts.extend(get_network_create_opts(network))
    return opts


def preflight_create_config(config: CreateConfig) -> None:
    network = normalize_network_mode(config.network)
    validate_network_options(network, config.port_binds)


def _get_home_share_mounts(
    home_shares: tuple[str, ...],
    image_home_dir: str,
) -> tuple[MountSpec, ...]:
    if len(home_shares) == 0:
        return ()

    user_home_dir = normalize_host_path(get_home_dir())
    mounted_pairs: set[tuple[str, str]] = set()
    mounts: list[MountSpec] = []

    for home_share_input in home_shares:
        home_share_src_abs = normalize_host_path(home_share_input)

        if not os.path.exists(home_share_src_abs):
            logger.warn(f"home share [{home_share_src_abs}] does not exist, skipping")
            continue

        if os.path.commonpath([home_share_src_abs, user_home_dir]) != user_home_dir:
            logger.warn(
                f"home share [{home_share_src_abs}] is not under the user's "
                "home directory, skipping"
            )
            continue

        home_share_pair = (home_share_src_abs, home_share_src_abs)
        if home_share_pair not in mounted_pairs:
            mounts.append(MountSpec(home_share_pair[0], home_share_pair[1]))
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
                mounts.append(
                    MountSpec(home_share_tilde_pair[0], home_share_tilde_pair[1])
                )
                mounted_pairs.add(home_share_tilde_pair)

    return tuple(mounts)


def _parse_device_specs(devices: tuple[str, ...]) -> tuple[str, ...]:
    parsed_devices: list[str] = []

    for device_spec_input in devices:
        device_spec = device_spec_input.strip()
        if len(device_spec) == 0:
            raise ValueError("device spec cannot be empty")

        host_device_path = normalize_host_path(device_spec.split(":", 1)[0].strip())
        if not os.path.exists(host_device_path):
            raise ValueError(f"device path [{host_device_path}] does not exist")

        parsed_devices.append(device_spec)

    return tuple(parsed_devices)


def _parse_keepalive_args(keepalive_command: str | None) -> tuple[str, ...]:
    if keepalive_command is None:
        return ()

    keepalive_args = shlex.split(keepalive_command)
    if len(keepalive_args) == 0:
        raise ValueError("keepalive command cannot be empty")

    return tuple(keepalive_args)


def resolve_create_config(
    config: CreateConfig,
    image_home_dir: str,
) -> ResolvedCreateConfig:
    network = normalize_network_mode(config.network)
    validate_network_options(network, config.port_binds)

    mounts = (
        *(parse_workspace_spec(workspace) for workspace in config.workspaces),
        *_get_home_share_mounts(config.home_shares, image_home_dir),
        *(parse_mount_spec(mount) for mount in config.mounts),
    )

    return ResolvedCreateConfig(
        keepalive_args=_parse_keepalive_args(config.keepalive_command),
        mounts=mounts,
        device_specs=_parse_device_specs(config.devices),
        port_binds=config.port_binds,
        host_pid=config.host_pid,
        network=network,
        privileged=config.privileged,
        integrate_home=config.integrate_home,
    )
