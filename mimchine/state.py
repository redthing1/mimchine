from __future__ import annotations

import fcntl
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .domain import SCHEMA_VERSION, Machine, validate_machine_name


class MachineNotFoundError(KeyError):
    pass


class MigrationRequiredError(ValueError):
    pass


def atomic_write(path: Path, contents: bytes) -> None:
    ensure_directory(path.parent)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=".mim-", delete=False
    ) as file:
        temporary = Path(file.name)
        try:
            file.write(contents)
            file.flush()
            os.fsync(file.fileno())
            os.replace(temporary, path)
            sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_directory(path: Path) -> None:
    if path.is_dir():
        return
    ensure_directory(path.parent)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        if not path.is_dir():
            raise
    # Persist the directory entry as well as the files subsequently inside it.
    sync_directory(path.parent)


class MachineStore:
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)

    def machine_dir(self, name: str) -> Path:
        return self.base_dir / validate_machine_name(name)

    def record_path(self, name: str) -> Path:
        return self.machine_dir(name) / "machine.json"

    @contextmanager
    def lock(self, name: str) -> Iterator[None]:
        name = validate_machine_name(name)
        lock_dir = self.base_dir / ".locks"
        ensure_directory(lock_dir)
        # Lock files survive deletion so waiters coordinate on one inode.
        descriptor = os.open(
            lock_dir / f"{name}.lock",
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def exists(self, name: str) -> bool:
        return self.record_path(name).is_file()

    def save(self, machine: Machine) -> None:
        atomic_write(
            self.record_path(machine.name),
            (json.dumps(machine.to_data(), indent=2, sort_keys=True) + "\n").encode(),
        )

    def read(self, name: str) -> bytes:
        try:
            return self.record_path(name).read_bytes()
        except FileNotFoundError as exc:
            raise MachineNotFoundError(validate_machine_name(name)) from exc

    def load(self, name: str) -> Machine:
        data = json.loads(self.read(name))
        if not isinstance(data, dict):
            raise ValueError(
                f"machine record is not an object: {self.record_path(name)}"
            )
        version = data.get("schema_version")
        if type(version) is int and version == 2:
            raise MigrationRequiredError(
                f"machine [{name}] requires migration; run: mim migrate {name} --plan"
            )
        if type(version) is int and version == 3:
            # Normalize the old implicit gVisor policy at the record boundary.
            # Reads stay read-only; the next ordinary save writes schema 4.
            spec = data["spec"]
            resources = spec.setdefault("resources", {})
            if spec["isolation"] == "gvisor":
                if resources.get("cpus") is None or resources.get("memory_mib") is None:
                    raise ValueError(
                        "schema-3 gVisor record has no finite CPU/memory policy"
                    )
                resources.update(swap_mib=0, tasks=1024)
            data["schema_version"] = version = SCHEMA_VERSION
        if type(version) is not int or version != SCHEMA_VERSION:
            raise ValueError(f"unsupported machine record schema: {version!r}")
        machine = Machine.from_data(data)
        if machine.name != name:
            raise ValueError(
                f"machine record name does not match its directory: {name}"
            )
        if (
            machine.gvisor
            and machine.gvisor.control_dir
            != (self.machine_dir(name) / "gvisor").resolve()
        ):
            raise ValueError(
                "gVisor control directory does not match the machine store"
            )
        return machine

    def delete(self, name: str) -> None:
        shutil.rmtree(self.machine_dir(name))
        sync_directory(self.base_dir)

    def names(self) -> list[str]:
        if not self.base_dir.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self.base_dir.iterdir()
            if not entry.name.startswith(".")
            and entry.is_dir()
            and (entry / "machine.json").is_file()
        )

    def list(self) -> list[Machine]:
        # Corruption and migration needs remain visible.
        return [self.load(name) for name in self.names()]
