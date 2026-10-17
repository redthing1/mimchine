from __future__ import annotations

import json
from dataclasses import replace

import pytest
from support import CID, IMAGE_ID, MACHINE_ID, context, make_machine

from mimchine.config import AppConfig, Defaults, GvisorRuntime
from mimchine.domain import (
    ContainerAllocation,
    GvisorBinding,
    IdentityMode,
    ImageUser,
    IsolationMode,
    Machine,
    MachineObservation,
    MachineSpec,
    MissingContainer,
    MountSpec,
    Operation,
    ResourceSpec,
    RuntimeState,
)
from mimchine.engines.gvisor import GvisorSandbox
from mimchine.engines.lifecycle import KEEPALIVE_COMMAND
from mimchine.gvisor import GvisorRuntimeIdentity
from mimchine.process import ProcessRunner
from mimchine.services import CreateOptions, MachineService
from mimchine.shell_state import ShellStateManager
from mimchine.state import MachineStore

PROBE_ID = "c" * 64


def pending(tmp_path, **changes):
    spec = MachineSpec(
        "dev",
        "image",
        "podman",
        isolation=IsolationMode.GVISOR,
        resources=ResourceSpec(1, 1024),
        shell_state=False,
        **changes,
    )
    return Machine(
        MACHINE_ID,
        spec,
        "now",
        creation_context=context(),
        gvisor=GvisorBinding(
            tmp_path / "runtime",
            "0" * 64,
            "online",
            IMAGE_ID,
            tmp_path / "machines/dev/gvisor",
        ),
    )


def test_runtime_binding_roundtrip_keeps_identity_and_grants(tmp_path):
    machine = pending(tmp_path)
    machine = replace(
        machine, gvisor=replace(machine.gvisor, image_user=ImageUser(1234, 5678))
    )
    assert Machine.from_data(machine.to_data()) == machine
    with pytest.raises(ValueError, match="identity must be prepared"):
        replace(
            pending(tmp_path),
            operation=Operation.READY,
            creation_context=None,
            binding=replace(
                make_machine().binding, runtime=str(machine.gvisor.entrypoint)
            ),
        )


def test_native_record_needs_no_gvisor_migration():
    original = make_machine().to_data()
    original.pop("gvisor")
    original.pop("identity_probe")
    assert Machine.from_data(original) == make_machine()


def test_control_directory_must_stay_inside_its_machine_record(tmp_path):
    machine = pending(tmp_path)
    store = MachineStore(tmp_path / "machines")
    store.save(
        replace(
            machine, gvisor=replace(machine.gvisor, control_dir=tmp_path / "elsewhere")
        )
    )
    with pytest.raises(ValueError, match="control directory"):
        store.load("dev")


def observation(machine, *, probe=False):
    return MachineObservation(
        PROBE_ID if probe else CID,
        context(),
        IMAGE_ID,
        str(
            machine.gvisor.directory
            / "profiles"
            / ("offline" if probe else machine.gvisor.profile)
            / "runsc"
        ),
        machine.probe_labels if probe else machine.labels,
        RuntimeState.STOPPED,
        "{}",
    )


def effective(machine):
    return dict(
        process=dict(
            args=["/bin/sh", "-lc", KEEPALIVE_COMMAND],
            noNewPrivileges=True,
            user=dict(uid=0, gid=0),
            capabilities={},
        ),
        linux=dict(
            cgroupsPath=f"{machine.resource_unit}:libpod:{CID}",
            namespaces=[
                dict(type=name)
                for name in ("pid", "network", "ipc", "uts", "mount", "cgroup")
            ],
        ),
        mounts=[],
    )


