from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


class ImageSourceKind(Enum):
    OCI_REFERENCE = "oci_reference"
    SMOLMACHINE = "smolmachine"


@dataclass(frozen=True)
class ImageSource:
    kind: ImageSourceKind
    value: str

    @classmethod
    def oci_reference(cls, value: str) -> "ImageSource":
        return cls(ImageSourceKind.OCI_REFERENCE, _require_text(value, "image"))

    @classmethod
    def smolmachine(cls, path: str | Path) -> "ImageSource":
        return cls(ImageSourceKind.SMOLMACHINE, str(_normalize_path(path)))

    @classmethod
    def from_cli(cls, value: str) -> "ImageSource":
        text = _require_text(value, "image")
        expanded = Path(os.path.expanduser(text))
        if text.endswith(".smolmachine"):
            return cls.smolmachine(expanded)
        return cls.oci_reference(text)

    def display(self) -> str:
        return self.value

    def to_data(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "value": self.value}

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "ImageSource":
        return cls(
            kind=ImageSourceKind(str(data["kind"])),
            value=str(data["value"]),
        )


@dataclass(frozen=True)
class BuildSpec:
    image: str
    file: Path
    context: Path
    builder: str
    platform: str | None = None
    build_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "image", _require_text(self.image, "image"))
        object.__setattr__(self, "builder", _require_text(self.builder, "builder"))
        object.__setattr__(self, "file", _normalize_path(self.file))
        object.__setattr__(self, "context", _normalize_path(self.context))
        object.__setattr__(self, "build_args", tuple(self.build_args))


class NetworkMode(Enum):
    DEFAULT = "default"
    NONE = "none"
    HOST = "host"


@dataclass(frozen=True)
class NetworkSpec:
    mode: NetworkMode = NetworkMode.DEFAULT
    allow_hosts: tuple[str, ...] = ()
    allow_cidrs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", _enum(NetworkMode, self.mode))
        object.__setattr__(self, "allow_hosts", tuple(self.allow_hosts))
        object.__setattr__(self, "allow_cidrs", tuple(self.allow_cidrs))
        if self.mode is NetworkMode.NONE and (self.allow_hosts or self.allow_cidrs):
            raise ValueError("restricted network rules cannot be combined with no network")

    def to_data(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "allow_hosts": list(self.allow_hosts),
            "allow_cidrs": list(self.allow_cidrs),
        }

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "NetworkSpec":
        return cls(
            mode=NetworkMode(str(data.get("mode", NetworkMode.DEFAULT.value))),
            allow_hosts=tuple(str(x) for x in data.get("allow_hosts", [])),
            allow_cidrs=tuple(str(x) for x in data.get("allow_cidrs", [])),
        )


class IdentityMode(Enum):
    IMAGE = "image"
    ROOT = "root"
    HOST = "host"


@dataclass(frozen=True)
class IdentitySpec:
    mode: IdentityMode = IdentityMode.IMAGE

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", _enum(IdentityMode, self.mode))

    def to_data(self) -> dict[str, Any]:
        return {"mode": self.mode.value}

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "IdentitySpec":
        return cls(mode=IdentityMode(str(data.get("mode", IdentityMode.IMAGE.value))))


@dataclass(frozen=True)
class MountSpec:
    source: Path
    target: str
    read_only: bool = False
    kind: str = "mount"

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _normalize_path(self.source))
        target = _require_text(self.target, "mount target")
        if not target.startswith("/"):
            raise ValueError(f"mount target must be absolute: {target}")
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "kind", _require_text(self.kind, "mount kind"))

    @property
    def mode(self) -> str:
        return "ro" if self.read_only else "rw"

    def volume_arg(self) -> str:
        return f"{self.source}:{self.target}:{self.mode}"

    def to_data(self) -> dict[str, Any]:
        return {
            "source": str(self.source),
            "target": self.target,
            "read_only": self.read_only,
            "kind": self.kind,
        }

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "MountSpec":
        return cls(
            source=Path(str(data["source"])),
            target=str(data["target"]),
            read_only=_bool_from_data(data.get("read_only", False), "read_only"),
            kind=str(data.get("kind", "mount")),
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
    storage_gib: int | None = None
    overlay_gib: int | None = None

    def __post_init__(self) -> None:
        for field_name in ("cpus", "memory_mib", "storage_gib", "overlay_gib"):
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
            "storage_gib": self.storage_gib,
            "overlay_gib": self.overlay_gib,
        }

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "ResourceSpec":
        return cls(
            cpus=_optional_int(data.get("cpus")),
            memory_mib=_optional_int(data.get("memory_mib")),
            storage_gib=_optional_int(data.get("storage_gib")),
            overlay_gib=_optional_int(data.get("overlay_gib")),
        )


@dataclass(frozen=True)
class ShellStateSpec:
    enabled: bool = True

    def __post_init__(self) -> None:
        _validate_bool(self.enabled, "shell_state.enabled")

    def to_data(self) -> dict[str, bool]:
        return {"enabled": self.enabled}

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "ShellStateSpec":
        return cls(enabled=_bool_from_data(data.get("enabled", True), "enabled"))


