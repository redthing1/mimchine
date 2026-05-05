from __future__ import annotations

from pathlib import Path

import pytest

from mimchine.domain import ImageSource, MachineRecord, MachineSpec
from mimchine.state import MachineNotFoundError, MachineStore


def test_machine_store_saves_loads_lists_and_deletes(tmp_path: Path) -> None:
    store = MachineStore(tmp_path)
    record = MachineRecord.from_spec(
        MachineSpec("dev", ImageSource.oci_reference("alpine"), "podman"),
        created_at="2026-01-01T00:00:00+00:00",
    )

    store.save(record)

    assert store.exists("dev") is True
    assert store.load("dev") == record
    assert store.list() == [record]

    store.delete("dev")

    assert store.exists("dev") is False
    assert store.list() == []


def test_machine_store_rejects_path_traversal(tmp_path: Path) -> None:
    store = MachineStore(tmp_path)

    with pytest.raises(ValueError):
        store.record_path("../outside")


def test_machine_store_missing_record_raises(tmp_path: Path) -> None:
    store = MachineStore(tmp_path)

    with pytest.raises(MachineNotFoundError):
        store.load("missing")


def test_machine_store_invalid_json_raises(tmp_path: Path) -> None:
    store = MachineStore(tmp_path)
    path = store.record_path("bad")
    path.parent.mkdir(parents=True)
    path.write_text("not-json", encoding="utf-8")

    with pytest.raises(ValueError):
        store.load("bad")


def test_machine_store_delete_removes_owned_directory(tmp_path: Path) -> None:
    store = MachineStore(tmp_path)
    record = MachineRecord.from_spec(
        MachineSpec("dev", ImageSource.oci_reference("alpine"), "podman"),
        created_at="2026-01-01T00:00:00+00:00",
    )
    store.save(record)
    (store.machine_dir("dev") / "sidecar").write_text("x", encoding="utf-8")

    store.delete("dev")

    assert not store.machine_dir("dev").exists()
