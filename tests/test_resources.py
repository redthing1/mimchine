from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from support import make_machine

from mimchine.domain import ResourceSpec
from mimchine.process import ProcessResult
from mimchine.resources import SystemdOwner
from mimchine.scope import cgroup_path, check_limits, identity


def limits(
    group,
    *,
    memory="1073741824",
    swap="0",
    cpu="100000 100000",
    tasks="1024",
):
    group.mkdir(parents=True, exist_ok=True)
    for name, value in {
        "memory.max": memory,
        "memory.swap.max": swap,
        "cpu.max": cpu,
        "pids.max": tasks,
    }.items():
        (group / name).write_text(value)


@pytest.mark.parametrize(
    "changed",
    [
        dict(memory="max"),
        dict(swap="max"),
        dict(cpu="max 100000"),
        dict(cpu="200000 100000"),
        dict(tasks="max"),
    ],
)
def test_actual_missing_or_unlimited_controller_blocks_dispatch(tmp_path, changed):
    limits(tmp_path, **changed)
    with pytest.raises(ValueError, match="not enforced"):
        check_limits(tmp_path, cpus=1, memory_mib=1024, swap_mib=0, tasks=1024)


def test_cgroup_path_rejects_traversal_and_ambiguous_hierarchy(tmp_path):
    (tmp_path / "42").mkdir()
    file = tmp_path / "42/cgroup"
    for data in ("0::/../other", "0::relative", "0::/one\n0::/two", "1:memory:/legacy"):
        file.write_text(data)
        with pytest.raises(ValueError, match="cgroup v2"):
            cgroup_path(42, proc=tmp_path, root=tmp_path)


class Manager:
    def __init__(self, machine, root):
        self.machine, self.root, self.calls = machine, root, []
        self.state = dict(
            LoadState="loaded",
            ActiveState="inactive",
            Description="Synthetic slice",
            ControlGroup="",
            Transient="no",
            FragmentPath="",
            DropInPaths="/usr/lib/systemd/user/slice.d/defaults.conf",
        )

    def run(self, args, **kwargs):
        self.calls.append(tuple(args))
        if args[0] == "busctl":
            relative = f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/{self.machine.resource_unit}"
            self.state.update(
                ActiveState="active",
                Description=f"mimchine {self.machine.id}",
                ControlGroup=relative,
                Transient="yes",
            )
            properties = dict(zip(args[12::3], args[14::3]))
            period = int(properties.get("CPUQuotaPeriodUSec", "100000"))
            quota = properties.get("CPUQuotaPerSecUSec")
            limits(
                self.root / relative.lstrip("/"),
                memory=properties.get("MemoryMax", "max"),
                swap=properties.get("MemorySwapMax", "max"),
                tasks=properties.get("TasksMax", "max"),
                cpu=f"{'max' if quota is None else int(quota) * period // 1000000} {period}",
            )
        if args[:3] == ("systemctl", "--user", "stop"):
            self.state["ActiveState"] = "inactive"
        return ProcessResult(
            tuple(args),
            0,
            "\n".join(f"{key}={value}" for key, value in self.state.items()),
        )


def owner_fixture(tmp_path, resources=ResourceSpec(1, 1024, 0, 1024)):
    machine = make_machine(resources=resources)
    process = Manager(machine, tmp_path)
    return machine, process, SystemdOwner(process, root=tmp_path)


def test_synthetic_slice_is_created_then_verified_from_kernel_files(tmp_path):
    machine, process, owner = owner_fixture(tmp_path)
    group = owner.ensure(machine)
    assert group.name == machine.resource_unit
    assert any(call[0] == "busctl" for call in process.calls)
    # A matching unit name/description cannot stand in for actual enforcement.
    (group / "memory.max").write_text("max")
    with pytest.raises(ValueError, match="not enforced"):
        owner.ensure(machine)


