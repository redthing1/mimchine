import os
import posixpath
from typing import List
from dataclasses import dataclass

@dataclass
class ContainerIntegrationMount:
    source_path: str
    container_path: str
    is_file: bool


CONTAINER_INTEGRATION_MOUNTS = [
    # ContainerIntegrationMount("$/.zsh_history", "$/.zsh_history", True),
    # ContainerIntegrationMount("$/.zsh_history.new", "$/.zsh_history.new", True),
    # ContainerIntegrationMount("$/.bash_history", "$/.bash_history", True),
]

CONTAINER_HOME_DIR = "/root"
CONTAINER_HOST_HOME_BASE = "/mim/home"


def get_container_integration_mounts(data_dir) -> List[str]:
    return [
        ContainerIntegrationMount(
            x.source_path.replace("$", data_dir),
            x.container_path.replace("$", CONTAINER_HOME_DIR),
            x.is_file,
        )
        for x in CONTAINER_INTEGRATION_MOUNTS
    ]


def get_home_integration_mount() -> str:
    host_home = os.path.realpath(os.path.abspath(os.path.expanduser(get_home_dir())))
    home_name = os.path.basename(host_home.rstrip(os.sep))
    container_home = posixpath.join(CONTAINER_HOST_HOME_BASE, home_name)
    return f"{host_home}:{container_home}"


def get_home_integration_env() -> str:
    host_home = os.path.realpath(os.path.abspath(os.path.expanduser(get_home_dir())))
    home_name = os.path.basename(host_home.rstrip(os.sep))
    container_home = posixpath.join(CONTAINER_HOST_HOME_BASE, home_name)
    return f"HOST_HOME={container_home}"


def map_host_path_to_container(host_path: str, mounts) -> str | None:
    host_path = os.path.realpath(os.path.abspath(os.path.expanduser(host_path)))

    best_source = None
    best_destination = None
    for mount in mounts:
        source = mount.get("source")
        destination = mount.get("destination")
        if not source or not destination:
            continue

        source = os.path.realpath(os.path.abspath(source))
        try:
            common_path = os.path.commonpath([host_path, source])
        except ValueError:
            continue

        if common_path != source:
            continue

        if best_source is None or len(source) > len(best_source):
            best_source = source
            best_destination = destination

    if best_source is None or best_destination is None:
        return None

    rel_path = os.path.relpath(host_path, best_source)
    if rel_path == ".":
        return best_destination

    return posixpath.join(best_destination, rel_path.replace("\\", "/"))


def get_app_data_dir(app_name: str) -> str:
    import platform

    system = platform.system().lower()

    if system == "darwin":
        return os.path.join(
            os.environ["HOME"], f"Library/Application Support/{app_name}"
        )
    elif system == "linux":
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if xdg_data:
            return os.path.join(xdg_data, app_name)
        else:
            return os.path.join(os.environ["HOME"], f".local/share/{app_name}")
    elif system == "windows":
        return os.path.join(os.environ["APPDATA"], app_name)
    else:
        raise NotImplementedError(f"unknown system: {system}")


def get_home_dir() -> str:
    if os.name == "posix":
        if os.uname().sysname == "Darwin":
            return os.environ["HOME"]
        elif os.uname().sysname == "Linux":
            return os.environ["HOME"]
        else:
            raise NotImplementedError(f"unknown posix system: {os.uname().sysname}")
    elif os.name == "nt":
        return os.environ["USERPROFILE"]
    else:
        raise NotImplementedError(f"unknown os: {os.name}")
