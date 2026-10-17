"""Explicit schema-2 adoption. Normal execution never imports this decoder."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from uuid import uuid4

from .domain import (
    ContainerAllocation,
    IsolationMode,
    Machine,
    MachineBinding,
    MachineSpec,
    MissingContainer,
    NetworkMode,
    Operation,
    RuntimeState,
    verify_observation,
)
from .engines import Engine, get_engine
from .state import MachineStore, atomic_write


@dataclass(frozen=True)
class MigrationPlan:
    source_sha256: str
    machine: Machine
    observed_configuration: str
    observed_state: RuntimeState

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_sha256, str)
            or re.fullmatch(r"[a-f0-9]{64}", self.source_sha256) is None
        ):
            raise ValueError("adoption requires the original record's SHA-256 digest")
        if (
            self.machine.operation is not Operation.READY
            or self.machine.spec.isolation is not IsolationMode.NATIVE
        ):
            raise ValueError("adoption must preserve an existing native machine")
        configuration = json.loads(self.observed_configuration)
        if not isinstance(configuration, dict):
            raise ValueError("observed configuration must be an object")
        object.__setattr__(
            self, "observed_configuration", json.dumps(configuration, sort_keys=True)
        )
        object.__setattr__(self, "observed_state", RuntimeState(self.observed_state))
        if self.observed_state is RuntimeState.MISSING:
            raise ValueError("cannot adopt a missing container")

    def to_data(self) -> dict:
        return {
            "plan_version": 1,
            "source_sha256": self.source_sha256,
            "machine": self.machine.to_data(),
            "observed_configuration": json.loads(self.observed_configuration),
            "observed_state": self.observed_state.value,
        }

    @classmethod
    def from_data(cls, data: dict) -> "MigrationPlan":
        if (
            not isinstance(data, dict)
            or type(data.get("plan_version")) is not int
            or data["plan_version"] != 1
        ):
            raise ValueError("unsupported adoption plan version")
        return cls(
            data["source_sha256"],
            Machine.from_data(data["machine"]),
            json.dumps(data["observed_configuration"], sort_keys=True),
            data["observed_state"],
        )


class MigrationService:
    def __init__(self, store: MachineStore, engines: dict[str, Engine] | None = None):
        self.store, self.engines = store, engines or {}

    def _engine(self, name: str) -> Engine:
        return self.engines.get(name) or get_engine(name)

    def plan(self, name: str) -> MigrationPlan:
        source = self.store.read(name)
        spec, created_at = _decode_schema2(source)
        if spec.name != name:
            raise ValueError("old machine record name does not match its directory")
        observed = self._engine(spec.engine).observe(name)
        if isinstance(observed, MissingContainer):
            raise ValueError("cannot adopt a missing container")
        labels = dict(observed.labels)
        if labels.get("mimchine") != "1":
            raise ValueError("named container has no mimchine ownership label")
        _verify_declared_configuration(spec, observed.configuration)
        constraints = tuple(
            (key, value)
            for key, value in observed.labels
            if key in {"mimchine", "mimchine.id"}
        )
        allocation = ContainerAllocation(
            observed.container_id, observed.context, constraints
        )
        binding = MachineBinding(allocation, observed.image_id, observed.runtime)
        machine = Machine(
            str(uuid4()), spec, created_at, Operation.READY, binding=binding
        )
        return MigrationPlan(
            hashlib.sha256(source).hexdigest(),
            machine,
            observed.configuration,
            observed.state,
        )

    def apply(self, name: str, plan: MigrationPlan) -> Machine:
        machine = plan.machine
        if machine.name != name or machine.binding is None:
            raise ValueError("adoption plan does not name this machine")
        with self.store.lock(name):
            source = self.store.read(name)
            data = json.loads(source)
            if not isinstance(data, dict):
                raise ValueError("machine record must be an object")
            if data.get("schema_version") == 3:
                current = self.store.load(name)
                if (
                    current.id == machine.id
                    and current.binding == machine.binding
                    and current.spec == machine.spec
                ):
                    return current
                raise ValueError(
                    "machine has already been adopted with a different plan"
                )
            if hashlib.sha256(source).hexdigest() != plan.source_sha256:
                raise ValueError(
                    "old machine record changed; generate a new adoption plan"
                )
            spec, created_at = _decode_schema2(source)
            if spec != machine.spec or created_at != machine.created_at:
                raise ValueError("adoption plan does not preserve the original intent")
            observed = verify_observation(
                self._engine(spec.engine).observe(machine.target.container_id),
                machine.target,
                machine.binding,
            )
            if observed is None:
                raise ValueError(
                    "planned container is missing; adoption was not applied"
                )
            if json.loads(observed.configuration) != json.loads(
                plan.observed_configuration
            ):
                raise ValueError(
                    "container configuration changed; generate a new adoption plan"
                )
            _verify_declared_configuration(spec, observed.configuration)
            backup = self.store.machine_dir(name) / "machine.schema-2.json"
            if backup.exists() and backup.read_bytes() != source:
                raise ValueError(
                    "existing schema-2 backup differs; reconcile it before adoption"
                )
            if not backup.exists():
                atomic_write(backup, source)
            self.store.save(machine)
            return machine


def _decode_schema2(source: bytes) -> tuple[MachineSpec, str]:
    data = json.loads(source)
    if (
        not isinstance(data, dict)
        or type(data.get("schema_version")) is not int
        or data["schema_version"] != 2
    ):
        raise ValueError("adoption requires an unmodified schema-2 machine record")
    # Translate once at the migration boundary; no execution-time aliases.
    fields = dict(data, engine=data["runner"], isolation="native")
    return MachineSpec.from_data(fields), data["created_at"]


def _verify_declared_configuration(spec: MachineSpec, configuration: str) -> None:
    data = json.loads(configuration)
    mounts = {item.get("Destination"): item for item in data.get("mounts", [])}
    for mount in spec.mounts:
        observed = mounts.get(mount.target)
        if (
            observed is None
            or observed.get("Source") != str(mount.source)
            or observed.get("RW") is not (not mount.read_only)
        ):
            raise ValueError(
                f"declared mount [{mount.target}] does not match the existing container"
            )
    for port in spec.ports:
        candidates = data.get("ports", {}).get(f"{port.guest}/{port.protocol}", [])
        if not any(
            item.get("HostPort") == str(port.host)
            and (port.address is None or item.get("HostIp") == port.address)
            for item in candidates
        ):
            raise ValueError(
                f"declared publication [{port.arg()}] does not match the existing container"
            )
    if (
        spec.network is not NetworkMode.DEFAULT
        and data.get("network") != spec.network.value
    ):
        raise ValueError("declared network mode does not match the existing container")
    if (
        spec.resources.memory_mib is not None
        and data.get("memory") != spec.resources.memory_mib * 1024 * 1024
    ):
        raise ValueError("declared memory limit does not match the existing container")
    if spec.resources.cpus is not None:
        quota, period = data.get("cpu_quota"), data.get("cpu_period")
        cpus = data.get("nano_cpus", 0) or 0
        if cpus != spec.resources.cpus * 1_000_000_000 and not (
            isinstance(quota, int)
            and isinstance(period, int)
            and period > 0
            and quota == spec.resources.cpus * period
        ):
            raise ValueError("declared CPU limit does not match the existing container")
