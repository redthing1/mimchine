from __future__ import annotations

from pathlib import Path

import pytest

from mimchine.paths import cache_dir, config_dir, data_dir


def test_xdg_app_dirs_use_env_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))

    paths = {
        "config": config_dir(),
        "data": data_dir(),
        "cache": cache_dir(),
    }

    assert paths == {
        "config": tmp_path / "xdg-config" / "mimchine",
        "data": tmp_path / "xdg-data" / "mimchine",
        "cache": tmp_path / "xdg-cache" / "mimchine",
    }
    assert all(path.exists() for path in paths.values())


def test_xdg_app_dirs_fall_back_to_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    paths = {
        "config": config_dir(),
        "data": data_dir(),
        "cache": cache_dir(),
    }

    assert paths == {
        "config": tmp_path / ".config" / "mimchine",
        "data": tmp_path / ".local" / "share" / "mimchine",
        "cache": tmp_path / ".cache" / "mimchine",
    }
    assert all(path.exists() for path in paths.values())
