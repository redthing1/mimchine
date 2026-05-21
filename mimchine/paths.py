from __future__ import annotations

import os
from pathlib import Path

from .constants import APP_NAME


def config_dir() -> Path:
    return _xdg_app_dir("XDG_CONFIG_HOME", Path(".config"))


def data_dir() -> Path:
    return _xdg_app_dir("XDG_DATA_HOME", Path(".local/share"))


def cache_dir() -> Path:
    return _xdg_app_dir("XDG_CACHE_HOME", Path(".cache"))


def _xdg_app_dir(env_var: str, fallback: Path) -> Path:
    configured = os.environ.get(env_var)
    base = Path(configured).expanduser() if configured else Path.home() / fallback
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path
