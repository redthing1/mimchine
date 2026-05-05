from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir

from .domain import NetworkMode
from .log import logger


SUPPORTED_BUILDERS = ("podman", "docker")
SUPPORTED_RUNNERS = ("podman", "docker", "smolvm")
SUPPORTED_TOP_LEVEL_TABLES = ("defaults", "profiles")
SUPPORTED_DEFAULT_KEYS = ("builder", "runner", "network", "shell")


@dataclass(frozen=True)
class Defaults:
    builder: str = "podman"
    runner: str = "podman"
    network: NetworkMode = NetworkMode.DEFAULT
    shell: str = "sh"


@dataclass(frozen=True)
class AppConfig:
    defaults: Defaults
    profiles: dict[str, dict[str, Any]]


DEFAULT_CONFIG_TOML = """[defaults]
builder = "podman"
runner = "podman"
network = "default"
shell = "sh"
"""


def get_config_dir() -> Path:
    path = Path(user_config_dir("mimchine"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_config_path() -> Path:
    return get_config_dir() / "config.toml"


def create_default_config(path: Path | None = None) -> None:
    config_path = path or get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    logger.info(f"created default config file at {config_path}")


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or get_config_path()
    if not config_path.exists():
        create_default_config(config_path)
        return AppConfig(defaults=Defaults(), profiles={})

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config must be a TOML table")
    unknown_tables = sorted(set(data) - set(SUPPORTED_TOP_LEVEL_TABLES))
    if unknown_tables:
        expected = ", ".join(f"[{name}]" for name in SUPPORTED_TOP_LEVEL_TABLES)
        actual = ", ".join(f"[{name}]" for name in unknown_tables)
        raise ValueError(f"unknown config table {actual}; supported tables: {expected}")

    defaults = _read_defaults(data.get("defaults", {}))
    profiles = data.get("profiles", {})
    if profiles is None:
        profiles = {}
    if not isinstance(profiles, dict):
        raise ValueError("config field [profiles] must be a table")

    normalized_profiles: dict[str, dict[str, Any]] = {}
    for name, profile in profiles.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("profile names must be non-empty strings")
        if not isinstance(profile, dict):
            raise ValueError(f"profile [{name}] must be a table")
        normalized_profiles[name.strip()] = profile

    return AppConfig(defaults=defaults, profiles=normalized_profiles)


def _read_defaults(data: Any) -> Defaults:
    if data is None:
        return Defaults()
    if not isinstance(data, dict):
        raise ValueError("config field [defaults] must be a table")
    unknown = sorted(set(data) - set(SUPPORTED_DEFAULT_KEYS))
    if unknown:
        raise ValueError(
            f"config field [defaults] has unknown key(s): {', '.join(unknown)}"
        )

    builder = str(data.get("builder", "podman")).strip()
    runner = str(data.get("runner", "podman")).strip()
    network = NetworkMode(str(data.get("network", "default")).strip())
    shell = str(data.get("shell", "sh")).strip() or "sh"

    validate_builder(builder)
    validate_runner(runner)

    return Defaults(builder=builder, runner=runner, network=network, shell=shell)


def validate_builder(name: str) -> str:
    if name not in SUPPORTED_BUILDERS:
        expected = ", ".join(SUPPORTED_BUILDERS)
        raise ValueError(f"unsupported builder [{name}], expected one of: {expected}")
    return name


def validate_runner(name: str) -> str:
    if name not in SUPPORTED_RUNNERS:
        expected = ", ".join(SUPPORTED_RUNNERS)
        raise ValueError(f"unsupported runner [{name}], expected one of: {expected}")
    return name