@pytest.mark.parametrize(
    "state",
    [
        dict(ActiveState="active"),
        dict(FragmentPath="/operator/custom.slice"),
        dict(DropInPaths="/operator/custom.slice.d/limits.conf"),
        dict(Transient="yes"),
    ],
)
def test_collision_never_reuses_or_stops_somebody_elses_slice(tmp_path, state):
    machine, process, owner = owner_fixture(tmp_path)
    process.state.update(state)
    for operation in (owner.ensure, owner.remove):
        with pytest.raises(ValueError, match="ownership"):
            operation(machine)
    assert all(call[0] == "systemctl" and call[2] == "show" for call in process.calls)


def test_owned_unit_in_wrong_user_manager_is_rejected(tmp_path):
    machine, process, owner = owner_fixture(tmp_path)
    owner.ensure(machine)
    process.state["ControlGroup"] = f"/system.slice/{machine.resource_unit}"
    with pytest.raises(ValueError, match="local user manager"):
        owner.verify(machine)


def test_dispatch_checks_budget_inside_scope_without_expanding_guest_arguments(
    tmp_path,
):
    machine, process, owner = owner_fixture(tmp_path)
    owner.ensure(machine)
    command = owner.command(
        machine, ("podman", "exec", "id", "$HOME", "${SECRET}", "two words")
    )
    assert "--expand-environment=no" in command
    assert f"--slice={machine.resource_unit}" in command
    assert command[-6:] == ("podman", "exec", "id", "$HOME", "${SECRET}", "two words")
    assert str(Path(__file__).parents[1] / "mimchine/scope.py") in command


def test_owned_resource_unit_removal_is_confirmed(tmp_path):
    machine, process, owner = owner_fixture(tmp_path)
    owner.ensure(machine)
    owner.remove(machine)
    assert process.state["ActiveState"] == "inactive"


def test_parent_recreation_race_is_checked_inside_dispatch_scope(tmp_path, monkeypatch):
    from mimchine import scope

    group = tmp_path / "mimowner.slice" / "mimcall.scope"
    group.mkdir(parents=True)
    limits(group.parent, memory="max")
    monkeypatch.setattr(scope, "cgroup_path", lambda pid: group)
    monkeypatch.setattr(
        scope.sys,
        "argv",
        [
            "scope.py",
            "mimowner.slice",
            *map(str, identity(group.parent)),
            json.dumps(ResourceSpec(1, 1024).to_data()),
            "podman",
            "start",
            "id",
        ],
    )
    invoked = []
    monkeypatch.setattr(scope.os, "execvp", lambda *args: invoked.append(args))
    with pytest.raises(ValueError, match="not enforced"):
        scope.main()
    assert not invoked


@pytest.mark.parametrize(
    "resources,files",
    [
        (ResourceSpec(), {}),
        (ResourceSpec(cpus=2), {"cpu.max": "200000 100000"}),
        (ResourceSpec(memory_mib=32), {"memory.max": "33554432"}),
        (ResourceSpec(swap_mib=0), {"memory.swap.max": "0"}),
        (ResourceSpec(swap_mib=16), {"memory.swap.max": "16777216"}),
        (ResourceSpec(tasks=64), {"pids.max": "64"}),
    ],
)
def test_limits_are_independent_and_unused_controllers_are_not_required(
    tmp_path, resources, files
):
    for name, value in files.items():
        (tmp_path / name).write_text(value)
    check_limits(tmp_path, **resources.to_data())
    for name in files:
        (tmp_path / name).unlink()
        with pytest.raises(OSError):
            check_limits(tmp_path, **resources.to_data())
        (tmp_path / name).write_text(files[name])


