from __future__ import annotations

import json
from dataclasses import replace

import pytest
from support import CID, IMAGE_ID, RecordingProcessRunner, make_machine

from mimchine.domain import (
    ExecSpec,
    IdentityMode,
    MissingContainer,
    MountSpec,
    NetworkMode,
    PortBind,
    ResourceSpec,
)
from mimchine.engines.containers import DockerEngine, PodmanEngine
from mimchine.engines.lifecycle import KEEPALIVE_COMMAND, STARTUP_HOOK
from mimchine.process import ProcessError


def test_allocate_captures_id_and_ownership(tmp_path):
    process = RecordingProcessRunner()
    machine = make_machine(
        identity=IdentityMode.ROOT,
        network=NetworkMode.NONE,
        resources=ResourceSpec(cpus=2, memory_mib=1024),
        mounts=(MountSpec(tmp_path, "/work/dev"),),
        ports=(PortBind(8080, 80),),
    )
    allocated = PodmanEngine(process).allocate(machine)
    command = next(call for call in process.calls if "create" in call)
    assert allocated.container_id == CID
    assert dict(allocated.labels)["mimchine.id"] == machine.id
    assert command[:2] == ("podman", "--remote=false")
    assert command[command.index("--user") + 1] == "0:0"
    assert command[command.index("--memory") + 1] == "1024m"
    assert command[command.index("--cpus") + 1] == "2"
    assert "--pids-limit=-1" in command
    assert "--tz=local" in command
    assert f"{tmp_path}:/work/dev:rw" in command
    assert command[-6:] == (
        "--entrypoint",
        "/bin/sh",
        "--",
        "alpine",
        "-lc",
        KEEPALIVE_COMMAND,
    )
    assert STARTUP_HOOK in KEEPALIVE_COMMAND
    assert all(cwd == "/" for cwd in process.cwd)


def test_native_shell_history_relabeling_is_preserved(tmp_path):
    process = RecordingProcessRunner()
    machine = make_machine(
        identity=IdentityMode.ROOT,
        mounts=(MountSpec(tmp_path, "/mim/shell-state", kind="shell_state"),),
    )
    PodmanEngine(process).allocate(machine)
    assert f"{tmp_path}:/mim/shell-state:rw,Z" in process.calls[-1]


def test_native_gpu_and_extra_arguments_are_preserved(monkeypatch):
    monkeypatch.setattr(
        "mimchine.engines.containers.podman_gpu_args",
        lambda: ("--device", "/dev/dri/renderD128"),
    )
    process = RecordingProcessRunner()
    machine = make_machine(
        identity=IdentityMode.ROOT, gpu=True, container_args=("--cap-drop=all",)
    )
    PodmanEngine(process).allocate(machine)
    assert process.calls[-1][-9:-6] == (
        "--device",
        "/dev/dri/renderD128",
        "--cap-drop=all",
    )


def test_image_identity_mapping_is_retained_for_native_execution():
    process = RecordingProcessRunner(stdout="1001\n1002\n")
    PodmanEngine(process).allocate(make_machine())
    command = process.calls[-1]
    assert command[command.index("--userns") + 1] == "keep-id:uid=1001,gid=1002"
    assert any("run" in call and "none" in call for call in process.calls)


def test_bad_image_identity_does_not_allocate():
    process = RecordingProcessRunner(stdout="user\ngroup\n")
    with pytest.raises(ValueError, match="invalid uid/gid"):
        PodmanEngine(process).allocate(make_machine())
    assert not any("create" in call for call in process.calls)


@pytest.mark.parametrize(
    "engine_type, name", [(PodmanEngine, "podman"), (DockerEngine, "docker")]
)
def test_observation_captures_effective_identity(engine_type, name):
    process = RecordingProcessRunner(engine=name)
    observed = engine_type(process).observe(CID)
    assert observed.container_id == CID
    assert observed.image_id == IMAGE_ID
    assert observed.context.engine == name
    assert observed.runtime == ("crun" if name == "podman" else "runc")


@pytest.mark.parametrize("engine_type", [PodmanEngine, DockerEngine])
def test_engine_unavailable_is_not_reported_as_missing(engine_type):
    process = RecordingProcessRunner(returncode=127)
    with pytest.raises(ProcessError):
        engine_type(process).observe(CID)


