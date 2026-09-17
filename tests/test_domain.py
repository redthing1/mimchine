from __future__ import annotations

from pathlib import Path

import pytest

from mimchine.domain import (
    ExecSpec,
    MachineRecord,
    MountSpec,
    NetworkMode,
    PortBind,
    ResourceSpec,
    validate_machine_name,
)


def test_machine_record_round_trips(tmp_path: Path) -> None:
    record = MachineRecord(
        name=" dev ",
        image=" fedora:latest ",
        runner=" podman ",
        created_at=" 2026-01-01T00:00:00+00:00 ",
        mounts=(
            MountSpec(tmp_path, "/work/project", kind="workspace", options=("z",)),
        ),
        ports=(PortBind(8080, 80),),
        env=("APP_ENV=dev",),
        network=NetworkMode.NONE,
        gpu=True,
    )

    assert (record.name, record.image, record.runner, record.created_at) == (
        "dev",
        "fedora:latest",
        "podman",
        "2026-01-01T00:00:00+00:00",
    )
    assert MachineRecord.from_data(record.to_data()) == record


@pytest.mark.parametrize("name", ["dev", "fedora-41", "a_b.c"])
def test_machine_name_accepts_simple_names(name: str) -> None:
    assert validate_machine_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "bad/name", "bad name", "Bad", "bad%h", "-bad"],
)
def test_machine_name_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(ValueError):
        validate_machine_name(name)


def test_resource_values_must_be_integers() -> None:
    with pytest.raises(ValueError, match="integer"):
        ResourceSpec(cpus=True)


def test_ports_must_be_integers() -> None:
    with pytest.raises(ValueError, match="integer"):
        PortBind(True, 80)


@pytest.mark.parametrize("value", ["KEY", "=value", "   "])
def test_environment_requires_a_key_value_pair(value: str) -> None:
    with pytest.raises(ValueError):
        ExecSpec(("true",), env=(value,))


@pytest.mark.parametrize("field", ["ssh_agent", "gpu", "shell_state"])
def test_record_boolean_fields_must_be_boolean(field: str) -> None:
    record = MachineRecord(
        "dev",
        "alpine",
        "podman",
        created_at="2026-01-01T00:00:00+00:00",
    ).to_data()
    record[field] = "false"

    with pytest.raises(ValueError, match="boolean"):
        MachineRecord.from_data(record)


def test_mount_record_read_only_must_be_boolean(tmp_path: Path) -> None:
    record = MachineRecord(
        "dev",
        "alpine",
        "podman",
        mounts=(MountSpec(tmp_path, "/work/dev"),),
        created_at="2026-01-01T00:00:00+00:00",
    ).to_data()
    record["mounts"][0]["read_only"] = "false"

    with pytest.raises(ValueError, match="boolean"):
        MachineRecord.from_data(record)


def test_record_port_fields_must_be_numbers(tmp_path: Path) -> None:
    record = MachineRecord(
        "dev",
        "alpine",
        "podman",
        ports=(PortBind(8080, 80),),
        created_at="2026-01-01T00:00:00+00:00",
    ).to_data()
    record["ports"][0]["host"] = "8080"

    with pytest.raises(ValueError, match="integer"):
        MachineRecord.from_data(record)


def test_record_schema_version_must_be_number() -> None:
    record = MachineRecord(
        "dev",
        "alpine",
        "podman",
        created_at="2026-01-01T00:00:00+00:00",
    ).to_data()
    record["schema_version"] = "1"

    with pytest.raises(ValueError, match="integer"):
        MachineRecord.from_data(record)


def test_old_machine_record_schema_is_rejected() -> None:
    record = MachineRecord(
        "dev",
        "alpine",
        "podman",
        created_at="2026-01-01T00:00:00+00:00",
    ).to_data()
    record["schema_version"] = 1

    with pytest.raises(ValueError, match="unsupported machine record schema: 1"):
        MachineRecord.from_data(record)
