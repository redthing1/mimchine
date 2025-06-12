import os
import sys
from pathlib import Path
from typing import Dict, Any, Optional

from platformdirs import user_config_dir
from minlog import logger

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

import tomli_w


DEFAULT_CONFIG = {
    "container": {
        "runtime": "podman",
    }
}


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
        logger.info(f"Config file not found at {config_path}")
        create_default_config()
        return DEFAULT_CONFIG.copy()
    
    try:
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
        logger.debug(f"Loaded config from {config_path}")
        return config
    except Exception as e:
        logger.warning(f"Failed to load config from {config_path}: {e}")
        logger.info("Using default configuration")
        return DEFAULT_CONFIG.copy()


def create_default_config() -> None:
    """Create the default configuration file."""
    config_path = get_config_path()
    config_dir = config_path.parent
    
    # Ensure config directory exists
    config_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        with open(config_path, "wb") as f:
            tomli_w.dump(DEFAULT_CONFIG, f)
        logger.info(f"Created default config file at {config_path}")
        logger.info("You can edit this file to customize mimchine's behavior")
    except Exception as e:
        logger.error(f"Failed to create config file at {config_path}: {e}")


def get_container_runtime() -> str:
    """Get the configured container runtime (podman or docker)."""
    config = load_config()
    return config.get("container", {}).get("runtime", "podman")


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
            runtime = container_config["runtime"]
            if runtime not in ["podman", "docker"]:
                logger.error(f"Invalid container runtime: {runtime}. Must be 'podman' or 'docker'")
                return False
    
    return True