def test_inspect_failure_for_existing_object_is_not_missing():
    process = RecordingProcessRunner()
    run = process.run

    def fail_inspect(args, **kwargs):
        if "inspect" in args:
            from mimchine.process import ProcessResult

            return ProcessResult(tuple(args), 125, stderr="storage inaccessible")
        return run(args, **kwargs)

    process.run = fail_inspect
    with pytest.raises(ProcessError, match="125"):
        PodmanEngine(process).observe(CID)
    process.exists = False
    assert isinstance(PodmanEngine(process).observe(CID), MissingContainer)


def test_every_lifecycle_command_uses_id_and_neutral_cwd():
    process = RecordingProcessRunner()
    engine = PodmanEngine(process)
    machine = make_machine()
    engine.start(machine)
    engine.exec(machine, ExecSpec(("pwd",)))
    engine.stop(machine)
    engine.delete(machine)
    for action in ("start", "exec", "stop", "rm"):
        commands = [call for call in process.calls if action in call]
        assert commands and all(CID in call and "dev" not in call for call in commands)
    assert all(cwd == "/" for cwd in process.cwd)
    assert not any("info" in call for call in process.calls)


def test_podman_context_uses_only_the_trusted_namespace_probe():
    process = RecordingProcessRunner()
    observed = PodmanEngine(process).context()
    assert dict(observed.identity)["storage"] == "/test/storage"
    assert len(process.calls) == 1
    command = process.calls[0]
    assert "unshare" in command and "-I" in command
    compile(command[-1], "<podman-context-probe>", "exec")
    assert not any("info" in call or "ls" in call for call in process.calls)


def test_payload_dispatch_rechecks_runtime():
    process = RecordingProcessRunner()
    process.inspect_stdout = json.dumps(
        [
            {
                "Id": CID,
                "Image": IMAGE_ID,
                "OCIRuntime": "runsc",
                "State": {"Running": True},
                "Config": {"Labels": dict(make_machine().target.labels)},
            }
        ]
    )
    with pytest.raises(ValueError, match="OCI runtime"):
        PodmanEngine(process).exec(make_machine(), ExecSpec(("true",)))
    assert not any("exec" in call for call in process.calls)


def test_docker_does_not_guess_an_unreported_runtime_from_daemon_defaults():
    process = RecordingProcessRunner(engine="docker")
    process.inspect_stdout = json.dumps(
        [
            {
                "Id": CID,
                "Image": IMAGE_ID,
                "HostConfig": {"Runtime": ""},
                "State": {"Running": False},
                "Config": {"Labels": {"mimchine": "1"}},
            }
        ]
    )
    with pytest.raises(ValueError, match="runtime"):
        DockerEngine(process).observe(CID)


def test_docker_name_probe_does_not_match_a_longer_name():
    process = RecordingProcessRunner(engine="docker")
    run = process.run

    def names(args, **kwargs):
        result = run(args, **kwargs)
        return replace(result, stdout="developer\n") if "ls" in args else result

    process.run = names
    assert isinstance(DockerEngine(process).observe("dev"), MissingContainer)


@pytest.mark.parametrize(
    "changed",
    [
        {"State": None},
        {"HostConfig": []},
        {"Config": None},
        {"Config": {"Labels": []}},
        {"Config": {"Labels": {"mimchine": 1}}},
    ],
)
def test_malformed_engine_objects_fail_with_a_diagnostic(changed):
    process = RecordingProcessRunner()
    item = {
        "Id": CID,
        "Image": IMAGE_ID,
        "OCIRuntime": "crun",
        "State": {"Running": False},
        "Config": {"Labels": {"mimchine": "1"}},
        **changed,
    }
    process.inspect_stdout = json.dumps([item])
    with pytest.raises(ValueError, match="container"):
        PodmanEngine(process).observe(CID)


@pytest.mark.parametrize(
    "extra",
    [
        ("--name=other",),
        ("--replace",),
        ("--label=mimchine.id=other",),
        ("-lother=value",),
    ],
)
def test_extra_arguments_cannot_override_ownership(extra):
    process = RecordingProcessRunner()
    with pytest.raises(ValueError, match="ownership"):
        PodmanEngine(process).allocate(make_machine(container_args=extra))
    assert not process.calls
