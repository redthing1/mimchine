from dataclasses import dataclass
from typing import Any

from .config import load_config

PROFILE_KEYS = {
    "workspace",
    "workspaces",
    "mount",
    "mounts",
    "home_share",
    "home_shares",
    "port",
    "ports",
    "port_bind",
    "port_binds",
    "device",
    "devices",
    "network",
    "host_pid",
    "privileged",
    "integrate_home",
    "keepalive_command",
}


@dataclass(frozen=True)
class Profile:
    name: str
    workspaces: tuple[str, ...]
    mounts: tuple[str, ...]
    home_shares: tuple[str, ...]
    port_binds: tuple[str, ...]
    devices: tuple[str, ...]
    network: str | None
    host_pid: bool
    privileged: bool
    integrate_home: bool
    keepalive_command: str | None


def _read_str_tuple(
    profile_name: str,
    data: dict[str, Any],
    *keys: str,
) -> tuple[str, ...]:
    values: list[str] = []
    for key in keys:
        value = data.get(key)
        if value is None:
            continue

        if isinstance(value, str):
            values.append(value)
            continue

        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            values.extend(value)
            continue

        raise ValueError(
            f"profile [{profile_name}] field [{key}] must be a string or list of strings"
        )

    return tuple(values)


def _read_optional_str(
    profile_name: str,
    data: dict[str, Any],
    key: str,
) -> str | None:
    value = data.get(key)
    if value is None:
        return None

    if not isinstance(value, str):
        raise ValueError(f"profile [{profile_name}] field [{key}] must be a string")

    stripped_value = value.strip()
    if len(stripped_value) == 0:
        return None

    return stripped_value


def _read_bool(profile_name: str, data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if value is None:
        return False

    if not isinstance(value, bool):
        raise ValueError(f"profile [{profile_name}] field [{key}] must be a boolean")

    return value


def _read_profile(profile_name: str, data: dict[str, Any]) -> Profile:
    unknown_keys = sorted(set(data.keys()) - PROFILE_KEYS)
    if len(unknown_keys) > 0:
        unknown_key_list = ", ".join(unknown_keys)
        raise ValueError(
            f"profile [{profile_name}] has unknown field(s): {unknown_key_list}"
        )

    return Profile(
        name=profile_name,
        workspaces=_read_str_tuple(profile_name, data, "workspace", "workspaces"),
        mounts=_read_str_tuple(profile_name, data, "mount", "mounts"),
        home_shares=_read_str_tuple(profile_name, data, "home_share", "home_shares"),
        port_binds=_read_str_tuple(
            profile_name,
            data,
            "port",
            "ports",
            "port_bind",
            "port_binds",
        ),
        devices=_read_str_tuple(profile_name, data, "device", "devices"),
        network=_read_optional_str(profile_name, data, "network"),
        host_pid=_read_bool(profile_name, data, "host_pid"),
        privileged=_read_bool(profile_name, data, "privileged"),
        integrate_home=_read_bool(profile_name, data, "integrate_home"),
        keepalive_command=_read_optional_str(profile_name, data, "keepalive_command"),
    )


def load_profile(profile_name: str) -> Profile:
    normalized_profile_name = profile_name.strip()
    if len(normalized_profile_name) == 0:
        raise ValueError("profile name cannot be empty")

    profiles = load_config().get("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("config field [profiles] must be a table")

    profile_data = profiles.get(normalized_profile_name)
    if profile_data is None:
        raise ValueError(f"profile [{normalized_profile_name}] was not found")

    if not isinstance(profile_data, dict):
        raise ValueError(f"profile [{normalized_profile_name}] must be a table")

    return _read_profile(normalized_profile_name, profile_data)
