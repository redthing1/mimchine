"""Bounded opt-in integration; the image needs /bin/sh, core tools and /var/tmp.

MIM_GVISOR_LIVE=1 MIM_GVISOR_RUNTIME=/path/to/runsc \
MIM_GVISOR_IMAGE=sha256:... uv run pytest -q tests/test_gvisor_live.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from uuid import uuid4

import pytest

from mimchine.config import AppConfig, Defaults, GvisorRuntime
from mimchine.domain import ExecSpec, IsolationMode, NetworkMode, RuntimeState
from mimchine.process import ProcessRunner
from mimchine.resources import SystemdBudget
from mimchine.services import CreateOptions, MachineService
from mimchine.shell_state import ShellStateManager
from mimchine.state import MachineStore

pytestmark = [
    pytest.mark.live_gvisor,
    pytest.mark.skipif(
        os.environ.get("MIM_GVISOR_LIVE") != "1",
        reason="live gVisor integration requires explicit opt-in",
    ),
]


def test_rootless_gvisor_lifecycle_and_boundary_failures(tmp_path):
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
    rootfs_file = "/var/tmp/" + name

    def execute(script):
        service.exec(name, ExecSpec(("/bin/sh", "-c", script)))

    try:
        machine = service.create(
            CreateOptions(
                name,
                image=image,
                isolation=IsolationMode.GVISOR,
                network=NetworkMode.NONE,
                cpus=1,
                memory_mib=1024,
                workspaces=(f"{workspace}:/work",),
                mounts=(f"{readonly}:/readonly:ro",),
                start=True,
            )
        )
        budget = SystemdBudget(ProcessRunner())
        group = budget.verify(machine)
        execute(
            f'test "$(cat /readonly/source)" = readonly; ! touch /readonly/forbidden; printf guest > /work/shared; printf persistent > {rootfs_file}'
        )
        assert (workspace / "shared").read_text() == "guest"
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
        budget.verify(machine)
        execute(
            f'test "$(cat {rootfs_file})" = persistent; test "$(cat /work/shared)" = guest'
        )
    finally:
        if service.store.exists(name):
            service.delete(name)
    assert not service.store.exists(name)
