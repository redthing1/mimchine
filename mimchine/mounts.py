from __future__ import annotations

import os
import posixpath
from pathlib import Path

from .domain import MountSpec


DEFAULT_WORKSPACE_BASE = "/work"
DEFAULT_GUEST_HOME = "/home/user"
_NO_TARGET_OPTION_PREFIXES = {"ro", "rw", "z", "Z", "U", "O"}


def parse_mount_spec(value: str) -> MountSpec:
    source, target, read_only, options = _parse_parts(value, require_target=True)
    return MountSpec(
        source=source,
        target=target,
        read_only=read_only,
        kind="mount",
        options=options,
    )


def parse_workspace_spec(value: str) -> MountSpec:
    source, target, read_only, options = _parse_parts(value, require_target=False)
    if not source.is_dir():
        raise ValueError(f"workspace path is not a directory: {source}")
    if target is None:
        target = _default_workspace_target(source)
    return MountSpec(
        source=source,
        target=target,
        read_only=read_only,
        kind="workspace",
        options=options,
    )


def parse_home_share_spec(
    value: str,
    *,
    host_home: Path | None = None,
    guest_home: str = DEFAULT_GUEST_HOME,
) -> tuple[MountSpec, ...]:
    source, read_only, options = _parse_home_share_parts(value)
    if not source.is_dir():
        raise ValueError(f"home-share path is not a directory: {source}")

    resolved_host_home = (host_home or Path.home()).resolve()
    try:
        common = os.path.commonpath([source, resolved_host_home])
    except ValueError as exc:
        raise ValueError(f"home-share path is not under host home: {source}") from exc
    if Path(common) != resolved_host_home:
        raise ValueError(f"home-share path is not under host home: {source}")

    guest_home = _validate_target(guest_home)
    rel = os.path.relpath(source, resolved_host_home)
    guest_target = (
        guest_home
        if rel == "."
        else posixpath.join(guest_home, rel.replace(os.sep, "/"))
    )
    mounts = [
        MountSpec(
            source=source,
            target=str(source),
            read_only=read_only,
            kind="home_share",
            options=options,
        )
    ]
    if guest_target != str(source):
        mounts.append(
            MountSpec(
                source=source,
                target=guest_target,
                read_only=read_only,
                kind="home_share",
                options=options,
            )
        )
    return tuple(mounts)


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


def _parse_parts(
    value: str, *, require_target: bool
) -> tuple[Path, str | None, bool, tuple[str, ...]]:
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
    read_only = False
    options: tuple[str, ...] = ()

    if len(parts) == 2:
        if _looks_like_mode_options(parts[1]) and not require_target:
            read_only, options = _validate_mode_options(parts[1])
        else:
            target = _validate_target(parts[1])
    elif len(parts) == 3:
        target = _validate_target(parts[1])
        read_only, options = _validate_mode_options(parts[2])

    if require_target and target is None:
        raise ValueError(f"mount target is required: {value}")

    return source, target, read_only, options


def _parse_home_share_parts(value: str) -> tuple[Path, bool, tuple[str, ...]]:
    parts = [part.strip() for part in value.strip().split(":")]
    if len(parts) == 0 or any(part == "" for part in parts):
        raise ValueError(f"invalid home-share spec: {value}")
    if len(parts) > 2:
        raise ValueError(f"invalid home-share spec: {value}")

    source = Path(os.path.expanduser(parts[0])).resolve()
    if not source.exists():
        raise ValueError(f"home-share source does not exist: {source}")
    read_only = False
    options: tuple[str, ...] = ()
    if len(parts) == 2:
        if not _looks_like_mode_options(parts[1]):
            raise ValueError(
                f"mount mode must be ro or rw, optionally followed by options: {parts[1]}"
            )
        read_only, options = _validate_mode_options(parts[1])
    return source, read_only, options


def _validate_target(value: str) -> str:
    if not value.startswith("/"):
        raise ValueError(f"mount target must be absolute: {value}")
    return value


def _looks_like_mode_options(value: str) -> bool:
    first = value.split(",", 1)[0]
    return first in _NO_TARGET_OPTION_PREFIXES


def _validate_mode_options(value: str) -> tuple[bool, tuple[str, ...]]:
    parts = [part.strip() for part in value.split(",")]
    if any(part == "" for part in parts):
        raise ValueError(f"invalid mount option list: {value}")

    read_only = False
    options = parts
    if parts[0] in {"ro", "rw"}:
        read_only = parts[0] == "ro"
        options = parts[1:]
    elif not _looks_like_mode_options(value):
        raise ValueError(
            f"mount mode must be ro or rw, optionally followed by options: {value}"
        )

    for option in options:
        if option in {"ro", "rw"}:
            raise ValueError(f"mount access mode must appear first: {value}")
        if ":" in option or "," in option:
            raise ValueError(f"invalid mount option: {option}")
    return read_only, tuple(options)


def _default_workspace_target(source: Path) -> str:
    name = source.name
    if not name:
        raise ValueError(f"workspace path has no usable name: {source}")
    return posixpath.join(DEFAULT_WORKSPACE_BASE, name)
