"""Engine primitives for explicit, recoverable machine updates."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID

from ..domain import Machine, RootfsImage, RuntimeState, verify_observation
from ..process import ProcessError


def _quoted(value: str) -> str:
    if any(c in value for c in "\n\r\x00"):
        raise ValueError("replacement does not support multiline image configuration")
    return (
        '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'
    )


class UpdateEngineMixin:
    def _image_metadata(self, image: str, *, missing_ok: bool = False) -> dict | None:
        result = self.process.run(
            (*self.prefix, "image", "inspect", image),
            capture=True,
            check=False,
            cwd="/",
        )
        if result.returncode:
            # Do not turn daemon/permission failures into confirmed absence.
            if (
                missing_ok
                and result.returncode != 127
                and any(
                    message in result.stderr.lower()
                    for message in (
                        "no such image",
                        "image not known",
                        "image not found",
                    )
                )
            ):
                return None
            raise ProcessError(result)
        values = json.loads(result.stdout)
        if not isinstance(values, list) or len(values) != 1:
            raise ValueError("invalid image metadata response")
        return values[0]

    def prepare_replacement(self, machine: Machine) -> None:
        observed = self._observe_target(machine)
        if observed.state not in {RuntimeState.RUNNING, RuntimeState.STOPPED}:
            raise ValueError("replacement requires a running or stopped machine")
        data = json.loads(observed.configuration)
        expected = {m.target: m for m in machine.spec.mounts}
        found = set()
        for mount in data["mounts"]:
            destination = mount["Destination"]
            if destination not in expected or mount.get("Type", "bind") != "bind":
                raise ValueError(
                    "replacement cannot preserve unmanaged or named volumes"
                )
            wanted = expected[destination]
            if (
                mount["Source"] != str(wanted.source)
                or mount.get("RW") != (not wanted.read_only)
            ):
                raise ValueError("actual mount differs from saved settings")
            found.add(destination)
        if found != expected.keys():
            raise ValueError("actual mounts differ from saved settings")
        self._import_changes(machine, token="validation")

    def verify_replacement(self, machine: Machine) -> None:
        self.prepare_replacement(machine)
        observed = self._observe_target(machine)
        data = json.loads(observed.configuration)
        actual = sorted(
            (item.get("HostIp") or "0.0.0.0", int(item["HostPort"]), guest)
            for guest, values in data["ports"].items()
            for item in values or []
        )
        wanted = sorted(
            (p.address or "127.0.0.1", p.host, f"{p.guest}/{p.protocol}")
            for p in machine.spec.ports
        )
        if actual != wanted:
            raise ValueError(
                "replacement port publication differs from requested settings"
            )
        env = dict(v.split("=", 1) for v in data.get("env") or [])
        if any(
            env.get(k) != v
            for k, v in (entry.split("=", 1) for entry in machine.spec.env)
        ):
            raise ValueError("replacement environment differs from requested settings")
        if machine.spec.workdir and data.get("workdir") != machine.spec.workdir:
            raise ValueError(
                "replacement working directory differs from requested settings"
            )

    def _import_changes(
        self, machine: Machine, *, token: str
    ) -> tuple[list[str], dict]:
        metadata = self._image_metadata(machine.binding.image_id)
        config = metadata.get("Config") or {}
        if (
            config.get("Volumes")
            or ((config.get("Healthcheck") or {}).get("Test") or ["NONE"])[0] != "NONE"
        ):
            raise ValueError(
                "replacement does not support images with volumes or healthchecks"
            )
        if config.get("OnBuild"):
            raise ValueError(
                "replacement does not support images with ONBUILD instructions"
            )
        changes = [f"LABEL mimchine.snapshot={token}"]
        for entry in config.get("Env") or []:
            key, value = entry.split("=", 1)
            changes.append(f"ENV {key}={_quoted(value)}")
        for field, instruction in (
            ("User", "USER"),
            ("WorkingDir", "WORKDIR"),
            ("StopSignal", "STOPSIGNAL"),
        ):
            if config.get(field):
                changes.append(f"{instruction} {_quoted(config[field])}")
        for key, value in (config.get("Labels") or {}).items():
            if not key.startswith("mimchine"):
                changes.append(f"LABEL {_quoted(key)}={_quoted(value)}")
        for port in sorted(config.get("ExposedPorts") or {}):
            changes.append(f"EXPOSE {_quoted(port)}")
        return changes, metadata

    def preserve_rootfs(
        self, machine: Machine, token: str, directory: Path
    ) -> RootfsImage:
        observed = self._observe_target(machine)
        if observed.state is not RuntimeState.STOPPED:
            raise ValueError("rootfs preservation requires a stopped container")
        config = json.loads(observed.configuration)
        identity = []
        if config.get("user"):
            identity += ["--user", config["user"]]
        if self.name == "podman":
            mappings = config.get("id_mappings") or {}
            if not any(mappings.values()) and config.get("userns"):
                identity += ["--userns", config["userns"]]
            for key, flag in (("UidMap", "--uidmap"), ("GidMap", "--gidmap")):
                for mapping in mappings.get(key) or []:
                    identity += [flag, mapping]
        template = RootfsImage("0" * 64, token, observed.runtime, tuple(identity))
        existing = self._image_metadata(template.tag, missing_ok=True)
        if existing is not None:
            if (existing.get("Config", {}).get("Labels") or {}).get(
                "mimchine.snapshot"
            ) != token:
                raise ValueError("rootfs image ownership differs from update")
            return RootfsImage(existing["Id"], token, observed.runtime, tuple(identity))
        changes, metadata = self._import_changes(machine, token=token)
        archive = directory / f"rootfs-{token}.tar"
        # An interrupted export is always overwritten. Never read or unpack the
        # workload archive in Mim, and never include external mounted contents.
        descriptor = os.open(
            archive, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        os.close(descriptor)
        try:
            self.process.run(
                (
                    *self.prefix,
                    "export",
                    "--output",
                    str(archive),
                    observed.container_id,
                ),
                capture=True,
                cwd="/",
            )
            args = [*self.prefix, "import"]
            if self.name == "podman":
                args += [
                    "--os",
                    metadata.get("Os", "linux"),
                    "--arch",
                    metadata["Architecture"],
                ]
                if metadata.get("Variant"):
                    args += ["--variant", metadata["Variant"]]
            else:
                platform = f"{metadata.get('Os', 'linux')}/{metadata['Architecture']}"
                if metadata.get("Variant"):
                    platform += "/" + metadata["Variant"]
                args += ["--platform", platform]
            for change in changes:
                args += ["--change", change]
            args += [str(archive), template.tag]
            self.process.run(args, capture=True, cwd="/")
            image = self._image_metadata(template.tag)
            if (image.get("Config", {}).get("Labels") or {}).get(
                "mimchine.snapshot"
            ) != token:
                raise ValueError("imported image ownership differs")
            return RootfsImage(image["Id"], token, observed.runtime, tuple(identity))
        finally:
            archive.unlink(missing_ok=True)

    def remove_rootfs(self, machine: Machine, *, token: str | None = None) -> None:
        if token is None and machine.rootfs is None:
            return
        context = machine.target.context if machine.target else machine.creation_context
        if self.context() != context:
            raise ValueError("engine context changed before image cleanup")
        reference = (
            f"localhost/mim-rootfs:{UUID(token).hex}"
            if token is not None
            else machine.rootfs.image_id
        )
        owner = token if token is not None else machine.rootfs.token
        image = self._image_metadata(reference, missing_ok=True)
        if image is None:
            return
        if (image.get("Config", {}).get("Labels") or {}).get(
            "mimchine.snapshot"
        ) != owner:
            raise ValueError("rootfs image ownership differs; cleanup refused")
        self.process.run(
            (*self.prefix, "image", "rm", image["Id"]),
            capture=True,
            cwd="/",
        )

    def update_resources(self, before: Machine, after: Machine) -> None:
        if before.gvisor:
            raise ValueError("gVisor resource updates are not supported")
        observed = self._observe_target(before)
        r = after.spec.resources
        args = [
            "update",
            f"--cpus={r.cpus or 0}",
            f"--memory={str(r.memory_mib) + 'm' if r.memory_mib else 0}",
            f"--memory-swap={str(r.memory_mib + r.swap_mib) + 'm' if r.memory_mib and r.swap_mib is not None else -1}",
            f"--pids-limit={r.tasks if r.tasks is not None else -1}",
            observed.container_id,
        ]
        self.process.run((*self.prefix, *args), capture=True, cwd="/")
        current = verify_observation(
            self.observe(observed.container_id), before.target, before.binding
        )
        if current is None:
            raise ValueError("container disappeared during resource update")
        data = json.loads(current.configuration)
        expected_memory = (r.memory_mib or 0) * 1024 * 1024
        actual_cpus = (data.get("nano_cpus") or 0) / 1e9 or (
            max(data.get("cpu_quota") or 0, 0) / (data.get("cpu_period") or 100000)
        )
        if (data.get("memory") or 0) != expected_memory or actual_cpus != (r.cpus or 0):
            raise ValueError("engine did not apply requested resource limits")
        if (r.tasks is not None and data.get("pids_limit") != r.tasks) or (
            r.tasks is None and (data.get("pids_limit") or 0) > 0
        ):
            raise ValueError("engine did not apply requested task limit")
        if (
            r.memory_mib
            and r.swap_mib is not None
            and data.get("memory_swap") != (r.memory_mib + r.swap_mib) * 1024 * 1024
        ):
            raise ValueError("engine did not apply requested swap limit")
        if r.memory_mib and r.swap_mib is None and data.get("memory_swap") != -1:
            raise ValueError("engine did not clear the swap limit")
