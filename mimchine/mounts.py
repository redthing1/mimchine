import os
import posixpath
from dataclasses import dataclass

from .paths import normalize_host_path

DEFAULT_WORKSPACE_BASE = "/work"
MOUNT_MODE_READ_ONLY = "ro"
MOUNT_MODE_READ_WRITE = "rw"
SUPPORTED_MOUNT_MODES = (MOUNT_MODE_READ_ONLY, MOUNT_MODE_READ_WRITE)
SUPPORTED_MOUNT_MODES_STR = ", ".join(SUPPORTED_MOUNT_MODES)


@dataclass(frozen=True)
class MountSpec:
    source_path: str
    container_path: str
    mode: str = MOUNT_MODE_READ_WRITE

    def volume_arg(self) -> str:
        return f"{self.source_path}:{self.container_path}:{self.mode}"


def normalize_mount_mode(mode: str) -> str:
    normalized_mode = mode.strip().lower()
    if normalized_mode not in SUPPORTED_MOUNT_MODES:
        raise ValueError(
            f"invalid mount mode [{mode}], expected one of: {SUPPORTED_MOUNT_MODES_STR}"
        )

    return normalized_mode


def _split_colon_spec(spec: str) -> list[str]:
    stripped_spec = spec.strip()
    if len(stripped_spec) == 0:
        raise ValueError("mount spec cannot be empty")

    parts = stripped_spec.split(":")
    if any(len(part.strip()) == 0 for part in parts):
        raise ValueError(f"invalid mount spec [{spec}], empty fields are not allowed")

    if len(parts) > 3:
        raise ValueError(
            f"invalid mount spec [{spec}], expected host_path:container_path[:ro|rw]"
        )

    return [part.strip() for part in parts]


def _validate_source_path(
    source_path: str,
    *,
    source_kind: str,
    require_directory: bool = False,
) -> str:
    normalized_source_path = normalize_host_path(source_path)
    if not os.path.exists(normalized_source_path):
        raise ValueError(
            f"{source_kind} path [{normalized_source_path}] does not exist"
        )

    if require_directory and not os.path.isdir(normalized_source_path):
        raise ValueError(
            f"{source_kind} path [{normalized_source_path}] is not a directory"
        )

    return normalized_source_path


def _validate_container_path(container_path: str, *, spec: str) -> str:
    stripped_container_path = container_path.strip()
    if len(stripped_container_path) == 0:
        raise ValueError(f"mount spec [{spec}] has empty container path target")

    if not stripped_container_path.startswith("/"):
        raise ValueError(
            f"mount target [{stripped_container_path}] must be an absolute container path"
        )

    return stripped_container_path


def parse_mount_spec(mount_input: str) -> MountSpec:
    parts = _split_colon_spec(mount_input)
    if len(parts) < 2:
        raise ValueError(
            f"invalid mount format [{mount_input}], expected host_path:container_path[:ro|rw]"
        )

    source_path = _validate_source_path(parts[0], source_kind="mount source")
    container_path = _validate_container_path(parts[1], spec=mount_input)
    mode = MOUNT_MODE_READ_WRITE
    if len(parts) == 3:
        mode = normalize_mount_mode(parts[2])

    return MountSpec(
        source_path=source_path,
        container_path=container_path,
        mode=mode,
    )


def _default_workspace_target(source_path: str) -> str:
    workspace_name = os.path.basename(source_path.rstrip(os.sep))
    if len(workspace_name) == 0:
        raise ValueError(f"workspace path [{source_path}] has no usable directory name")

    return posixpath.join(DEFAULT_WORKSPACE_BASE, workspace_name)


def parse_workspace_spec(workspace_input: str) -> MountSpec:
    parts = _split_colon_spec(workspace_input)
    source_path = _validate_source_path(
        parts[0],
        source_kind="workspace",
        require_directory=True,
    )

    container_path = _default_workspace_target(source_path)
    mode = MOUNT_MODE_READ_WRITE

    if len(parts) == 2:
        if parts[1].strip().lower() in SUPPORTED_MOUNT_MODES:
            mode = normalize_mount_mode(parts[1])
        else:
            container_path = _validate_container_path(parts[1], spec=workspace_input)
    elif len(parts) == 3:
        container_path = _validate_container_path(parts[1], spec=workspace_input)
        mode = normalize_mount_mode(parts[2])

    return MountSpec(
        source_path=source_path,
        container_path=container_path,
        mode=mode,
    )
