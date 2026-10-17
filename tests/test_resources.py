from __future__ import annotations

import os
from pathlib import Path

import pytest
from support import make_machine

from mimchine.domain import ResourceSpec
from mimchine.process import ProcessResult
from mimchine.resources import SystemdBudget
from mimchine.scope import HOST_TASK_LIMIT, cgroup_path, check_limits


def limits(
    group,
    *,
    memory="1073741824",
    swap="0",
    cpu="100000 100000",
    tasks=str(HOST_TASK_LIMIT),
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
        check_limits(tmp_path, 1, 1024)


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
            limits(self.root / relative.lstrip("/"))
        if args[:3] == ("systemctl", "--user", "stop"):
            self.state["ActiveState"] = "inactive"
        return ProcessResult(
            tuple(args),
            0,
            "\n".join(f"{key}={value}" for key, value in self.state.items()),
        )


def budget(tmp_path):
    machine = make_machine(resources=ResourceSpec(1, 1024))
    process = Manager(machine, tmp_path)
    return machine, process, SystemdBudget(process, root=tmp_path)


def test_synthetic_slice_is_created_then_verified_from_kernel_files(tmp_path):
    machine, process, owner = budget(tmp_path)
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
    machine, process, owner = budget(tmp_path)
    process.state.update(state)
    for operation in (owner.ensure, owner.remove):
        with pytest.raises(ValueError, match="ownership"):
            operation(machine)
    assert all(call[0] == "systemctl" and call[2] == "show" for call in process.calls)


def test_owned_unit_in_wrong_user_manager_is_rejected(tmp_path):
    machine, process, owner = budget(tmp_path)
    owner.ensure(machine)
    process.state["ControlGroup"] = f"/system.slice/{machine.resource_unit}"
    with pytest.raises(ValueError, match="local user manager"):
        owner.verify(machine)


def test_dispatch_checks_budget_inside_scope_without_expanding_guest_arguments(
    tmp_path,
):
    machine, process, owner = budget(tmp_path)
    command = owner.command(
        machine, ("podman", "exec", "id", "$HOME", "${SECRET}", "two words")
    )
    assert "--expand-environment=no" in command
    assert f"--slice={machine.resource_unit}" in command
    assert command[-6:] == ("podman", "exec", "id", "$HOME", "${SECRET}", "two words")
    assert str(Path(__file__).parents[1] / "mimchine/scope.py") in command


def test_owned_resource_unit_removal_is_confirmed(tmp_path):
    machine, process, owner = budget(tmp_path)
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
        ["scope.py", "mimowner.slice", "1", "1024", "podman", "start", "id"],
    )
    invoked = []
    monkeypatch.setattr(scope.os, "execvp", lambda *args: invoked.append(args))
    with pytest.raises(ValueError, match="not enforced"):
        scope.main()
    assert not invoked
