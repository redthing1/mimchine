from __future__ import annotations

from dataclasses import replace

import pytest
from support import context, make_machine

from mimchine.domain import (
    ContainerAllocation,
    ExecSpec,
    Machine,
    MachineSpec,
    MountSpec,
    NetworkMode,
    Operation,
    PortBind,
    ResourceSpec,
    validate_machine_name,
)


def test_spec_and_bound_machine_round_trip(tmp_path):
    machine = make_machine(
        name=" dev ",
        image=" fedora:latest ",
        mounts=(
            MountSpec(tmp_path, "/work/project", kind="workspace", options=("z",)),
        ),
        ports=(PortBind(8080, 80),),
        env=("APP_ENV=dev",),
        network=NetworkMode.NONE,
        gpu=True,
        job_limits=True,
    )
    assert machine.name == "dev"
    assert machine.spec.image == "fedora:latest"
    assert MachineSpec.from_data(machine.spec.to_data()) == machine.spec
    assert Machine.from_data(machine.to_data()) == machine


@pytest.mark.parametrize("name", ["dev", "fedora-41", "a_b.c"])
def test_machine_name_accepts_simple_names(name):
    assert validate_machine_name(name) == name


@pytest.mark.parametrize(
    "name", ["", ".", "..", "bad/name", "bad name", "Bad", "bad%h", "-bad"]
)
def test_machine_name_rejects_unsafe_names(name):
    with pytest.raises(ValueError):
        validate_machine_name(name)


def test_resource_values_must_be_integers():
    with pytest.raises(ValueError, match="integer"):
        ResourceSpec(cpus=True)


def test_ports_must_be_integers():
    with pytest.raises(ValueError, match="integer"):
        PortBind(True, 80)


@pytest.mark.parametrize("value", ["KEY", "=value", "   "])
def test_environment_requires_a_key_value_pair(value):
    with pytest.raises(ValueError):
        ExecSpec(("true",), env=(value,))


@pytest.mark.parametrize("field", ["ssh_agent", "gpu", "job_limits", "shell_state"])
def test_spec_boolean_fields_must_be_boolean(field):
    data = make_machine().spec.to_data()
    data[field] = "false"
    with pytest.raises(ValueError, match="boolean"):
        MachineSpec.from_data(data)


def test_mount_read_only_must_be_boolean(tmp_path):
    with pytest.raises(ValueError, match="boolean"):
        MountSpec.from_data(
            {"source": str(tmp_path), "target": "/work", "read_only": "false"}
        )


def test_port_fields_must_be_numbers():
    with pytest.raises(ValueError, match="integer"):
        PortBind.from_data({"host": "8080", "guest": 80})


@pytest.mark.parametrize("version", [1, 2, "3", True])
def test_execution_schema_does_not_accept_old_or_coerced_versions(version):
    data = make_machine().to_data()
    data["schema_version"] = version
    with pytest.raises(ValueError):
        Machine.from_data(data)


@pytest.mark.parametrize("value", ["abc", "a" * 12, "not-an-id", "A" * 64])
def test_bindings_require_full_immutable_ids(value):
    with pytest.raises(ValueError, match="full immutable"):
        ContainerAllocation(value, context(), (("mimchine", "1"),))


def test_ready_machine_cannot_be_unbound():
    with pytest.raises(ValueError, match="requires a binding"):
        replace(make_machine(), binding=None)


def test_pending_allocation_and_binding_are_mutually_exclusive():
    machine = make_machine()
    with pytest.raises(ValueError, match="both"):
        replace(machine, allocation=machine.target)


def test_creation_reservation_is_serializable_without_a_target():
    machine = replace(
        make_machine(),
        operation=Operation.CREATING,
        binding=None,
        creation_context=context(),
    )
    assert machine.target is None
    assert Machine.from_data(machine.to_data()) == machine


@pytest.mark.parametrize(
    ("text", "address", "protocol", "rendered"),
    [
        ("8080:80", None, "tcp", "8080:80"),
        ("127.0.0.1:8080:80", "127.0.0.1", "tcp", "127.0.0.1:8080:80"),
        ("[::1]:8080:80/udp", "::1", "udp", "[::1]:8080:80/udp"),
    ],
)
def test_explicit_port_bindings_round_trip(text, address, protocol, rendered):
    port = PortBind.parse(text)
    assert port.address == address
    assert port.protocol == protocol
    assert port.arg() == rendered
    assert PortBind.from_data(port.to_data()) == port


@pytest.mark.parametrize("address", [True, 2130706433, b"127.0.0.1"])
def test_port_address_cannot_be_coerced_from_nontext(address):
    with pytest.raises(ValueError, match="address must be a string"):
        PortBind(8080, 80, address)


def test_loading_a_machine_does_not_reinterpret_saved_mount_sources(tmp_path):
    source = tmp_path / "workspace"
    source.mkdir()
    machine = make_machine(mounts=(MountSpec(source, "/work"),))
    data = machine.to_data()
    source.rmdir()
    other = tmp_path / "other"
    other.mkdir()
    source.symlink_to(other, target_is_directory=True)
    loaded = Machine.from_data(data)
    assert loaded == machine
    assert loaded.to_data() == data


@pytest.mark.parametrize("field", ["name", "image", "engine"])
def test_saved_intent_does_not_coerce_missing_text(field):
    data = make_machine().spec.to_data()
    data[field] = None
    with pytest.raises(ValueError, match="must be a string"):
        MachineSpec.from_data(data)


def test_pending_allocation_must_match_the_reserved_context_and_ownership():
    machine = make_machine()
    pending = replace(
        machine,
        binding=None,
        operation=Operation.CREATING,
        allocation=machine.target,
        creation_context=machine.target.context,
    )
    assert Machine.from_data(pending.to_data()) == pending
    with pytest.raises(ValueError, match="allocation context"):
        replace(
            pending, creation_context=replace(context(), identity=(("host", "other"),))
        )
    with pytest.raises(ValueError, match="allocation ownership"):
        replace(
            pending, allocation=replace(pending.allocation, labels=(("mimchine", "1"),))
        )
    with pytest.raises(ValueError, match="completed binding"):
        replace(machine, operation=Operation.CREATING)


def test_environment_values_preserve_whitespace():
    env = ("TOKEN=  value with spaces  ", "EMPTY=")
    spec = replace(make_machine().spec, env=env)
    assert MachineSpec.from_data(spec.to_data()).env == env
    assert ExecSpec(("env",), env=env).env == env
