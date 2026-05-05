from __future__ import annotations

from pathlib import Path

import pytest

from mimchine.domain import (
    ImageSource,
    MachineRecord,
    MachineSpec,
    MountSpec,
    NetworkMode,
    NetworkSpec,
    PortBind,
    ResourceSpec,
    ShellStateSpec,
    validate_machine_name,
)


def test_machine_record_round_trips(tmp_path: Path) -> None:
    spec = MachineSpec(
        name="dev",
        image=ImageSource.oci_reference("fedora:latest"),
        runner="podman",
        mounts=(MountSpec(tmp_path, "/work/project", kind="workspace"),),
        ports=(PortBind(8080, 80),),
        env=("APP_ENV=dev",),
        network=NetworkSpec(NetworkMode.NONE),
    )

    record = MachineRecord.from_spec(spec, created_at="2026-01-01T00:00:00+00:00")
    assert MachineRecord.from_data(record.to_data()) == record


@pytest.mark.parametrize("name", ["dev", "fedora-41", "a_b.c"])
def test_machine_name_accepts_simple_names(name: str) -> None:
    assert validate_machine_name(name) == name


@pytest.mark.parametrize("name", ["", ".", "..", "bad/name", "bad name"])
def test_machine_name_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(ValueError):
        validate_machine_name(name)


def test_smolmachine_cli_image_is_path(tmp_path: Path) -> None:
    path = tmp_path / "tool.smolmachine"
    source = ImageSource.from_cli(str(path))
    assert source.kind.value == "smolmachine"
    assert source.value == str(path.resolve())


def test_restricted_network_cannot_be_no_network() -> None:
    with pytest.raises(ValueError, match="no network"):
        NetworkSpec(NetworkMode.NONE, allow_cidrs=("10.0.0.0/8",))


def test_resource_values_must_be_integers() -> None:
    with pytest.raises(ValueError, match="integer"):
        ResourceSpec(cpus=True)


def test_ports_must_be_integers() -> None:
    with pytest.raises(ValueError, match="integer"):
        PortBind(True, 80)


def test_shell_state_enabled_must_be_boolean() -> None:
    with pytest.raises(ValueError, match="boolean"):
        ShellStateSpec(enabled="false")


def test_record_boolean_fields_must_be_boolean(tmp_path: Path) -> None:
    record = MachineRecord.from_spec(
        MachineSpec("dev", ImageSource.oci_reference("alpine"), "podman"),
        created_at="2026-01-01T00:00:00+00:00",
    ).to_data()
    record["ssh_agent"] = "false"

    with pytest.raises(ValueError, match="boolean"):
        MachineRecord.from_data(record)


def test_mount_record_read_only_must_be_boolean(tmp_path: Path) -> None:
    record = MachineRecord.from_spec(
        MachineSpec(
            "dev",
            ImageSource.oci_reference("alpine"),
            "podman",
            mounts=(MountSpec(tmp_path, "/work/dev"),),
        ),
        created_at="2026-01-01T00:00:00+00:00",
    ).to_data()
    record["mounts"][0]["read_only"] = "false"

    with pytest.raises(ValueError, match="boolean"):
        MachineRecord.from_data(record)


def test_record_port_fields_must_be_numbers(tmp_path: Path) -> None:
    record = MachineRecord.from_spec(
        MachineSpec(
            "dev",
            ImageSource.oci_reference("alpine"),
            "podman",
            ports=(PortBind(8080, 80),),
        ),
        created_at="2026-01-01T00:00:00+00:00",
    ).to_data()
    record["ports"][0]["host"] = "8080"

    with pytest.raises(ValueError, match="integer"):
        MachineRecord.from_data(record)


def test_record_schema_version_must_be_number(tmp_path: Path) -> None:
    record = MachineRecord.from_spec(
        MachineSpec("dev", ImageSource.oci_reference("alpine"), "podman"),
        created_at="2026-01-01T00:00:00+00:00",
    ).to_data()
    record["schema_version"] = "1"

    with pytest.raises(ValueError, match="integer"):
        MachineRecord.from_data(record)
