from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

SCHEMA_VERSION = 3
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


class IsolationMode(Enum):
    NATIVE = "native"
    GVISOR = "gvisor"


@dataclass(frozen=True)
class MountSpec:
    source: Path
    target: str
    read_only: bool = False
    kind: str = "mount"
    options: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Resolve operator input in the mount parser. A stored path is intent;
        # loading it must not reinterpret a symlink against today's filesystem.
        source = Path(self.source)
        if not source.is_absolute():
            raise ValueError("mount source must be absolute")
        object.__setattr__(self, "source", source)
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
        _validate_bool(self.read_only, "read_only")

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
            source=Path(data["source"]),
            target=data["target"],
            read_only=data.get("read_only", False),
            kind=data.get("kind", "mount"),
            options=tuple(data.get("options", [])),
        )


@dataclass(frozen=True)
class PortBind:
    host: int
    guest: int
    address: str | None = None
    protocol: str = "tcp"

    def __post_init__(self) -> None:
        _validate_port(self.host, "host port")
        _validate_port(self.guest, "guest port")
        if self.address is not None:
            if not isinstance(self.address, str):
                raise ValueError("port address must be a string")
            object.__setattr__(self, "address", str(ipaddress.ip_address(self.address)))
        if self.protocol not in {"tcp", "udp"}:
            raise ValueError("port protocol must be tcp or udp")

    def arg(self) -> str:
        prefix = ""
        if self.address is not None:
            address = f"[{self.address}]" if ":" in self.address else self.address
            prefix = f"{address}:"
        suffix = "" if self.protocol == "tcp" else "/udp"
        return f"{prefix}{self.host}:{self.guest}{suffix}"

    @classmethod
    def parse(cls, value: str) -> "PortBind":
        mapping, separator, protocol = value.strip().partition("/")
        protocol = protocol if separator else "tcp"
        address = None
        if mapping.startswith("["):
            address, close, mapping = mapping[1:].partition("]:")
            if not close:
                raise ValueError(f"invalid port mapping: {value}")
        parts = mapping.split(":")
        if len(parts) == 3 and address is None:
            address, *parts = parts
        if len(parts) != 2 or any(part == "" for part in parts):
            raise ValueError(f"invalid port mapping: {value}")
        return cls(int(parts[0]), int(parts[1]), address, protocol)

    def to_data(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "guest": self.guest,
            "address": self.address,
            "protocol": self.protocol,
        }

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "PortBind":
        return cls(
            host=data["host"],
            guest=data["guest"],
            address=data.get("address"),
            protocol=data.get("protocol", "tcp"),
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
            cpus=data.get("cpus"),
            memory_mib=data.get("memory_mib"),
        )


@dataclass(frozen=True)
class MachineSpec:
    name: str
    image: str
    engine: str
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
    job_limits: bool = False
    container_args: tuple[str, ...] = ()
    isolation: IsolationMode = IsolationMode.NATIVE

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", validate_machine_name(self.name))
        object.__setattr__(self, "image", _require_text(self.image, "image"))
        object.__setattr__(self, "engine", _require_text(self.engine, "engine"))
        if self.engine not in {"podman", "docker"}:
            raise ValueError(f"unsupported engine: {self.engine}")
        object.__setattr__(self, "mounts", tuple(self.mounts))
        object.__setattr__(self, "ports", tuple(self.ports))
        object.__setattr__(self, "env", tuple(_parse_env(value) for value in self.env))
        object.__setattr__(self, "network", NetworkMode(self.network))
        object.__setattr__(self, "identity", IdentityMode(self.identity))
        object.__setattr__(self, "isolation", IsolationMode(self.isolation))
        object.__setattr__(
            self, "container_args", tuple(str(x) for x in self.container_args)
        )
        _validate_bool(self.ssh_agent, "ssh_agent")
        _validate_bool(self.gpu, "gpu")
        _validate_bool(self.job_limits, "job_limits")
        _validate_bool(self.shell_state, "shell_state")

    def to_data(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "engine": self.engine,
            "isolation": self.isolation.value,
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
            "job_limits": self.job_limits,
            "container_args": list(self.container_args),
        }

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "MachineSpec":
        return cls(
            name=data["name"],
            image=data["image"],
            engine=data["engine"],
            isolation=data["isolation"],
            mounts=tuple(MountSpec.from_data(dict(x)) for x in data.get("mounts", [])),
            ports=tuple(PortBind.from_data(dict(x)) for x in data.get("ports", [])),
            env=tuple(data.get("env", [])),
            workdir=data.get("workdir"),
            shell=data.get("shell"),
            network=data.get("network", NetworkMode.DEFAULT.value),
            identity=data.get("identity", IdentityMode.IMAGE.value),
            resources=ResourceSpec.from_data(dict(data.get("resources", {}))),
            shell_state=data.get("shell_state", True),
            ssh_agent=data.get("ssh_agent", False),
            gpu=data.get("gpu", False),
            job_limits=data.get("job_limits", False),
            container_args=tuple(str(x) for x in data.get("container_args", [])),
        )