@dataclass(frozen=True)
class MachineSpec:
    name: str
    image: ImageSource
    runner: str
    mounts: tuple[MountSpec, ...] = ()
    ports: tuple[PortBind, ...] = ()
    env: tuple[str, ...] = ()
    workdir: str | None = None
    shell: str | None = None
    network: NetworkSpec = field(default_factory=NetworkSpec)
    identity: IdentitySpec = field(default_factory=IdentitySpec)
    resources: ResourceSpec = field(default_factory=ResourceSpec)
    shell_state: ShellStateSpec = field(default_factory=ShellStateSpec)
    ssh_agent: bool = False
    gpu: bool = False

    def __post_init__(self) -> None:
        validate_machine_name(self.name)
        object.__setattr__(self, "runner", _require_text(self.runner, "runner"))
        object.__setattr__(self, "mounts", tuple(self.mounts))
        object.__setattr__(self, "ports", tuple(self.ports))
        object.__setattr__(self, "env", tuple(self.env))
        _validate_bool(self.ssh_agent, "ssh_agent")
        _validate_bool(self.gpu, "gpu")


@dataclass(frozen=True)
class MachineRecord:
    schema_version: int
    name: str
    runner: str
    backend_id: str
    image: ImageSource
    mounts: tuple[MountSpec, ...]
    ports: tuple[PortBind, ...]
    env: tuple[str, ...]
    workdir: str | None
    shell: str | None
    network: NetworkSpec
    identity: IdentitySpec
    resources: ResourceSpec
    shell_state: ShellStateSpec
    ssh_agent: bool
    gpu: bool
    created_at: str

    @classmethod
    def from_spec(cls, spec: MachineSpec, *, created_at: str) -> "MachineRecord":
        return cls(
            schema_version=SCHEMA_VERSION,
            name=spec.name,
            runner=spec.runner,
            backend_id=spec.name,
            image=spec.image,
            mounts=spec.mounts,
            ports=spec.ports,
            env=spec.env,
            workdir=spec.workdir,
            shell=spec.shell,
            network=spec.network,
            identity=spec.identity,
            resources=spec.resources,
            shell_state=spec.shell_state,
            ssh_agent=spec.ssh_agent,
            gpu=spec.gpu,
            created_at=created_at,
        )

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported machine record schema: {self.schema_version}")
        validate_machine_name(self.name)
        object.__setattr__(self, "runner", _require_text(self.runner, "runner"))
        object.__setattr__(
            self, "backend_id", _require_text(self.backend_id, "backend id")
        )
        object.__setattr__(self, "mounts", tuple(self.mounts))
        object.__setattr__(self, "ports", tuple(self.ports))
        object.__setattr__(self, "env", tuple(self.env))

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "runner": self.runner,
            "backend_id": self.backend_id,
            "image": self.image.to_data(),
            "mounts": [m.to_data() for m in self.mounts],
            "ports": [p.to_data() for p in self.ports],
            "env": list(self.env),
            "workdir": self.workdir,
            "shell": self.shell,
            "network": self.network.to_data(),
            "identity": self.identity.to_data(),
            "resources": self.resources.to_data(),
            "shell_state": self.shell_state.to_data(),
            "ssh_agent": self.ssh_agent,
            "gpu": self.gpu,
            "created_at": self.created_at,
        }

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "MachineRecord":
        return cls(
            schema_version=_int_from_data(data["schema_version"], "schema_version"),
            name=str(data["name"]),
            runner=str(data["runner"]),
            backend_id=str(data["backend_id"]),
            image=ImageSource.from_data(dict(data["image"])),
            mounts=tuple(MountSpec.from_data(dict(x)) for x in data.get("mounts", [])),
            ports=tuple(PortBind.from_data(dict(x)) for x in data.get("ports", [])),
            env=tuple(str(x) for x in data.get("env", [])),
            workdir=data.get("workdir"),
            shell=data.get("shell"),
            network=NetworkSpec.from_data(dict(data.get("network", {}))),
            identity=IdentitySpec.from_data(dict(data.get("identity", {}))),
            resources=ResourceSpec.from_data(dict(data.get("resources", {}))),
            shell_state=ShellStateSpec.from_data(dict(data.get("shell_state", {}))),
            ssh_agent=_bool_from_data(data.get("ssh_agent", False), "ssh_agent"),
            gpu=_bool_from_data(data.get("gpu", False), "gpu"),
            created_at=str(data["created_at"]),
        )


@dataclass(frozen=True)
class ExecSpec:
    command: tuple[str, ...]
    interactive: bool = False
    tty: bool = False
    env: tuple[str, ...] = ()
    workdir: str | None = None
    stream: bool = False

    def __post_init__(self) -> None:
        command = tuple(str(part) for part in self.command)
        if len(command) == 0:
            raise ValueError("command cannot be empty")
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "env", tuple(self.env))


class RuntimeState(Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    MISSING = "missing"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RuntimeStatus:
    name: str
    runner: str
    backend_id: str
    state: RuntimeState
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", _enum(RuntimeState, self.state))


@dataclass(frozen=True)
class RunnerCapabilities:
    image_sources: tuple[ImageSourceKind, ...]
    offline_oci_references: bool
    directory_mounts: bool
    file_mounts: bool
    published_ports: bool
    outbound_network: bool
    restricted_network: bool
    host_network: bool
    ssh_agent: bool
    gpu_vulkan: bool
    root_identity: bool
    host_identity: bool


def validate_machine_name(name: str) -> str:
    text = _require_text(name, "machine name")
    if text in {".", ".."} or "/" in text or "\\" in text:
        raise ValueError(f"invalid machine name: {name}")
    if any(ch.isspace() for ch in text):
        raise ValueError(f"machine name cannot contain whitespace: {name}")
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
