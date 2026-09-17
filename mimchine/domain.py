from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
MACHINE_NAME_PATTERN = re.compile(
    r"[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?)*"
)


@dataclass(frozen=True)
class BuildSpec:
    image: str
    file: Path
    context: Path
    platform: str | None = None
    build_args: tuple[str, ...] = ()
    no_cache: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "image", _require_text(self.image, "image"))
        object.__setattr__(self, "file", _normalize_path(self.file))
        object.__setattr__(self, "context", _normalize_path(self.context))
        object.__setattr__(self, "build_args", tuple(self.build_args))


class NetworkMode(Enum):
    DEFAULT = "default"
    NONE = "none"
    HOST = "host"


class IdentityMode(Enum):
    IMAGE = "image"
    ROOT = "root"
    HOST = "host"


@dataclass(frozen=True)
class MountSpec:
    source: Path
    target: str
    read_only: bool = False
    kind: str = "mount"
    options: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _normalize_path(self.source))
        target = _require_text(self.target, "mount target")
        if not target.startswith("/"):
            raise ValueError(f"mount target must be absolute: {target}")
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "kind", _require_text(self.kind, "mount kind"))
        options = tuple(
            _require_text(option, "mount option") for option in self.options
        )
        for option in options:
            if ":" in option or "," in option:
                raise ValueError(f"mount option cannot contain ':' or ',': {option}")
            if option in {"ro", "rw"}:
                raise ValueError(f"mount option cannot be an access mode: {option}")
        object.__setattr__(self, "options", options)

    @property
    def mode(self) -> str:
        mode = "ro" if self.read_only else "rw"
        if self.options:
            return ",".join((mode, *self.options))
        return mode

    def volume_arg(self) -> str:
        return f"{self.source}:{self.target}:{self.mode}"

    def to_data(self) -> dict[str, Any]:
        return {
            "source": str(self.source),
            "target": self.target,
            "read_only": self.read_only,
            "kind": self.kind,
            "options": list(self.options),
        }

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "MountSpec":
        return cls(
            source=Path(str(data["source"])),
            target=str(data["target"]),
            read_only=_bool_from_data(data.get("read_only", False), "read_only"),
            kind=str(data.get("kind", "mount")),
            options=tuple(str(x) for x in data.get("options", [])),
        )


@dataclass(frozen=True)
class PortBind:
    host: int
    guest: int

    def __post_init__(self) -> None:
        _validate_port(self.host, "host port")
        _validate_port(self.guest, "guest port")

    def arg(self) -> str:
        return f"{self.host}:{self.guest}"

    @classmethod
    def parse(cls, value: str) -> "PortBind":
        parts = value.strip().split(":")
        if len(parts) != 2 or any(part == "" for part in parts):
            raise ValueError(f"invalid port mapping: {value}")
        return cls(host=int(parts[0]), guest=int(parts[1]))

    def to_data(self) -> dict[str, int]:
        return {"host": self.host, "guest": self.guest}

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "PortBind":
        return cls(
            host=_int_from_data(data["host"], "host port"),
            guest=_int_from_data(data["guest"], "guest port"),
        )