@dataclass(frozen=True)
class ExecSpec:
    command: tuple[str, ...]
    interactive: bool = False
    tty: bool = False
    env: tuple[str, ...] = ()
    workdir: str | None = None
    memory_mib: int | None = None

    def __post_init__(self) -> None:
        command = tuple(str(part) for part in self.command)
        if len(command) == 0:
            raise ValueError("command cannot be empty")
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "env", tuple(_parse_env(value) for value in self.env))
        if self.memory_mib is not None:
            ResourceSpec(memory_mib=self.memory_mib)
            if self.memory_mib > 8796093022207:
                raise ValueError("memory limit is too large")


class RuntimeState(Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    MISSING = "missing"
    UNKNOWN = "unknown"


class Operation(Enum):
    CREATING = "creating"
    READY = "ready"
    DELETING = "deleting"


@dataclass(frozen=True)
class EngineContext:
    engine: str
    identity: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if self.engine not in {"podman", "docker"} or not self.identity:
            raise ValueError("invalid engine context")
        identity = tuple(
            sorted(
                (_require_text(k, "context key"), _require_text(v, "context value"))
                for k, v in self.identity
            )
        )
        if len(dict(identity)) != len(identity):
            raise ValueError("duplicate engine context key")
        object.__setattr__(self, "identity", identity)

    def to_data(self) -> dict[str, Any]:
        return {"engine": self.engine, "identity": dict(self.identity)}

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "EngineContext":
        return cls(data["engine"], tuple(dict(data["identity"]).items()))


@dataclass(frozen=True)
class ContainerAllocation:
    container_id: str
    context: EngineContext
    labels: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.container_id, str)
            or re.fullmatch(r"[a-f0-9]{64}", self.container_id) is None
        ):
            raise ValueError("expected a full immutable container ID")
        if any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in self.labels
        ):
            raise ValueError("ownership labels must contain text")
        labels = tuple(sorted(self.labels))
        if (
            not labels
            or len(dict(labels)) != len(labels)
            or dict(labels).get("mimchine") != "1"
        ):
            raise ValueError("allocation requires unambiguous ownership labels")
        object.__setattr__(self, "labels", labels)

    def to_data(self) -> dict[str, Any]:
        return {
            "container_id": self.container_id,
            "context": self.context.to_data(),
            "labels": dict(self.labels),
        }

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "ContainerAllocation":
        return cls(
            data["container_id"],
            EngineContext.from_data(data["context"]),
            tuple(dict(data["labels"]).items()),
        )


@dataclass(frozen=True)
class MachineBinding:
    allocation: ContainerAllocation
    image_id: str
    runtime: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "image_id", _require_text(self.image_id, "image ID"))
        object.__setattr__(self, "runtime", _require_text(self.runtime, "OCI runtime"))

    def to_data(self) -> dict[str, Any]:
        return {
            "allocation": self.allocation.to_data(),
            "image_id": self.image_id,
            "runtime": self.runtime,
        }

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "MachineBinding":
        return cls(
            ContainerAllocation.from_data(data["allocation"]),
            data["image_id"],
            data["runtime"],
        )


@dataclass(frozen=True)
class MachineObservation:
    container_id: str
    context: EngineContext
    image_id: str
    runtime: str
    labels: tuple[tuple[str, str], ...]
    state: RuntimeState
    configuration: str = "{}"

    def verify(
        self, target: ContainerAllocation, binding: MachineBinding | None = None
    ) -> None:
        if self.context != target.context:
            raise ValueError("engine context does not match the machine binding")
        if self.container_id != target.container_id:
            raise ValueError("container ID does not match the machine binding")
        if any(dict(self.labels).get(key) != value for key, value in target.labels):
            raise ValueError("container ownership does not match the machine binding")
        if binding is not None:
            if self.image_id != binding.image_id:
                raise ValueError("container image does not match the machine binding")
            if self.runtime != binding.runtime:
                raise ValueError("OCI runtime does not match the machine binding")


@dataclass(frozen=True)
class MissingContainer:
    # Absence is meaningful only in the engine context that was queried.
    context: EngineContext