@pytest.mark.parametrize(
    "resources",
    [
        ResourceSpec(),
        ResourceSpec(cpus=2),
        ResourceSpec(memory_mib=32),
        ResourceSpec(swap_mib=0),
        ResourceSpec(tasks=64),
    ],
)
def test_systemd_sets_only_requested_ceilings(tmp_path, resources):
    machine, process, owner = owner_fixture(tmp_path, resources)
    group = owner.ensure(machine)
    call = next(call for call in process.calls if call[0] == "busctl")
    actual = dict(zip(call[12::3], call[14::3]))
    expected = {"Description": f"mimchine {machine.id}"}
    if resources.cpus is not None:
        expected.update(
            CPUQuotaPerSecUSec=str(resources.cpus * 1000000),
            CPUQuotaPeriodUSec="100000",
        )
    if resources.memory_mib is not None:
        expected["MemoryMax"] = str(resources.memory_mib * 1024 * 1024)
    if resources.swap_mib is not None:
        expected["MemorySwapMax"] = str(resources.swap_mib * 1024 * 1024)
    if resources.tasks is not None:
        expected["TasksMax"] = str(resources.tasks)
    assert actual == expected
    assert owner.verify(machine) == group


@pytest.mark.parametrize("replaced", [False, True])
def test_unlimited_dispatch_checks_cgroup_identity_and_preserves_argv(
    tmp_path, monkeypatch, replaced
):
    from mimchine import scope

    machine, process, owner = owner_fixture(tmp_path, ResourceSpec())
    group = owner.ensure(machine)
    command = ("podman", "exec", "id", "$HOME", "two words")
    wrapped = owner.command(machine, command)
    if replaced:
        group.rename(group.with_name("previous.slice"))
        group.mkdir()
    invocation = group / "mimcall.scope"
    invocation.mkdir()
    monkeypatch.setattr(scope, "cgroup_path", lambda pid: invocation)
    script = wrapped.index(str(Path(scope.__file__)))
    monkeypatch.setattr(scope.sys, "argv", list(wrapped[script:]))
    invoked = []
    monkeypatch.setattr(scope.os, "execvp", lambda *args: invoked.append(args))
    if replaced:
        with pytest.raises(ValueError, match="replaced before dispatch"):
            scope.main()
        assert not invoked
    else:
        scope.main()
        assert invoked == [(command[0], list(command))]


def test_owner_replacement_during_verification_blocks_unlimited_dispatch(
    tmp_path, monkeypatch
):
    from mimchine import scope

    group = tmp_path / "mimowner.slice"
    group.mkdir()
    monkeypatch.setattr(scope, "cgroup_path", lambda pid: group / "mimcall.scope")
    monkeypatch.setattr(
        scope.sys,
        "argv",
        ["scope.py", group.name, *map(str, identity(group)), "{}", "podman"],
    )

    def replace_owner(*args, **kwargs):
        group.rename(tmp_path / "previous.slice")
        group.mkdir()

    monkeypatch.setattr(scope, "check_limits", replace_owner)
    monkeypatch.setattr(
        scope.os, "execvp", lambda *args: pytest.fail("unverified command dispatched")
    )
    with pytest.raises(ValueError, match="changed during verification"):
        scope.main()


def test_unlimited_owner_verification_requires_a_real_cgroup(tmp_path):
    machine, process, owner = owner_fixture(tmp_path, ResourceSpec())
    group = owner.ensure(machine)
    for file in group.iterdir():
        file.unlink()
    group.rmdir()
    with pytest.raises(ValueError, match="cgroup is absent"):
        owner.verify(machine)


def test_unlimited_owner_replacement_before_identity_capture_is_refused(
    tmp_path, monkeypatch
):
    machine, process, owner = owner_fixture(tmp_path, ResourceSpec())
    owner.ensure(machine)
    run = process.run
    queries = 0

    def replaced(args, **kwargs):
        nonlocal queries
        queries += 1
        if queries == 2:
            process.state["Description"] = "Recreated synthetic slice"
        return run(args, **kwargs)

    monkeypatch.setattr(process, "run", replaced)
    with pytest.raises(ValueError, match="ownership does not match"):
        owner.command(machine, ("podman",))