@dataclass(frozen=True)
class ResourceSpec:
    cpus: int | None = None
    memory_mib: int | None = None

    def __post_init__(self) -> None:
        for field_name in ("cpus", "memory_mib"):
            value = getattr(self, field_name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{field_name} must be an integer")
            if value <= 0:
                raise ValueError(f"{field_name} must be positive")

    def to_data(self) -> dict[str, int | None]:
        return {
            "cpus": self.cpus,
            "memory_mib": self.memory_mib,
        }

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "ResourceSpec":
        return cls(
            cpus=_optional_int(data.get("cpus")),
            memory_mib=_optional_int(data.get("memory_mib")),
        )


@dataclass(frozen=True)
class MachineRecord:
    name: str
    image: str
    runner: str
    created_at: str
    mounts: tuple[MountSpec, ...] = ()
    ports: tuple[PortBind, ...] = ()
    env: tuple[str, ...] = ()
    workdir: str | None = None
    shell: str | None = None
    network: NetworkMode = NetworkMode.DEFAULT
    identity: IdentityMode = IdentityMode.IMAGE
    resources: ResourceSpec = field(default_factory=ResourceSpec)
    shell_state: bool = True
    ssh_agent: bool = False
    gpu: bool = False
    container_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", validate_machine_name(self.name))
        object.__setattr__(self, "image", _require_text(self.image, "image"))
        object.__setattr__(self, "runner", _require_text(self.runner, "runner"))
        object.__setattr__(
            self, "created_at", _require_text(self.created_at, "created at")
        )
        object.__setattr__(self, "mounts", tuple(self.mounts))
        object.__setattr__(self, "ports", tuple(self.ports))
        object.__setattr__(self, "env", tuple(_parse_env(value) for value in self.env))
        object.__setattr__(self, "network", _enum(NetworkMode, self.network))
        object.__setattr__(self, "identity", _enum(IdentityMode, self.identity))
        object.__setattr__(
            self, "container_args", tuple(str(x) for x in self.container_args)
        )
        _validate_bool(self.ssh_agent, "ssh_agent")
        _validate_bool(self.gpu, "gpu")
        _validate_bool(self.shell_state, "shell_state")

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "name": self.name,
            "runner": self.runner,
            "image": self.image,
            "mounts": [m.to_data() for m in self.mounts],
            "ports": [p.to_data() for p in self.ports],
            "env": list(self.env),
            "workdir": self.workdir,
            "shell": self.shell,
            "network": self.network.value,
            "identity": self.identity.value,
            "resources": self.resources.to_data(),
            "shell_state": self.shell_state,
            "ssh_agent": self.ssh_agent,
            "gpu": self.gpu,
            "container_args": list(self.container_args),
            "created_at": self.created_at,
        }

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "MachineRecord":
        schema_version = _int_from_data(data["schema_version"], "schema_version")
        if schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported machine record schema: {schema_version}")
        return cls(
            name=str(data["name"]),
            image=str(data["image"]),
            runner=str(data["runner"]),
            created_at=str(data["created_at"]),
            mounts=tuple(MountSpec.from_data(dict(x)) for x in data.get("mounts", [])),
            ports=tuple(PortBind.from_data(dict(x)) for x in data.get("ports", [])),
            env=tuple(str(x) for x in data.get("env", [])),
            workdir=data.get("workdir"),
            shell=data.get("shell"),
            network=NetworkMode(str(data.get("network", NetworkMode.DEFAULT.value))),
            identity=IdentityMode(str(data.get("identity", IdentityMode.IMAGE.value))),
            resources=ResourceSpec.from_data(dict(data.get("resources", {}))),
            shell_state=_bool_from_data(data.get("shell_state", True), "shell_state"),
            ssh_agent=_bool_from_data(data.get("ssh_agent", False), "ssh_agent"),
            gpu=_bool_from_data(data.get("gpu", False), "gpu"),
            container_args=tuple(str(x) for x in data.get("container_args", [])),
        )


@dataclass(frozen=True)
class ExecSpec:
    command: tuple[str, ...]
    interactive: bool = False
    tty: bool = False
    env: tuple[str, ...] = ()
    workdir: str | None = None

    def __post_init__(self) -> None:
        command = tuple(str(part) for part in self.command)
        if len(command) == 0:
            raise ValueError("command cannot be empty")
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "env", tuple(_parse_env(value) for value in self.env))


class RuntimeState(Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    MISSING = "missing"
    UNKNOWN = "unknown"


def validate_machine_name(name: str) -> str:
    text = _require_text(name, "machine name")
    if MACHINE_NAME_PATTERN.fullmatch(text) is None:
        raise ValueError(
            "machine names must use lowercase letters, numbers, or dots, with "
            "hyphens and underscores only inside labels"
        )
    return text


def _parse_env(value: str) -> str:
    text = value.strip()
    if "=" not in text:
        raise ValueError(f"environment value must be KEY=VALUE: {value}")
    key, _ = text.split("=", 1)
    if not key:
        raise ValueError(f"environment key cannot be empty: {value}")
    return text


def _require_text(value: str, label: str) -> str:
    text = str(value).strip()
    if len(text) == 0:
        raise ValueError(f"{label} cannot be empty")
    return text


def _normalize_path(path: str | Path) -> Path:
    return Path(os.path.expanduser(str(path))).resolve()


def _enum(enum_type: type[Enum], value: Any) -> Any:
    if isinstance(value, enum_type):
        return value
    return enum_type(str(value))


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return _int_from_data(value, "integer value")


def _int_from_data(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _validate_port(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if value < 1 or value > 65535:
        raise ValueError(f"{label} must be between 1 and 65535")


def _validate_bool(value: bool, label: str) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")


def _bool_from_data(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value
