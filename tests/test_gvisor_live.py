"""Bounded opt-in integration; the image needs /bin/sh, core tools and /var/tmp.

MIM_GVISOR_LIVE=1 MIM_GVISOR_RUNTIME=/path/to/runsc \
MIM_GVISOR_IMAGE=sha256:... uv run pytest -q tests/test_gvisor_live.py
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from mimchine.config import AppConfig, Defaults, GvisorRuntime
from mimchine.domain import (
    ExecSpec,
    IdentityMode,
    IsolationMode,
    Machine,
    MachineSpec,
    NetworkMode,
    ResourceSpec,
    RuntimeState,
)
from mimchine.process import ProcessError, ProcessResult, ProcessRunner
from mimchine.resources import SystemdOwner
from mimchine.scope import check_limits
from mimchine.services import CreateOptions, MachineService
from mimchine.shell_state import ShellStateManager
from mimchine.state import MachineStore, atomic_write

pytestmark = [
    pytest.mark.live_gvisor,
    pytest.mark.skipif(
        os.environ.get("MIM_GVISOR_LIVE") != "1",
        reason="live gVisor integration requires explicit opt-in",
    ),
]


def test_rootless_gvisor_lifecycle_and_boundary_failures(tmp_path, monkeypatch):
    runtime = Path(os.environ["MIM_GVISOR_RUNTIME"]).resolve(strict=True)
    image = os.environ["MIM_GVISOR_IMAGE"]
    if not re.fullmatch(r"(sha256:)?[a-f0-9]{64}", image):
        pytest.fail("MIM_GVISOR_IMAGE must be an existing immutable image ID")
    name = "mim-test-" + uuid4().hex
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    (readonly / "source").write_text("readonly")
    service = MachineService(
        AppConfig(Defaults(), {}, GvisorRuntime(runtime)),
        MachineStore(tmp_path / "state/machines"),
        ShellStateManager(tmp_path / "state/shell-state"),
    )
    engine = service._engine("podman")
    service.engines["podman"] = engine
    context = engine.context()
    ambient = tmp_path / "containers.conf"
    ambient.write_text(
        "[containers]\nprivileged = true\ninit = true\nread_only = true\n"
        'env = ["MIM_AMBIENT=must-not-leak"]\n'
        f'mounts = ["type=bind,source={readonly},destination=/implicit", {{append=true}}]\n'
    )
    monkeypatch.setenv("CONTAINERS_CONF", str(ambient))
    assert engine.context() == context
    rootfs_file = "/var/tmp/" + name

    def execute(script):
        service.exec(name, ExecSpec(("/bin/sh", "-ec", script)))

    try:
        machine = service.create(
            CreateOptions(
                name,
                image=image,
                isolation=IsolationMode.GVISOR,
                network=NetworkMode.NONE,
                cpus=1,
                memory_mib=1024,
                swap_mib=0,
                tasks=1024,
                workspaces=(f"{workspace}:/work",),
                mounts=(f"{readonly}:/readonly:ro",),
                start=True,
            )
        )
        owner = SystemdOwner(ProcessRunner())
        group = owner.verify(machine)
        # Exercise the read-only normalization of an existing machine record.
        previous = machine.to_data()
        previous["schema_version"] = 3
        previous["spec"]["resources"].pop("swap_mib")
        previous["spec"]["resources"].pop("tasks")
        atomic_write(service.store.record_path(name), json.dumps(previous).encode())
        assert service.store.load(name) == machine
        execute(
            f"""