@pytest.mark.parametrize(
    "attack",
    [
        "privileges",
        "hooks",
        "capability",
        "namespace",
        "device",
        "mount",
        "command",
        "parent",
        "identity",
    ],
)
def test_effective_oci_policy_blocks_unrequested_authority(tmp_path, attack):
    machine = pending(tmp_path, identity=IdentityMode.ROOT)
    sandbox = GvisorSandbox(ProcessRunner(), machine)
    spec = effective(machine)
    sandbox._verify_spec(spec, observation(machine))
    if attack == "privileges":
        spec["process"]["noNewPrivileges"] = False
    if attack == "hooks":
        spec["hooks"] = {"prestart": [{"path": "/host/program"}]}
    if attack == "capability":
        spec["process"]["capabilities"] = {"bounding": ["CAP_SYS_ADMIN"]}
    if attack == "namespace":
        spec["linux"]["namespaces"][0]["path"] = "/proc/1/ns/pid"
    if attack == "device":
        spec["linux"]["devices"] = [
            dict(path="/dev/kvm", type="c", major=10, minor=232)
        ]
    if attack == "mount":
        spec["mounts"] = [
            dict(destination="/host", type="bind", source="/home", options=["rw"])
        ]
    if attack == "command":
        spec["process"]["args"] = ["/image/entrypoint"]
    if attack == "parent":
        spec["linux"]["cgroupsPath"] = "user.slice:libpod:" + CID
    if attack == "identity":
        spec["process"]["user"]["uid"] = 42
    with pytest.raises(ValueError):
        sandbox._verify_spec(spec, observation(machine))


def test_bind_retargeting_is_rejected_and_source_inode_is_held(tmp_path):
    source = tmp_path / "workspace"
    source.mkdir()
    machine = pending(tmp_path, mounts=(MountSpec(source, "/work"),))
    sandbox = GvisorSandbox(ProcessRunner(), machine)
    with sandbox.grants() as grants:
        source.rename(tmp_path / "original")
        source.mkdir()
        actual = source.stat()
        assert grants["/work"] != (actual.st_dev, actual.st_ino)
    source.rmdir()
    source.symlink_to(tmp_path / "original", target_is_directory=True)
    with pytest.raises(ValueError, match="no longer canonical"):
        with sandbox.grants():
            pytest.fail("a retargeted bind was authorized")


def test_history_kind_cannot_grant_access_to_control_files(tmp_path):
    source = tmp_path / "machines"
    source.mkdir()
    machine = pending(
        tmp_path, mounts=(MountSpec(source, "/history", kind="shell_state"),)
    )
    with pytest.raises(ValueError, match="protected control path"):
        GvisorSandbox(ProcessRunner(), machine).validate_grants()


class GvisorEngine:
    name = "podman"

    def __init__(self):
        self.objects = {}
        self.calls = []
        self.probe_error = None
        self.allocate_error = None
        self.delete_error = None
        self.store = None

    def context(self):
        return context()

    def resolve_image(self, image):
        return IMAGE_ID

    def observe(self, target):
        return self.objects.get(target, MissingContainer(context()))

    def allocate_identity_probe(self, machine):
        self.calls.append(("probe-create", PROBE_ID))
        found = observation(machine, probe=True)
        self.objects[PROBE_ID] = self.objects[machine.probe_name] = found
        if self.allocate_error:
            raise self.allocate_error
        return ContainerAllocation(PROBE_ID, context(), machine.probe_labels)

    def initialize_identity_probe(self, machine):
        assert self.store.load(machine.name).identity_probe.container_id == PROBE_ID
        self.calls.append(("probe-init", PROBE_ID))
        if self.probe_error:
            raise self.probe_error
        return ImageUser(1234, 5678)

    def delete_identity_probe(self, machine):
        self.calls.append(("probe-delete", machine.identity_probe.container_id))
        if self.delete_error:
            raise self.delete_error
        self.objects.pop(PROBE_ID, None)
        self.objects.pop(machine.probe_name, None)

    def allocate(self, machine):
        self.calls.append(("create", CID))
        assert self.store.load(machine.name).gvisor.image_user == ImageUser(1234, 5678)
        assert machine.identity_probe is None
        self.objects[CID] = observation(machine)
        return ContainerAllocation(CID, context(), machine.labels)

    def delete(self, machine):
        self.calls.append(("delete", machine.target.container_id))
        self.objects.pop(machine.target.container_id, None)

    def release(self, machine):
        self.calls.append(("release", machine.resource_unit))


@pytest.fixture
def service(tmp_path, monkeypatch):
    engine = GvisorEngine()
    runtime = GvisorRuntimeIdentity(
        tmp_path / "runtime",
        "0" * 64,
        "20261012.0-mim.3",
        "a" * 40,
        "online",
        ("platform=systrap",),
    )
    monkeypatch.setattr(
        "mimchine.services.verify_runtime", lambda *args, **kwargs: runtime
    )
    config = AppConfig(Defaults(), {}, GvisorRuntime(tmp_path / "runtime/runsc"))
    svc = MachineService(
        config,
        MachineStore(tmp_path / "machines"),
        ShellStateManager(tmp_path / "shell-state"),
        {"podman": engine},
    )
    engine.store = svc.store
    return svc, engine