def verify_observation(
    observed: MachineObservation | MissingContainer,
    target: ContainerAllocation,
    binding: MachineBinding | None = None,
) -> MachineObservation | None:
    if observed.context != target.context:
        raise ValueError("engine context does not match the machine binding")
    if isinstance(observed, MissingContainer):
        return None
    observed.verify(target, binding)
    return observed


@dataclass(frozen=True)
class Machine:
    id: str
    spec: MachineSpec
    created_at: str
    operation: Operation = Operation.CREATING
    binding: MachineBinding | None = None
    allocation: ContainerAllocation | None = None
    error: str | None = None
    creation_context: EngineContext | None = None

    def __post_init__(self) -> None:
        _require_text(self.id, "machine ID")
        if str(UUID(self.id)) != self.id:
            raise ValueError("machine ID must be a canonical UUID")
        object.__setattr__(
            self, "created_at", _require_text(self.created_at, "created at")
        )
        object.__setattr__(self, "operation", Operation(self.operation))
        if self.binding is not None and self.allocation is not None:
            raise ValueError(
                "machine cannot have both a pending allocation and a binding"
            )
        if self.operation is Operation.READY and self.binding is None:
            raise ValueError("ready machine requires a binding")
        if self.operation is Operation.CREATING and self.binding is not None:
            raise ValueError("creating machine cannot have a completed binding")
        if self.binding is not None and self.creation_context is not None:
            raise ValueError("completed machine cannot retain a creation context")
        if self.target is None and self.creation_context is None:
            raise ValueError("unallocated machine requires a pinned creation context")
        if (
            self.creation_context is not None
            and self.creation_context.engine != self.spec.engine
        ):
            raise ValueError("creation context engine does not match machine intent")
        if self.target is not None and self.target.context.engine != self.spec.engine:
            raise ValueError("binding engine does not match machine intent")
        if self.allocation is not None:
            if self.creation_context != self.allocation.context:
                raise ValueError(
                    "allocation context does not match the creation transaction"
                )
            if dict(self.allocation.labels).get("mimchine.id") != self.id:
                raise ValueError(
                    "allocation ownership does not match the creation transaction"
                )

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def target(self) -> ContainerAllocation | None:
        return self.binding.allocation if self.binding else self.allocation

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "spec": self.spec.to_data(),
            "created_at": self.created_at,
            "operation": self.operation.value,
            "binding": None if self.binding is None else self.binding.to_data(),
            "allocation": None
            if self.allocation is None
            else self.allocation.to_data(),
            "error": self.error,
            "creation_context": None
            if self.creation_context is None
            else self.creation_context.to_data(),
        }

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "Machine":
        version = data["schema_version"]
        if type(version) is not int or version != SCHEMA_VERSION:
            raise ValueError(f"unsupported machine record schema: {version!r}")
        return cls(
            id=data["id"],
            spec=MachineSpec.from_data(data["spec"]),
            created_at=data["created_at"],
            operation=data["operation"],
            binding=None
            if data["binding"] is None
            else MachineBinding.from_data(data["binding"]),
            allocation=None
            if data["allocation"] is None
            else ContainerAllocation.from_data(data["allocation"]),
            error=data.get("error"),
            creation_context=None
            if data.get("creation_context") is None
            else EngineContext.from_data(data["creation_context"]),
        )


def validate_machine_name(name: str) -> str:
    text = _require_text(name, "machine name")
    if MACHINE_NAME_PATTERN.fullmatch(text) is None:
        raise ValueError(
            "machine names must use lowercase letters, numbers, or dots, with "
            "hyphens and underscores only inside labels"
        )
    return text


def _parse_env(value: str) -> str:
    if not isinstance(value, str) or "\0" in value:
        raise ValueError("environment value must be a string without NUL bytes")
    if "=" not in value:
        raise ValueError(f"environment value must be KEY=VALUE: {value}")
    key, _ = value.split("=", 1)
    if not key:
        raise ValueError(f"environment key cannot be empty: {value}")
    return value


def _require_text(value: str, label: str) -> str:
    if not isinstance(value, str) or "\0" in value:
        raise ValueError(f"{label} must be a string without NUL bytes")
    text = value.strip()
    if len(text) == 0:
        raise ValueError(f"{label} cannot be empty")
    return text


def _normalize_path(path: str | Path) -> Path:
    return Path(os.path.expanduser(str(path))).resolve()


def _validate_port(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if value < 1 or value > 65535:
        raise ValueError(f"{label} must be between 1 and 65535")


def _validate_bool(value: bool, label: str) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
