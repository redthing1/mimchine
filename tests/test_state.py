from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from support import IMAGE_ID, make_machine

from mimchine.domain import (
    SCHEMA_VERSION,
    GvisorBinding,
    IdentityMode,
    IsolationMode,
    ResourceSpec,
)

from mimchine.state import MachineNotFoundError, MachineStore, MigrationRequiredError


def test_machine_store_saves_loads_lists_and_deletes(tmp_path: Path) -> None:
    store = MachineStore(tmp_path)
    record = make_machine(
        "dev",
        "alpine",
        "podman",
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


def test_machine_store_does_not_hide_migration_needs(tmp_path):
    store = MachineStore(tmp_path)
    path = store.record_path("old")
    path.parent.mkdir(parents=True)
    original = b'{"schema_version": 2}'
    path.write_bytes(original)
    with pytest.raises(MigrationRequiredError, match="mim migrate old --plan"):
        store.list()
    assert path.read_bytes() == original


def test_machine_store_delete_removes_owned_directory(tmp_path: Path) -> None:
    store = MachineStore(tmp_path)
    record = make_machine(
        "dev",
        "alpine",
        "podman",
    )
    store.save(record)
    (store.machine_dir("dev") / "sidecar").write_text("x", encoding="utf-8")

    store.delete("dev")

    assert not store.machine_dir("dev").exists()


def test_machine_store_delete_does_not_hide_filesystem_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(path: Path) -> None:
        raise PermissionError(path)

    store = MachineStore(tmp_path)
    store.save(
        make_machine(
            "dev",
            "alpine",
            "podman",
        )
    )
    monkeypatch.setattr("mimchine.state.shutil.rmtree", fail)

    with pytest.raises(PermissionError):
        store.delete("dev")


def test_failed_atomic_replace_preserves_old_record_and_cleans_temporary_file(
    tmp_path, monkeypatch
):
    from dataclasses import replace

    store = MachineStore(tmp_path)
    machine = make_machine()
    store.save(machine)
    original = store.read("dev")

    def fail_replace(source, destination):
        raise OSError("atomic replace failed")

    monkeypatch.setattr("mimchine.state.os.replace", fail_replace)
    with pytest.raises(OSError, match="atomic replace failed"):
        store.save(replace(machine, error="new failure"))
    assert store.read("dev") == original
    assert not list(store.machine_dir("dev").glob(".mim-*"))


@pytest.mark.parametrize("version", [None, 1, 5, "2", True, 3.0])
def test_unsupported_schema_does_not_suggest_an_unavailable_migration(
    tmp_path, version
):
    import json

    store = MachineStore(tmp_path)
    path = store.record_path("dev")
    path.parent.mkdir()
    path.write_text(json.dumps({"schema_version": version}))
    with pytest.raises(ValueError, match="unsupported machine record schema") as error:
        store.load("dev")
    assert not isinstance(error.value, MigrationRequiredError)


def test_invalid_lock_name_does_not_create_store_state(tmp_path):
    store = MachineStore(tmp_path / "machines")
    with pytest.raises(ValueError):
        with store.lock("../other"):
            pytest.fail("invalid lock was acquired")
    assert not store.base_dir.exists()


@pytest.mark.parametrize("gvisor", [False, True])
def test_schema3_resources_normalize_without_changing_allocation_or_writing_on_read(
    tmp_path, gvisor
):
    store = MachineStore(tmp_path / "machines")
    machine = make_machine(
        resources=ResourceSpec(1, 2048), shell_state=False, identity=IdentityMode.ROOT
    )
    if gvisor:
        runtime = GvisorBinding(
            tmp_path / "runtime",
            "0" * 64,
            "online",
            IMAGE_ID,
            store.machine_dir("dev") / "gvisor",
        )
        machine = replace(
            machine,
            spec=replace(machine.spec, isolation=IsolationMode.GVISOR),
            gvisor=runtime,
            binding=replace(machine.binding, runtime=str(runtime.entrypoint)),
        )
    data = machine.to_data()
    data["schema_version"] = 3
    data["spec"]["resources"] = {"cpus": 1, "memory_mib": 2048}
    path = store.record_path("dev")
    path.parent.mkdir(parents=True)
    original = json.dumps(data).encode()
    path.write_bytes(original)

    normalized = store.load("dev")
    assert path.read_bytes() == original
    assert normalized.id == machine.id
    assert normalized.binding == machine.binding
    assert normalized.gvisor == machine.gvisor
    assert normalized.spec.resources == ResourceSpec(
        1, 2048, 0 if gvisor else None, 1024 if gvisor else None
    )
    store.save(normalized)
    assert json.loads(path.read_bytes())["schema_version"] == SCHEMA_VERSION
    assert store.load("dev") == normalized


def test_schema4_unset_resources_do_not_inherit_the_old_gvisor_defaults(tmp_path):
    store = MachineStore(tmp_path / "machines")
    machine = make_machine(shell_state=False, identity=IdentityMode.ROOT)
    runtime = GvisorBinding(
        tmp_path / "runtime",
        "0" * 64,
        "online",
        IMAGE_ID,
        store.machine_dir("dev") / "gvisor",
    )
    machine = replace(
        machine,
        spec=replace(machine.spec, isolation=IsolationMode.GVISOR),
        gvisor=runtime,
        binding=replace(machine.binding, runtime=str(runtime.entrypoint)),
    )
    store.save(machine)
    assert store.load("dev").spec.resources == ResourceSpec()
