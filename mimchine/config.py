import os
from pathlib import Path
from typing import Dict, Any

from platformdirs import user_config_dir
from .log import logger

import tomllib


DEFAULT_CONFIG = {
    "container": {
        "runtime": "podman",
    }
}
DEFAULT_CONFIG_TOML = '[container]\nruntime = "podman"\n'
SUPPORTED_CONTAINER_RUNTIMES = ("podman", "docker")
SUPPORTED_CONTAINER_RUNTIMES_STR = ", ".join(SUPPORTED_CONTAINER_RUNTIMES)

_container_runtime_override: str | None = None


def _normalize_runtime(runtime: str) -> str:
    return runtime.strip().lower()


def _normalize_and_validate_runtime(runtime: str) -> str:
    normalized_runtime = _normalize_runtime(runtime)
    if normalized_runtime not in SUPPORTED_CONTAINER_RUNTIMES:
        raise ValueError(
            f"invalid container runtime: {runtime}. must be one of: {SUPPORTED_CONTAINER_RUNTIMES_STR}"
        )

    return normalized_runtime


def set_container_runtime_override(runtime: str | None) -> None:
    global _container_runtime_override

    if runtime is None:
        _container_runtime_override = None
        return

    _container_runtime_override = _normalize_and_validate_runtime(runtime)


def get_config_dir() -> Path:
    """Get the platform-appropriate config directory for mimchine."""
    import platform

    system = platform.system().lower()

    if system == "darwin":
        config_path = Path.home() / ".config" / "mimchine"
    elif system == "linux":
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        if xdg_config:
            config_path = Path(xdg_config) / "mimchine"
        else:
            config_path = Path.home() / ".config" / "mimchine"
    else:
        config_path = Path(user_config_dir("mimchine"))

    config_path.mkdir(parents=True, exist_ok=True)
    return config_path


def get_config_path() -> Path:
    """Get the full path to the config file."""
    return get_config_dir() / "config.toml"


def load_config() -> Dict[str, Any]:
    """Load configuration from TOML file, creating default if it doesn't exist."""
    config_path = get_config_path()

    if not config_path.exists():
        logger.info(f"config file not found at {config_path}")
        create_default_config()
        return DEFAULT_CONFIG.copy()

    try:
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
        logger.debug(f"loaded config from {config_path}")
        return config
    except Exception as e:
        logger.warn(f"failed to load config from {config_path}: {e}")
        logger.info("using default configuration")
        return DEFAULT_CONFIG.copy()


def create_default_config() -> None:
    """Create the default configuration file."""
    config_path = get_config_path()
    config_dir = config_path.parent

    # Ensure config directory exists
    config_dir.mkdir(parents=True, exist_ok=True)

    try:
        config_path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
        logger.info(f"created default config file at {config_path}")
        logger.info("you can edit this file to customize mimchine's behavior")
    except Exception as e:
        logger.error(f"failed to create config file at {config_path}: {e}")


def get_container_runtime() -> str:
    """Get the configured container runtime (podman or docker)."""
    if _container_runtime_override is not None:
        return _container_runtime_override

    config = load_config()
    runtime = str(config.get("container", {}).get("runtime", "podman"))
    try:
        return _normalize_and_validate_runtime(runtime)
    except ValueError:
        normalized_runtime = _normalize_runtime(runtime)
        logger.error(
            f"invalid container runtime in config: {normalized_runtime}. falling back to podman"
        )
        return "podman"


def validate_config(config: Dict[str, Any]) -> bool:
    """Validate configuration structure and values."""
    if not isinstance(config, dict):
        return False

    # Check container runtime if specified
    if "container" in config:
        container_config = config["container"]
        if not isinstance(container_config, dict):
            return False

        if "runtime" in container_config:
            runtime = str(container_config["runtime"])
            try:
                _normalize_and_validate_runtime(runtime)
            except ValueError:
                logger.error(
                    f"invalid container runtime: {runtime}. must be one of: {SUPPORTED_CONTAINER_RUNTIMES_STR}"
                )
                return False

    return True
