from __future__ import annotations

from pathlib import Path

import pytest

from mimchine.config import Defaults, load_config
from mimchine.domain import IdentityMode, NetworkMode
from mimchine.profiles import read_profile


def test_load_config_creates_minimal_default(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"

    config = load_config(path)

    assert config.defaults == Defaults()
    assert config.profiles == {}
    assert path.read_text(encoding="utf-8").startswith("[defaults]")


def test_load_config_rejects_unknown_top_level_table(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[unexpected]\nvalue = true\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unknown config table \\[unexpected\\]"):
        load_config(path)


def test_load_config_rejects_unknown_default_key(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[defaults]\nrunnner = 'podman'\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unknown key"):
        load_config(path)


def test_read_profile_normalizes_supported_fields() -> None:
    profile = read_profile(
        "work",
        {
            "image": "fedora:latest",
            "runner": "smolvm",
            "workspace": "./src",
            "mounts": ["./cache:/cache:ro"],
            "ports": ["8080:80"],
            "env": ["MODE=dev"],
            "network": "none",
            "identity": "host",
            "ssh_agent": True,
            "gpu": True,
            "cpus": 2,
        },
    )

    assert profile.image == "fedora:latest"
    assert profile.runner == "smolvm"
    assert profile.workspaces == ("./src",)
    assert profile.mounts == ("./cache:/cache:ro",)
    assert profile.network is NetworkMode.NONE
    assert profile.identity is not None
    assert profile.identity.mode is IdentityMode.HOST
    assert profile.ssh_agent is True
    assert profile.gpu is True
    assert profile.cpus == 2


def test_read_profile_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="unknown field"):
        read_profile("bad", {"image": "alpine", "unexpected": True})


def test_read_profile_rejects_non_bool_gpu() -> None:
    with pytest.raises(ValueError, match="expected boolean value"):
        read_profile("bad", {"image": "alpine", "gpu": "false"})


def test_read_profile_rejects_non_int_resource() -> None:
    with pytest.raises(ValueError, match="expected integer value"):
        read_profile("bad", {"image": "alpine", "cpus": True})
