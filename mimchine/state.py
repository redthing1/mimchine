from __future__ import annotations

import json
import shutil
from pathlib import Path

from .domain import MachineRecord, validate_machine_name


class MachineNotFoundError(KeyError):
    pass


class MachineStore:
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)

    def machine_dir(self, name: str) -> Path:
        return self.base_dir / validate_machine_name(name)

    def record_path(self, name: str) -> Path:
        return self.machine_dir(name) / "machine.json"

    def exists(self, name: str) -> bool:
        return self.record_path(name).is_file()

    def save(self, record: MachineRecord) -> None:
        machine_dir = self.machine_dir(record.name)
        machine_dir.mkdir(parents=True, exist_ok=True)
        path = self.record_path(record.name)
        temp_path = path.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps(record.to_data(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(path)

    def load(self, name: str) -> MachineRecord:
        path = self.record_path(name)
        if not path.is_file():
            raise MachineNotFoundError(validate_machine_name(name))

        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"machine record is not an object: {path}")
        return MachineRecord.from_data(data)

    def delete(self, name: str) -> None:
        shutil.rmtree(self.machine_dir(name), ignore_errors=True)

    def list(self) -> list[MachineRecord]:
        if not self.base_dir.is_dir():
            return []

        records: list[MachineRecord] = []
        for entry in sorted(self.base_dir.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            record_path = entry / "machine.json"
            if not record_path.is_file():
                continue
            records.append(self.load(entry.name))
        return records
