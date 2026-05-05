from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import AppConfig, validate_runner
from .domain import IdentityMode, IdentitySpec, NetworkMode


PROFILE_KEYS = {
    "image",
    "runner",
    "workspace",
    "workspaces",
    "mount",
    "mounts",
    "port",
    "ports",
    "env",
    "workdir",
    "network",
    "identity",
    "shell",
    "ssh_agent",
    "gpu",
    "cpus",
    "memory",
    "storage",
    "overlay",
}


@dataclass(frozen=True)
class Profile:
    name: str
    image: str | None = None
    runner: str | None = None
    workspaces: tuple[str, ...] = ()
    mounts: tuple[str, ...] = ()
    ports: tuple[str, ...] = ()
    env: tuple[str, ...] = ()
    workdir: str | None = None
    network: NetworkMode | None = None
    identity: IdentitySpec | None = None
    shell: str | None = None
    ssh_agent: bool | None = None
    gpu: bool | None = None
    cpus: int | None = None
    memory: int | None = None
    storage: int | None = None
    overlay: int | None = None


def load_profile(config: AppConfig, name: str | None) -> Profile | None:
    if name is None:
        return None
    profile_name = name.strip()
    if not profile_name:
        raise ValueError("profile name cannot be empty")

    data = config.profiles.get(profile_name)
    if data is None:
        raise ValueError(f"profile [{profile_name}] was not found")
    return read_profile(profile_name, data)


def read_profile(name: str, data: dict[str, Any]) -> Profile:
    unknown = sorted(set(data) - PROFILE_KEYS)
    if unknown:
        raise ValueError(f"profile [{name}] has unknown field(s): {', '.join(unknown)}")

    runner = _optional_text(data.get("runner"))
    if runner is not None:
        validate_runner(runner)

    network_text = _optional_text(data.get("network"))
    network = None if network_text is None else NetworkMode(network_text)
    identity_text = _optional_text(data.get("identity"))
    identity = None if identity_text is None else IdentitySpec(IdentityMode(identity_text))

    return Profile(
        name=name,
        image=_optional_text(data.get("image")),
        runner=runner,
        workspaces=_read_str_tuple(data, "workspace", "workspaces"),
        mounts=_read_str_tuple(data, "mount", "mounts"),
        ports=_read_str_tuple(data, "port", "ports"),
        env=_read_str_tuple(data, "env"),
        workdir=_optional_text(data.get("workdir")),
        network=network,
        identity=identity,
        shell=_optional_text(data.get("shell")),
        ssh_agent=_optional_bool(data.get("ssh_agent")),
        gpu=_optional_bool(data.get("gpu")),
        cpus=_optional_int(data.get("cpus")),
        memory=_optional_int(data.get("memory")),
        storage=_optional_int(data.get("storage")),
        overlay=_optional_int(data.get("overlay")),
    )


def _read_str_tuple(data: dict[str, Any], *keys: str) -> tuple[str, ...]:
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
        raise ValueError(f"profile field [{key}] must be a string or list of strings")
    return tuple(values)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expected string value")
    text = value.strip()
    return text or None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError("expected boolean value")
    return value


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("expected integer value")
    return value
