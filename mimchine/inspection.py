from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContainerInspection:
    basics: list[tuple[str, str]]
    mounts: list[tuple[str, str, str]]
    ports: list[tuple[str, str]]
    devices: list[tuple[str, str, str]]
    env_keys: list[tuple[str]]


def _container_name(inspect_data: dict[str, Any], fallback: str) -> str:
    name = inspect_data.get("Name")
    if isinstance(name, str) and name.strip():
        return name.strip().lstrip("/")

    names = inspect_data.get("Names")
    if isinstance(names, list):
        for item in names:
            if isinstance(item, str) and item.strip():
                return item.strip().lstrip("/")

    return fallback


def _container_state(inspect_data: dict[str, Any]) -> str:
    state = inspect_data.get("State", {})
    if not isinstance(state, dict):
        return "unknown"

    status = state.get("Status")
    if isinstance(status, str) and status.strip():
        return status.strip()

    running = state.get("Running")
    if isinstance(running, bool):
        return "running" if running else "stopped"

    return "unknown"


def _bool_field(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"

    return str(value) if value is not None else ""


def _mount_mode(mount: dict[str, Any]) -> str:
    rw = mount.get("RW")
    if isinstance(rw, bool):
        return "rw" if rw else "ro"

    mode = mount.get("Mode") or mount.get("mode")
    if isinstance(mode, str) and mode.strip():
        return mode.strip()

    options = mount.get("Options") or mount.get("options")
    if isinstance(options, list) and len(options) > 0:
        return ",".join(str(option) for option in options)

    return ""


def _mount_rows(inspect_data: dict[str, Any]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    mounts = inspect_data.get("Mounts", [])
    if not isinstance(mounts, list):
        return rows

    for mount in mounts:
        if not isinstance(mount, dict):
            continue

        source = mount.get("Source") or mount.get("source")
        destination = mount.get("Destination") or mount.get("destination")
        if source and destination:
            rows.append((str(source), str(destination), _mount_mode(mount)))

    return rows


def _port_rows(inspect_data: dict[str, Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    network_settings = inspect_data.get("NetworkSettings", {})
    if not isinstance(network_settings, dict):
        return rows

    ports = network_settings.get("Ports", {})
    if not isinstance(ports, dict):
        return rows

    for container_port, bindings in sorted(ports.items()):
        if bindings is None:
            rows.append((str(container_port), ""))
            continue

        if not isinstance(bindings, list):
            rows.append((str(container_port), str(bindings)))
            continue

        for binding in bindings:
            if not isinstance(binding, dict):
                rows.append((str(container_port), str(binding)))
                continue

            host_ip = str(binding.get("HostIp", ""))
            host_port = str(binding.get("HostPort", ""))
            host_binding = host_port if len(host_ip) == 0 else f"{host_ip}:{host_port}"
            rows.append((str(container_port), host_binding))

    return rows


def _device_rows(inspect_data: dict[str, Any]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    host_config = inspect_data.get("HostConfig", {})
    if not isinstance(host_config, dict):
        return rows

    devices = host_config.get("Devices", [])
    if not isinstance(devices, list):
        return rows

    for device in devices:
        if isinstance(device, dict):
            rows.append(
                (
                    str(device.get("PathOnHost", "")),
                    str(device.get("PathInContainer", "")),
                    str(device.get("CgroupPermissions", "")),
                )
            )
        else:
            rows.append((str(device), "", ""))

    return rows


def _env_key_rows(inspect_data: dict[str, Any]) -> list[tuple[str]]:
    config = inspect_data.get("Config", {})
    if not isinstance(config, dict):
        return []

    env = config.get("Env", [])
    if not isinstance(env, list):
        return []

    keys = []
    for item in env:
        if isinstance(item, str) and "=" in item:
            keys.append(item.split("=", 1)[0])

    return [(key,) for key in sorted(set(keys))]


def build_container_inspection(
    container_name: str,
    runtime: str,
    data_dir: str,
    inspect_data: dict[str, Any],
) -> ContainerInspection:
    config = inspect_data.get("Config", {})
    host_config = inspect_data.get("HostConfig", {})
    if not isinstance(config, dict):
        config = {}
    if not isinstance(host_config, dict):
        host_config = {}

    basics = [
        ("name", _container_name(inspect_data, container_name)),
        ("image", str(config.get("Image", ""))),
        ("state", _container_state(inspect_data)),
        ("runtime", runtime),
        ("data dir", data_dir),
        ("network", str(host_config.get("NetworkMode", ""))),
        ("pid mode", str(host_config.get("PidMode", ""))),
        ("privileged", _bool_field(host_config.get("Privileged"))),
    ]

    return ContainerInspection(
        basics=basics,
        mounts=_mount_rows(inspect_data),
        ports=_port_rows(inspect_data),
        devices=_device_rows(inspect_data),
        env_keys=_env_key_rows(inspect_data),
    )