test -z "${{MIM_AMBIENT+x}}"
test ! -e /implicit
test "$(cat /readonly/source)" = readonly
if touch /readonly/forbidden; then exit 1; fi
printf guest > /work/shared
printf persistent > {rootfs_file}
"""
        )
        assert (workspace / "shared").read_text() == "guest"
        assert not (readonly / "forbidden").exists()
        (workspace / "host-edit").write_text("updated")
        execute('test "$(cat /work/host-edit)" = updated')

        # Tighten a finite limit; verify actual kernel state gates dispatch.
        quota = group / "cpu.max"
        original = quota.read_text()
        try:
            quota.write_text("50000 100000")
            with pytest.raises(ValueError, match="CPU budget"):
                execute("touch /work/should-not-run")
            assert not (workspace / "should-not-run").exists()
        finally:
            quota.write_text(original)

        # Preserve the pathname while replacing the authorized source inode.
        previous = tmp_path / "previous"
        workspace.rename(previous)
        workspace.mkdir()
        try:
            with pytest.raises(ValueError, match="Gofer mount differs"):
                execute("touch /work/should-not-run")
            assert not (workspace / "should-not-run").exists()
            assert not (previous / "should-not-run").exists()
        finally:
            workspace.rmdir()
            previous.rename(workspace)

        assert service.stop(name) is RuntimeState.STOPPED
        assert service.start(name) is RuntimeState.RUNNING
        owner.verify(machine)
        execute(
            f'test "$(cat {rootfs_file})" = persistent; test "$(cat /work/shared)" = guest'
        )
        # Inspection can fail independently of the resource owner. Stop must
        # still quiesce that owner and retain the exact binding for a retry.
        pid = json.loads(engine.observe(machine.target.container_id).configuration)[
            "boundary"
        ]["pid"]

        def unavailable(target):
            raise ProcessError(ProcessResult(("podman",), 125, stderr="inspect failed"))

        with monkeypatch.context() as failure:
            failure.setattr(engine, "observe", unavailable)
            with pytest.raises(ProcessError, match="inspect failed"):
                service.stop(name)
        assert not Path(f"/proc/{pid}").exists()
        assert service.store.load(name).target == machine.target
    finally:
        if service.store.exists(name):
            service.delete(name)
    assert not service.store.exists(name)


@pytest.mark.parametrize(
    "resources",
    [
        ResourceSpec(),
        ResourceSpec(cpus=1),
        ResourceSpec(memory_mib=512),
        ResourceSpec(swap_mib=0),
        ResourceSpec(swap_mib=32),
        ResourceSpec(tasks=256),
        ResourceSpec(1, 512, 0, 256),
    ],
    ids=["inherit", "cpu", "memory", "no-swap", "swap", "tasks", "combined"],
)
def test_optional_resources_under_independent_outer_containment(
    tmp_path, monkeypatch, resources
):
    runtime = Path(os.environ["MIM_GVISOR_RUNTIME"]).resolve(strict=True)
    image = os.environ["MIM_GVISOR_IMAGE"]
    if not re.fullmatch(r"(sha256:)?[a-f0-9]{64}", image):
        pytest.fail("MIM_GVISOR_IMAGE must be an existing immutable image ID")
    service = MachineService(
        AppConfig(Defaults(), {}, GvisorRuntime(runtime)),
        MachineStore(tmp_path / "machines"),
        ShellStateManager(tmp_path / "shell-state"),
    )
    context = service._engine("podman").context()
    outer = Machine(
        str(uuid4()),
        MachineSpec(
            "mim-test-owner", image, "podman", resources=ResourceSpec(2, 1536, 0, 1024)
        ),
        "test",
        creation_context=context,
    )
    owner = SystemdOwner(ProcessRunner())
    outer_group = owner.ensure(outer)
    parent = outer.resource_unit.removesuffix(".slice")
    name = "mim-test-" + uuid4().hex
    ensure = SystemdOwner.ensure

    def ensure_contained(self, machine):
        group = ensure(self, machine)
        assert group.is_relative_to(outer_group)
        check_limits(outer_group, **outer.spec.resources.to_data())
        return group

    try:
        with monkeypatch.context() as contained:
            # systemd slice names define ancestry. Only these private test
            # machines use the uniquely owned, independently bounded parent.
            contained.setattr(
                Machine,
                "resource_unit",
                property(lambda machine: f"{parent}-mim{UUID(machine.id).hex}.slice"),
            )
            contained.setattr(SystemdOwner, "ensure", ensure_contained)
            try:
                machine = service.create(
                    CreateOptions(
                        name,
                        image=image,
                        isolation=IsolationMode.GVISOR,
                        identity=IdentityMode.ROOT,
                        network=NetworkMode.NONE,
                        shell_state=False,
                        cpus=resources.cpus,
                        memory_mib=resources.memory_mib,
                        swap_mib=resources.swap_mib,
                        tasks=resources.tasks,
                        start=True,
                    )
                )
                assert machine.spec.resources == resources
                group = owner.verify(machine)
                assert group.is_relative_to(outer_group)
                expected = {
                    "cpu.max": f"{resources.cpus * 100000} 100000"
                    if resources.cpus is not None
                    else "max 100000",
                    "memory.max": str(resources.memory_mib * 1024 * 1024)
                    if resources.memory_mib is not None
                    else "max",
                    "memory.swap.max": str(resources.swap_mib * 1024 * 1024)
                    if resources.swap_mib is not None
                    else "max",
                    "pids.max": str(resources.tasks)
                    if resources.tasks is not None
                    else "max",
                }
                assert {
                    key: (group / key).read_text().strip() for key in expected
                } == expected
                service.exec(
                    name, ExecSpec(("/bin/sh", "-ec", "test -r /etc/os-release"))
                )
                assert service.stop(name) is RuntimeState.STOPPED
                assert service.start(name) is RuntimeState.RUNNING
                owner.verify(machine)
                check_limits(outer_group, **outer.spec.resources.to_data())
            finally:
                if service.store.exists(name):
                    service.delete(name)
            assert not service.store.exists(name)
    finally:
        owner.remove(outer)
    assert not outer_group.exists()