def create(service):
    svc, _ = service
    return svc.create(
        CreateOptions("dev", image="mutable-tag", isolation=IsolationMode.GVISOR)
    )


def test_image_identity_is_prepared_and_journaled_before_main_allocation(service):
    svc, engine = service
    machine = create(service)
    assert machine.operation is Operation.READY
    assert machine.gvisor.image_user == ImageUser(1234, 5678)
    assert machine.spec.resources == ResourceSpec(2, 4096)
    assert machine.gvisor.image_id == IMAGE_ID
    assert machine.identity_probe is None
    assert [action for action, _ in engine.calls] == [
        "probe-create",
        "probe-init",
        "probe-delete",
        "create",
    ]
    assert svc.store.load("dev") == machine


def test_failed_preparation_retains_exact_probe_id_for_deletion(service):
    svc, engine = service
    engine.probe_error = ValueError("missing controller")
    with pytest.raises(ValueError, match="missing controller"):
        create(service)
    machine = svc.store.load("dev")
    assert machine.identity_probe.container_id == PROBE_ID
    assert machine.target is None
    svc.delete("dev")
    assert not engine.objects
    assert not svc.store.exists("dev")


def test_crash_before_probe_id_is_saved_recovers_only_owned_reserved_name(service):
    svc, engine = service
    engine.allocate_error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        create(service)
    assert svc.store.load("dev").identity_probe is None
    svc.delete("dev")
    assert ("probe-delete", PROBE_ID) in engine.calls
    assert not engine.objects


def test_reused_probe_name_cannot_be_deleted(service):
    svc, engine = service
    engine.allocate_error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        create(service)
    machine = svc.store.load("dev")
    engine.objects[machine.probe_name] = replace(
        engine.objects[PROBE_ID],
        labels=(("mimchine", "1"), ("mimchine.id", "different")),
    )
    with pytest.raises(ValueError, match="ownership"):
        svc.delete("dev")
    assert not any(action == "probe-delete" for action, _ in engine.calls)
    assert svc.store.exists("dev")


def test_failed_probe_removal_remains_retryable_after_identity_is_known(service):
    svc, engine = service
    engine.delete_error = RuntimeError("temporary removal failure")
    with pytest.raises(RuntimeError):
        create(service)
    machine = svc.store.load("dev")
    assert machine.gvisor.image_user == ImageUser(1234, 5678)
    assert machine.identity_probe.container_id == PROBE_ID
    with pytest.raises(RuntimeError):
        svc.delete("dev")
    assert svc.store.load("dev").operation is Operation.DELETING
    engine.delete_error = None
    svc.delete("dev")
    assert not engine.objects
    assert not svc.store.exists("dev")


@pytest.mark.parametrize("privileged", [False, None])
def test_failed_initialization_proof_never_dispatches_start(
    tmp_path, monkeypatch, privileged
):
    from contextlib import nullcontext

    from mimchine.engines.containers import PodmanEngine

    machine = pending(tmp_path, identity=IdentityMode.ROOT)
    target = ContainerAllocation(CID, context(), machine.labels)
    machine = replace(
        machine,
        operation=Operation.READY,
        creation_context=None,
        binding=replace(
            make_machine().binding,
            allocation=target,
            runtime=str(machine.gvisor.entrypoint),
        ),
    )
    found = replace(
        observation(machine),
        configuration=json.dumps(
            {"boundary": dict(status="created", privileged=privileged, devices=[])}
        ),
    )
    engine = PodmanEngine(ProcessRunner())
    calls = []
    monkeypatch.setattr(engine, "context", context)
    monkeypatch.setattr(engine, "observe", lambda target: found)
    monkeypatch.setattr(GvisorSandbox, "prepare", lambda self: None)
    monkeypatch.setattr(GvisorSandbox, "grants", lambda self: nullcontext({}))
    monkeypatch.setattr(
        GvisorSandbox, "run", lambda self, args, **kwargs: calls.append(args)
    )

    def reject(self, *args):
        raise ValueError("effective mount differs")

    monkeypatch.setattr(GvisorSandbox, "verify", reject)
    message = (
        "effective mount differs" if privileged is False else "excludes privileged"
    )
    with pytest.raises(ValueError, match=message):
        engine.start(machine)
    assert calls == ([("init", CID)] if privileged is False else [])


