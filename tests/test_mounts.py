from __future__ import annotations

import os
from pathlib import Path

import pytest

from mimchine.mounts import (
    map_host_path_to_guest,
    parse_home_share_spec,
    parse_mount_spec,
    parse_workspace_spec,
)


def test_workspace_defaults_to_work_dir(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    mount = parse_workspace_spec(str(project))

    assert mount.source == project.resolve()
    assert mount.target == "/work/project"
    assert mount.kind == "workspace"


def test_workspace_accepts_default_target_with_mount_options(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    mount = parse_workspace_spec(f"{project}:rw,z")

    assert mount.target == "/work/project"
    assert mount.read_only is False
    assert mount.options == ("z",)
    assert mount.mode == "rw,z"


def test_mount_requires_absolute_guest_target(tmp_path: Path) -> None:
    path = tmp_path / "config"
    path.write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="absolute"):
        parse_mount_spec(f"{path}:relative")


def test_mount_rejects_special_files(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)

    with pytest.raises(ValueError, match="directory or regular file"):
        parse_mount_spec(f"{fifo}:/pipe")


def test_mount_accepts_relabel_option(tmp_path: Path) -> None:
    path = tmp_path / "config"
    path.write_text("x", encoding="utf-8")

    mount = parse_mount_spec(f"{path}:/config:ro,z")

    assert mount.read_only is True
    assert mount.options == ("z",)
    assert mount.mode == "ro,z"
    assert mount.volume_arg().endswith(":/config:ro,z")


def test_mount_accepts_generic_option_after_access_mode(tmp_path: Path) -> None:
    path = tmp_path / "config"
    path.write_text("x", encoding="utf-8")

    mount = parse_mount_spec(f"{path}:/config:rw,idmap")

    assert mount.read_only is False
    assert mount.options == ("idmap",)
    assert mount.mode == "rw,idmap"


def test_mount_rejects_unknown_option_without_access_mode(tmp_path: Path) -> None:
    path = tmp_path / "config"
    path.write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="mount mode"):
        parse_mount_spec(f"{path}:/config:r0")


def test_mount_rejects_access_mode_as_option(tmp_path: Path) -> None:
    path = tmp_path / "config"
    path.write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="access mode"):
        parse_mount_spec(f"{path}:/config:ro,rw")


def test_home_share_maps_host_home_relative_path_twice(tmp_path: Path) -> None:
    host_home = tmp_path / "home" / "fed"
    source = host_home / "Dev"
    source.mkdir(parents=True)

    mounts = parse_home_share_spec(str(source), host_home=host_home)

    assert [(mount.source, mount.target, mount.kind) for mount in mounts] == [
        (source.resolve(), str(source.resolve()), "home_share"),
        (source.resolve(), "/home/user/Dev", "home_share"),
    ]


def test_home_share_accepts_read_only_mode(tmp_path: Path) -> None:
    host_home = tmp_path / "home" / "fed"
    source = host_home / "Downloads" / "Work"
    source.mkdir(parents=True)

    mounts = parse_home_share_spec(f"{source}:ro", host_home=host_home)

    assert all(mount.read_only for mount in mounts)
    assert mounts[1].target == "/home/user/Downloads/Work"


def test_home_share_preserves_mount_options(tmp_path: Path) -> None:
    host_home = tmp_path / "home" / "fed"
    source = host_home / "Dev"
    source.mkdir(parents=True)

    mounts = parse_home_share_spec(f"{source}:ro,Z", host_home=host_home)

    assert all(mount.read_only for mount in mounts)
    assert all(mount.options == ("Z",) for mount in mounts)
    assert all(mount.mode == "ro,Z" for mount in mounts)


def test_home_share_rejects_paths_outside_host_home(tmp_path: Path) -> None:
    host_home = tmp_path / "home" / "fed"
    outside = tmp_path / "srv" / "data"
    outside.mkdir(parents=True)

    with pytest.raises(ValueError, match="under host home"):
        parse_home_share_spec(str(outside), host_home=host_home)


def test_map_host_path_to_guest_uses_most_specific_mount(tmp_path: Path) -> None:
    root = tmp_path / "project"
    nested = root / "src"
    nested.mkdir(parents=True)
    file_path = nested / "main.py"
    file_path.write_text("print(1)", encoding="utf-8")

    mounts = (
        parse_workspace_spec(f"{root}:/work/project"),
        parse_workspace_spec(f"{nested}:/src"),
    )

    assert map_host_path_to_guest(file_path, mounts) == "/src/main.py"
