from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from support import RecordingProcessRunner, make_machine

from mimchine.domain import ExecSpec, IdentityMode
from mimchine.engines.containers import PodmanEngine
from mimchine.job_limits import HELPER_PATH, SETUP_SCRIPT, require_host
from mimchine.process import ProcessError, ProcessResult


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
    return make_machine(image="app:dev", identity=IdentityMode.ROOT, **kwargs)


def test_creation_delegates_only_private_cgroups(monkeypatch, tmp_path):
    monkeypatch.setattr("mimchine.engines.containers.require_host", lambda info: None)
    monkeypatch.setattr(
        "mimchine.engines.containers.helper_mount_source", lambda: tmp_path / "helper"
    )
    process = RecordingProcessRunner(stdout="{}")
    PodmanEngine(process).allocate(record(job_limits=True))
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
    runner = PodmanEngine(process)
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


def test_failed_setup_blocks_dispatch_and_is_retried_for_a_running_machine():
    process = RecordingProcessRunner()
    engine = PodmanEngine(process)
    machine = record(job_limits=True)
    engine.start(machine)
    run = process.run
    fail_setup = True

    def prepare(args, **kwargs):
        result = run(args, **kwargs)
        if SETUP_SCRIPT in args and fail_setup:
            raise ProcessError(
                ProcessResult(tuple(args), 1, stderr="delegation failed")
            )
        return result

    process.run = prepare
    command = ExecSpec(("printf", "payload"), memory_mib=64)
    with pytest.raises(ProcessError):
        engine.exec(machine, command)
    assert not any("payload" in call for call in process.calls)
    fail_setup = False
    engine.exec(machine, command)
    assert process.calls[-1][-2:] == ("printf", "payload")
    assert sum(SETUP_SCRIPT in call for call in process.calls) == 2


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