@pytest.mark.parametrize(
    "mapping", ["0 0 4294967295", "0 999 1", "0 1000 2", "0 589823 2", "0 1000", ""]
)
def test_runtime_uid_mapping_cannot_escape_delegated_engine_identity(mapping):
    from mimchine.engines.gvisor import verify_uid_map

    with pytest.raises(ValueError, match="mapping|rootless engine identity"):
        verify_uid_map(mapping, "0 1000 1 1 524288 65536")


def test_restricted_sentry_namespace_is_within_delegated_ids():
    from mimchine.engines.gvisor import verify_uid_map

    verify_uid_map("65534 589821 1", "0 1000 1 1 524288 65536")


@pytest.mark.parametrize("failure", ["runtime", "cleanup", "drift"])
def test_failed_cleanup_quiesces_only_the_owned_budget(tmp_path, monkeypatch, failure):
    from mimchine.engines.containers import PodmanEngine
    from mimchine.process import ProcessError, ProcessResult
    from mimchine.resources import SystemdBudget

    machine = pending(tmp_path, identity=IdentityMode.ROOT)
    target = ContainerAllocation(CID, context(), machine.labels)
    machine = replace(machine, allocation=target)
    engine = PodmanEngine(ProcessRunner())
    found = observation(machine)
    if failure == "drift":
        found = replace(found, runtime="/untrusted/runtime")
    monkeypatch.setattr(engine, "observe", lambda target: found)

    def distribution(self):
        if failure == "runtime":
            raise ValueError("changed distribution")

    monkeypatch.setattr(GvisorSandbox, "distribution", distribution)
    monkeypatch.setattr(GvisorSandbox, "control_files", lambda self: None)
    commands = []

    def cleanup(self, args, **kwargs):
        commands.append((args, kwargs))
        raise ProcessError(ProcessResult(args, 125, stderr="cleanup failed"))

    monkeypatch.setattr(GvisorSandbox, "run", cleanup)
    removed = []
    monkeypatch.setattr(
        SystemdBudget, "remove", lambda self, machine: removed.append(machine.id)
    )
    with pytest.raises((ValueError, ProcessError)):
        engine._gvisor_cleanup(machine, ("rm", "-f", CID))
    assert removed == [machine.id]
    if failure == "cleanup":
        assert commands == [(("rm", "-f", CID), dict(scoped=False, capture=True))]
    else:
        assert not commands


def test_stop_of_missing_container_still_releases_its_owned_budget(service):
    svc, engine = service
    machine = create(service)
    engine.objects.clear()
    assert svc.stop("dev") is RuntimeState.MISSING
    assert ("release", machine.resource_unit) in engine.calls


@pytest.mark.parametrize("kind", ["effective", "permitted", "ambient", "inheritable"])
def test_nonroot_guest_cannot_receive_root_capabilities(tmp_path, kind):
    import os

    machine = pending(tmp_path, identity=IdentityMode.HOST)
    spec = effective(machine)
    spec["process"]["user"] = {"uid": os.getuid(), "gid": os.getgid()}
    spec["process"]["capabilities"] = {"bounding": ["CAP_DAC_OVERRIDE"]}
    sandbox = GvisorSandbox(ProcessRunner(), machine)
    sandbox._verify_spec(spec, observation(machine))
    spec["process"]["capabilities"][kind] = ["CAP_DAC_OVERRIDE"]
    with pytest.raises(ValueError, match="unexpected capabilities"):
        sandbox._verify_spec(spec, observation(machine))


def test_parent_retargeting_during_open_cannot_authorize_a_different_source(
    tmp_path, monkeypatch
):
    import os

    source = tmp_path / "workspace"
    source.mkdir()
    other = tmp_path / "control"
    other.mkdir()
    machine = pending(tmp_path, mounts=(MountSpec(source, "/work"),))
    original = os.open
    monkeypatch.setattr(
        "mimchine.engines.gvisor.os.open", lambda path, flags: original(other, flags)
    )
    with pytest.raises(ValueError, match="changed while opening"):
        with GvisorSandbox(ProcessRunner(), machine).grants():
            pytest.fail("a different opened source was authorized")
