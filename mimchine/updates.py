"""Pure patches and readable, redacted update plans."""

from __future__ import annotations

import ipaddress
import posixpath
from dataclasses import dataclass, replace

from .domain import MachineSpec, PortBind, ResourceSpec, UpdateOptions
from .mounts import parse_mount_spec, parse_workspace_spec
from .shells import normalize_shell


def mount_target(value: str) -> str:
    if not value.startswith("/") or ".." in value.split("/"):
        raise ValueError("mount destination must be an absolute path without '..'")
    return "/" + posixpath.normpath(value).lstrip("/")


def port_key(port: PortBind) -> tuple[str | None, int, str]:
    return port.address, port.host, port.protocol


def explicit_port(value: str) -> PortBind:
    port = PortBind.parse(value)
    return replace(port, address=port.address or "127.0.0.1")


def validate_mounts_ports(spec: MachineSpec) -> None:
    targets = [mount_target(m.target) for m in spec.mounts]
    if len(targets) != len(set(targets)):
        raise ValueError("mount destinations must be unique")
    for mount in spec.mounts:
        target = mount_target(mount.target)
        if mount.kind != "shell_state" and (
            target == "/mim" or target.startswith("/mim/") or target == "/"
        ):
            raise ValueError("mount destination overlaps Mim's managed paths")
    for index, port in enumerate(spec.ports):
        for other in spec.ports[:index]:
            if (port.host, port.protocol) != (other.host, other.protocol):
                continue
            a, b = port.address, other.address
            if (
                a is None
                or b is None
                or a == b
                or (
                    ipaddress.ip_address(a).version == ipaddress.ip_address(b).version
                    and (
                        ipaddress.ip_address(a).is_unspecified
                        or ipaddress.ip_address(b).is_unspecified
                    )
                )
            ):
                raise ValueError("published host listeners overlap")


@dataclass(frozen=True)
class UpdatePlan:
    before: MachineSpec
    after: MachineSpec
    effect: str
    changes: dict

    def to_data(self) -> dict:
        return dict(
            machine=self.before.name,
            effect=self.effect,
            changes=self.changes,
            interruption="requires --restart if running"
            if self.effect == "replace"
            else "no restart",
            execution_enabled=False,
        )


def plan_update(before: MachineSpec, options: UpdateOptions) -> UpdatePlan:
    mounts = {mount_target(m.target): m for m in before.mounts}
    if len(mounts) != len(before.mounts):
        raise ValueError("existing mount destinations are ambiguous")
    additions = [parse_mount_spec(v) for v in options.mounts]
    additions += [parse_workspace_spec(v) for v in options.workspaces]
    removals = {mount_target(v) for v in options.remove_mounts}
    seen = set()
    for mount in additions:
        key = mount_target(mount.target)
        if key in seen or key in removals:
            raise ValueError(f"conflicting mount changes at {key}")
        seen.add(key)
        old = mounts.get(key)
        if old and old.kind == "shell_state":
            raise ValueError("managed mounts cannot be changed")
        if old and old.kind == "workspace":
            if not mount.source.is_dir():
                raise ValueError("workspace source must be a directory")
            mount = replace(mount, kind="workspace")
        mounts[key] = replace(mount, target=key)
    for key in removals:
        if key in mounts and mounts[key].kind == "shell_state":
            raise ValueError("managed mounts cannot be removed")
        mounts.pop(key, None)

    ports = {port_key(p): p for p in before.ports}
    additions_p = [explicit_port(v) for v in options.ports]
    removals_p = []
    for value in options.remove_ports:
        raw = PortBind.parse(value)
        removals_p.append(raw if raw in before.ports else explicit_port(value))
    seen = set()
    for port in additions_p:
        key = port_key(port)
        if key in seen or any(port_key(p) == key for p in removals_p):
            raise ValueError("conflicting port changes")
        seen.add(key)
        ports[key] = port
    for port in removals_p:
        if ports.get(port_key(port)) == port:
            del ports[port_key(port)]

    env = dict(v.split("=", 1) for v in before.env)
    seen = set()
    for entry in options.env:
        if "=" not in entry:
            raise ValueError("environment values must use KEY=VALUE")
        key, value = entry.split("=", 1)
        if key in seen or key in options.unset_env:
            raise ValueError(f"conflicting environment changes for {key}")
        seen.add(key)
        env[key] = value
    for key in options.unset_env:
        if not key or "=" in key:
            raise ValueError("unset-env requires a variable name")
        env.pop(key, None)
    resources = before.resources.to_data()
    for key, value in options.resources:
        if key not in resources:
            raise ValueError(f"unknown resource: {key}")
        resources[key] = value
    after = replace(
        before,
        mounts=tuple(mounts.values()),
        ports=tuple(ports.values()),
        env=tuple(f"{k}={v}" for k, v in env.items())
        if options.env or options.unset_env
        else before.env,
        resources=ResourceSpec(**resources),
        shell=before.shell if options.shell is None else normalize_shell(options.shell),
        workdir=before.workdir if options.workdir is None else options.workdir,
    )
    if after.workdir and not after.workdir.startswith("/"):
        raise ValueError("working directory must be absolute")
    validate_mounts_ports(after)
    changes = {}
    old_data, new_data = before.to_data(), after.to_data()
    for key in old_data:
        if old_data[key] != new_data[key]:
            if key == "env":
                old_env = dict(v.split("=", 1) for v in before.env)
                changes[key] = {
                    k: "removed" if k not in env else "set"
                    for k in sorted(old_env.keys() | env.keys())
                    if old_env.get(k) != env.get(k)
                }
            else:
                changes[key] = dict(before=old_data[key], after=new_data[key])
    effect = "none"
    if changes:
        effect = (
            "metadata"
            if changes.keys() <= {"shell"}
            else (
                "resources" if changes.keys() <= {"shell", "resources"} else "replace"
            )
        )
        # Both CLI update APIs treat zero memory/CPU as "unchanged", not a
        # reliable removal of an existing ceiling. Use the preservation path.
        if any(
            getattr(before.resources, field) is not None
            and getattr(after.resources, field) is None
            for field in ("cpus", "memory_mib")
        ):
            effect = "replace"
    return UpdatePlan(before, after, effect, changes)
