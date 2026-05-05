from __future__ import annotations

import os
import posixpath
from pathlib import Path

from .domain import MountSpec


DEFAULT_WORKSPACE_BASE = "/work"


def parse_mount_spec(value: str) -> MountSpec:
    source, target, mode = _parse_parts(value, require_target=True)
    return MountSpec(source=source, target=target, read_only=mode == "ro", kind="mount")


def parse_workspace_spec(value: str) -> MountSpec:
    source, target, mode = _parse_parts(value, require_target=False)
    if not source.is_dir():
        raise ValueError(f"workspace path is not a directory: {source}")
    if target is None:
        target = _default_workspace_target(source)
    return MountSpec(
        source=source,
        target=target,
        read_only=mode == "ro",
        kind="workspace",
    )


def map_host_path_to_guest(host_path: Path, mounts: tuple[MountSpec, ...]) -> str | None:
    resolved_host_path = Path(os.path.expanduser(str(host_path))).resolve()
    best: MountSpec | None = None

    for mount in mounts:
        try:
            common = os.path.commonpath([resolved_host_path, mount.source])
        except ValueError:
            continue
        if Path(common) != mount.source:
            continue
        if best is None or len(str(mount.source)) > len(str(best.source)):
            best = mount

    if best is None:
        return None

    rel = os.path.relpath(resolved_host_path, best.source)
    if rel == ".":
        return best.target
    return posixpath.join(best.target, rel.replace(os.sep, "/"))


def _parse_parts(value: str, *, require_target: bool) -> tuple[Path, str | None, str]:
    parts = [part.strip() for part in value.strip().split(":")]
    if len(parts) == 0 or any(part == "" for part in parts):
        raise ValueError(f"invalid mount spec: {value}")
    if len(parts) > 3:
        raise ValueError(f"invalid mount spec: {value}")

    source = Path(os.path.expanduser(parts[0])).resolve()
    if not source.exists():
        raise ValueError(f"mount source does not exist: {source}")
    if not source.is_dir() and not source.is_file():
        raise ValueError(f"mount source must be a directory or regular file: {source}")

    target: str | None = None
    mode = "rw"

    if len(parts) == 2:
        if parts[1] in {"ro", "rw"} and not require_target:
            mode = parts[1]
        else:
            target = _validate_target(parts[1])
    elif len(parts) == 3:
        target = _validate_target(parts[1])
        mode = _validate_mode(parts[2])

    if require_target and target is None:
        raise ValueError(f"mount target is required: {value}")

    return source, target, mode


def _validate_target(value: str) -> str:
    if not value.startswith("/"):
        raise ValueError(f"mount target must be absolute: {value}")
    return value


def _validate_mode(value: str) -> str:
    if value not in {"ro", "rw"}:
        raise ValueError(f"mount mode must be ro or rw: {value}")
    return value


def _default_workspace_target(source: Path) -> str:
    name = source.name
    if not name:
        raise ValueError(f"workspace path has no usable name: {source}")
    return posixpath.join(DEFAULT_WORKSPACE_BASE, name)
