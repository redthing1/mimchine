"""Machine process ownership and optional aggregate limits through user systemd."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

from .domain import Machine
from .process import ProcessError, ProcessRunner
from .scope import check_limits, identity


class SystemdOwner:
    def __init__(self, process: ProcessRunner, *, root: Path = Path("/sys/fs/cgroup")):
        self.process, self.root = process, root

    def _state(self, machine: Machine) -> dict[str, str]:
        result = self.process.run(
            (
                "systemctl",
                "--user",
                "show",
                machine.resource_unit,
                "--property=LoadState,ActiveState,Description,ControlGroup,Transient,FragmentPath,DropInPaths",
            ),
            capture=True,
            check=False,
            cwd="/",
        )
        state = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        if state.get("LoadState") == "not-found":
            return state
        if result.returncode:
            raise ProcessError(result)
        # Merely querying a valid slice name creates a synthetic, inactive
        # unit. Generic slice.d defaults apply even to that synthetic unit;
        # a unit-specific definition or drop-in still requires ownership.
        if (
            state.get("LoadState") == "loaded"
            and state.get("ActiveState") == "inactive"
            and state.get("Transient") == "no"
            and state.get("FragmentPath") == ""
            and all(
                Path(path).parent.name == "slice.d"
                for path in state.get("DropInPaths", "").split()
            )
            and state.get("ControlGroup") == ""
        ):
            return {"LoadState": "not-found"}
        if state.get("Description") != f"mimchine {machine.id}":
            raise ValueError("resource unit ownership does not match the machine")
        return state

    def ensure(self, machine: Machine) -> Path:
        if sys.platform != "linux" or os.getuid() == 0:
            raise ValueError(
                "gVisor requires local rootless Linux with a user systemd manager"
            )
        state = self._state(machine)
        if state.get("LoadState") == "not-found":
            resources = machine.spec.resources
            properties = [("Description", "s", f"mimchine {machine.id}")]
            for name, value, scale in (
                ("MemoryMax", resources.memory_mib, 1024 * 1024),
                ("MemorySwapMax", resources.swap_mib, 1024 * 1024),
                ("CPUQuotaPerSecUSec", resources.cpus, 1000000),
                ("TasksMax", resources.tasks, 1),
            ):
                if value is not None:
                    properties.append((name, "t", str(value * scale)))
            if resources.cpus is not None:
                properties.append(("CPUQuotaPeriodUSec", "t", "100000"))
            self.process.run(
                (
                    "busctl",
                    "--user",
                    "--timeout=5",
                    "call",
                    "org.freedesktop.systemd1",
                    "/org/freedesktop/systemd1",
                    "org.freedesktop.systemd1.Manager",
                    "StartTransientUnit",
                    "ssa(sv)a(sa(sv))",
                    machine.resource_unit,
                    "fail",
                    str(len(properties)),
                    *(item for prop in properties for item in prop),
                    "0",
                ),
                capture=True,
                cwd="/",
            )
        elif state.get("ActiveState") != "active":
            self.process.run(
                ("systemctl", "--user", "start", machine.resource_unit),
                capture=True,
                cwd="/",
            )
        deadline = time.monotonic() + 5
        while True:
            state = self._state(machine)
            if state.get("ActiveState") == "active":
                return self.verify(machine, state=state)
            if time.monotonic() >= deadline:
                raise ValueError("machine resource unit did not become active")
            time.sleep(0.05)

    def verify(self, machine: Machine, *, state: dict[str, str] | None = None) -> Path:
        state = self._state(machine) if state is None else state
        group = self._group(machine, state)
        if not group.is_dir():
            raise ValueError("machine resource owner cgroup is absent")
        check_limits(group, **machine.spec.resources.to_data())
        return group

    def _group(self, machine: Machine, state: dict[str, str]) -> Path:
        relative = Path(state.get("ControlGroup", ""))
        if (
            state.get("ActiveState") != "active"
            or not relative.is_absolute()
            or ".." in relative.parts
            or relative.name != machine.resource_unit
            or f"user@{os.getuid()}.service" not in relative.parts
        ):
            raise ValueError(
                "machine resource owner is absent or outside the local user manager"
            )
        return self.root / str(relative).lstrip("/")

    def command(self, machine: Machine, args: tuple[str, ...]) -> tuple[str, ...]:
        group = self.verify(machine)
        device, inode = identity(group)
        # Validate ownership after capturing the directory identity. A slice
        # recreated between the first query and stat must not be accepted.
        if self._group(machine, self._state(machine)) != group or identity(group) != (
            device,
            inode,
        ):
            raise ValueError("machine resource owner changed before dispatch")
        # The helper checks the same directory inside the new scope, even when
        # there are no ceilings to distinguish an accidentally recreated slice.
        return (
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            "--collect",
            "--expand-environment=no",
            f"--slice={machine.resource_unit}",
            f"--unit=mimcall{uuid4().hex}.scope",
            "--",
            sys.executable,
            "-I",
            str(Path(__file__).with_name("scope.py")),
            machine.resource_unit,
            str(device),
            str(inode),
            json.dumps(machine.spec.resources.to_data(), separators=(",", ":")),
            *args,
        )

    def remove(self, machine: Machine) -> None:
        if self._state(machine).get("LoadState") == "not-found":
            return
        self.process.run(
            ("systemctl", "--user", "stop", machine.resource_unit),
            capture=True,
            cwd="/",
        )
        if self._state(machine).get("ActiveState") == "active":
            raise ValueError("machine resource owner remains active after removal")
