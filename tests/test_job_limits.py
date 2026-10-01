from __future__ import annotations

from pathlib import Path
import subprocess

import pytest
from mimchine.domain import ExecSpec, IdentityMode, MachineRecord
from mimchine.job_limits import HELPER_PATH, require_host
from mimchine.runners.containers import PodmanRunner
from test_builders_runners import RecordingProcessRunner


def host_info(**overrides):
    return {
        "host": {
            "cgroupVersion": "v2",
            "cgroupManager": "systemd",
            "ociRuntime": {"name": "crun"},
            **overrides,
        }
    }


@pytest.mark.parametrize(
    "override",
    [
        {"cgroupVersion": "v1"},
        {"cgroupManager": "cgroupfs"},
        {"ociRuntime": {"name": "runc"}},
    ],
)
def test_rejects_hosts_without_required_delegation(monkeypatch, override):
    monkeypatch.setattr("mimchine.job_limits.platform.system", lambda: "Linux")
    monkeypatch.setattr("mimchine.job_limits.platform.release", lambda: "test")
    with pytest.raises(ValueError, match="job limits require"):
        require_host(host_info(**override))


def record(**kwargs):
    return MachineRecord(
        "dev", "app:dev", "podman", "test", identity=IdentityMode.ROOT, **kwargs
    )


def test_creation_delegates_only_private_cgroups(monkeypatch, tmp_path):
    monkeypatch.setattr("mimchine.runners.containers.require_host", lambda info: None)
    monkeypatch.setattr(
        "mimchine.runners.containers.helper_mount_source", lambda: tmp_path / "helper"
    )
    process = RecordingProcessRunner(stdout="{}")
    PodmanRunner(process).create(record(job_limits=True))
    command = process.calls[-1]
    assert "--cgroupns=private" in command
    assert "--security-opt=unmask=/sys/fs/cgroup" in command
    assert "--annotation=run.oci.delegate-cgroup=payload" in command
    assert f"{tmp_path}/helper:{HELPER_PATH}:ro,z" in command
    assert not any(
        "privileged" in arg
        or "cap-add" in arg
        or "label=disable" in arg
        or "cgroupns=host" in arg
        for arg in command
    )


def test_memory_cap_requires_opt_in_and_keeps_arguments():
    process = RecordingProcessRunner()
    runner = PodmanRunner(process)
    spec = ExecSpec(("printf", "%s", "spaces; $literal"), memory_mib=64)
    with pytest.raises(ValueError, match="--job-limits"):
        runner.exec(record(), spec)
    assert not process.calls
    runner.exec(record(job_limits=True), spec)
    assert process.calls[-1][-7:] == (
        HELPER_PATH,
        "--memory",
        "64",
        "--",
        "printf",
        "%s",
        "spaces; $literal",
    )


@pytest.mark.parametrize(
    "memory", ["0", "-1", "bad", "1G", "8796093022208", "999999999999999999999"]
)
def test_helper_rejects_invalid_limits_before_launch(memory):
    helper = Path(__file__).parents[1] / "mimchine/scripts/mim-limit.sh"
    result = subprocess.run(
        ["sh", str(helper), "--memory", memory, "--", "sh", "-c", "echo launched"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 125
    assert "launched" not in result.stdout
