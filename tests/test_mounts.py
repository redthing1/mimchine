from __future__ import annotations

import os
from pathlib import Path

import pytest

from mimchine.mounts import map_host_path_to_guest, parse_mount_spec, parse_workspace_spec


def test_workspace_defaults_to_work_dir(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    mount = parse_workspace_spec(str(project))

    assert mount.source == project.resolve()
    assert mount.target == "/work/project"
    assert mount.kind == "workspace"


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
