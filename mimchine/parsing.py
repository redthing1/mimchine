from __future__ import annotations

from .domain import NetworkMode, PortBind


def parse_port_bind(value: str) -> PortBind:
    parts = value.strip().split(":")
    if len(parts) != 2 or any(part == "" for part in parts):
        raise ValueError(f"invalid port mapping: {value}")
    return PortBind(host=int(parts[0]), guest=int(parts[1]))


def parse_env(value: str) -> str:
    text = value.strip()
    if "=" not in text:
        raise ValueError(f"environment value must be KEY=VALUE: {value}")
    key, _ = text.split("=", 1)
    if not key:
        raise ValueError(f"environment key cannot be empty: {value}")
    return text


def parse_network_mode(value: str) -> NetworkMode:
    return NetworkMode(value.strip())
