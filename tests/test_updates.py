from dataclasses import replace
import json

import pytest
from typer.testing import CliRunner
from support import UpdateFakeEngine, make_machine

from mimchine import cli
from mimchine.config import AppConfig, Defaults
from mimchine.domain import (
    MachineSpec,
    MountSpec,
    Operation,
    PortBind,
    ResourceSpec,
    UpdateOptions,
)
from mimchine.services import CreateOptions, MachineService
from mimchine.shell_state import ShellStateManager
from mimchine.state import MachineStore
from mimchine.updates import plan_update


def service_at(tmp_path):
    engine = UpdateFakeEngine()
    service = MachineService(
        AppConfig(Defaults(), {}),
        MachineStore(tmp_path / "machines"),
        ShellStateManager(tmp_path / "history"),
        {"podman": engine},
    )
    return service, engine


def test_plan_redacts_environment_and_preserves_omitted_values(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    before = MachineSpec(
        "dev",
        "image",
        "podman",
        env=("SECRET=old-private",),
        resources=ResourceSpec(cpus=3),
        mounts=(MountSpec(source, "/data"),),
    )
    plan = plan_update(
        before,
        UpdateOptions(env=("SECRET=new-private",), mounts=(f"{source}:/data:ro",)),
    )
    assert plan.after.resources.cpus == 3
    assert plan.after.mounts[0].read_only
    assert plan.effect == "replace"
    assert "private" not in json.dumps(plan.to_data())
    assert (
        plan_update(
            plan.after,
            UpdateOptions(env=("SECRET=new-private",), mounts=(f"{source}:/data:ro",)),
        ).effect
        == "none"
    )


def test_port_set_and_exact_remove_do_not_remove_other_listeners():
    spec = MachineSpec(
        "dev",
        "image",
        "podman",
        ports=(PortBind(8080, 80, "127.0.0.1"), PortBind(9090, 80, "127.0.0.1")),
    )
    changed = plan_update(spec, UpdateOptions(ports=("8080:81",))).after
    assert changed.ports[0].guest == 81
    removed = plan_update(changed, UpdateOptions(remove_ports=("8080:80",))).after
    assert removed == changed
    removed = plan_update(changed, UpdateOptions(remove_ports=("8080:81",))).after
    assert removed.ports == (PortBind(9090, 80, "127.0.0.1"),)


@pytest.mark.parametrize(
    "options",
    [
        UpdateOptions(ports=("0.0.0.0:8080:80", "127.0.0.1:8080:80")),
        UpdateOptions(ports=("8080:80", "8080:81")),
        UpdateOptions(env=("X=1",), unset_env=("X",)),
        UpdateOptions(resources=(("memory_mib", 0),)),
    ],
)
def test_invalid_patch_rejected(options):
    with pytest.raises(ValueError):
        plan_update(MachineSpec("dev", "image", "podman"), options)


def test_managed_mount_and_workspace_semantics(tmp_path):
    spec = MachineSpec(
        "dev",
        "image",
        "podman",
        mounts=(
            MountSpec(tmp_path, "/work", kind="workspace"),
            MountSpec(tmp_path, "/mim/shell-state", kind="shell_state"),
        ),
    )
    plan = plan_update(spec, UpdateOptions(mounts=(f"{tmp_path}:/work:ro",)))
    assert plan.after.mounts[0].kind == "workspace"
    with pytest.raises(ValueError, match="managed"):
        plan_update(spec, UpdateOptions(remove_mounts=("/mim/shell-state",)))
    with pytest.raises(ValueError, match="conflicting"):
        plan_update(
            spec,
            UpdateOptions(mounts=(f"{tmp_path}:/work",), remove_mounts=("/work/",)),
        )


def test_plan_is_offline_and_metadata_update_never_calls_engine(tmp_path):
    service, engine = service_at(tmp_path)
    before = service.create(CreateOptions("dev", image="image"))
    engine.calls.clear()
    engine.inspected.clear()
    original = service.store.read("dev")
    service.plan_update("dev", UpdateOptions(ports=("8080:80",)))
    assert service.store.read("dev") == original
    service.update("dev", UpdateOptions(shell="bash"))
    assert not engine.calls and not engine.inspected
    assert service.store.load("dev").target == before.target


def test_running_combined_update_refuses_before_any_mutation(tmp_path):
    service, engine = service_at(tmp_path)
    service.create(CreateOptions("dev", image="image", start=True))
    engine.calls.clear()
    original = service.store.read("dev")
    with pytest.raises(ValueError, match="--restart"):
        service.update(
            "dev", UpdateOptions(ports=("8080:80",), resources=(("cpus", 2),))
        )
    assert service.store.read("dev") == original
    assert engine.calls == []


@pytest.mark.parametrize("running", [True, False])
def test_replacement_retains_identity_and_running_intent(tmp_path, running):
    service, engine = service_at(tmp_path)
    old = service.create(CreateOptions("dev", image="image", start=running))
    service.update("dev", UpdateOptions(ports=("8080:80",)), restart=True)
    new = service.store.load("dev")
    assert new.id == old.id and new.created_at == old.created_at
    assert new.target != old.target
    assert new.spec.image == old.spec.image
    assert len(engine.objects) == 1 and len(engine.images) == 1
    assert service.inspect("dev").state.value == ("running" if running else "stopped")
    service.update("dev", UpdateOptions(remove_ports=("8080:80",)), restart=True)
    assert len(engine.objects) == len(engine.images) == 1
    service.delete("dev")
    assert not engine.objects and not engine.images


@pytest.mark.parametrize(
    "failure",
    [
        "stop",
        "snapshot",
        "snapshot_reply",
        "allocate",
        "allocated_reply",
        "verify",
        "start",
        "delete",
        "deleted_reply",
        "image_cleanup",
    ],
)
def test_interrupted_replacement_recovers_without_extra_allocations(tmp_path, failure):
    service, engine = service_at(tmp_path)
    service.create(CreateOptions("dev", image="image", start=True))
    engine.fail = failure
    with pytest.raises(RuntimeError, match="injected"):
        service.update("dev", UpdateOptions(ports=("8080:80",)), restart=True)
    journal = service.store.load("dev")
    assert journal.operation is Operation.UPDATING and journal.error
    with pytest.raises(ValueError, match="recover"):
        service.start("dev")
    service.recover("dev")
    current = service.store.load("dev")
    assert current.operation is Operation.READY
    assert len(engine.objects) == len(engine.images) == 1
    assert service.inspect("dev").state.value == "running"
    assert current.spec.ports == (PortBind(8080, 80, "127.0.0.1"),)


def test_lost_candidate_reply_does_not_adopt_reused_name(tmp_path):
    service, engine = service_at(tmp_path)
    service.create(CreateOptions("dev", image="image"))
    engine.fail = "allocated_reply"
    with pytest.raises(RuntimeError):
        service.update("dev", UpdateOptions(ports=("8080:80",)))
    pending = service.store.load("dev").update
    cid = engine.names[pending.candidate.backend_name]
    engine.objects[cid] = replace(engine.objects[cid], labels=(("mimchine", "1"),))
    with pytest.raises(ValueError, match="ownership"):
        service.recover("dev")
    assert service.store.load("dev").update is not None


def test_resource_failure_is_journaled_and_recoverable(tmp_path):
    service, engine = service_at(tmp_path)
    before = service.create(CreateOptions("dev", image="image", start=True))
    engine.fail = "resources"
    with pytest.raises(RuntimeError):
        service.update("dev", UpdateOptions(resources=(("cpus", 2),)))
    service.recover("dev")
    after = service.store.load("dev")
    assert after.target == before.target
    assert after.spec.resources.cpus == 2


@pytest.mark.parametrize(
    "failure",
    [
        "stop",
        "snapshot",
        "snapshot_reply",
        "allocate",
        "allocated_reply",
        "verify",
        "start",
        "delete",
        "deleted_reply",
        "image_cleanup",
        "resources",
    ],
)
def test_delete_interrupted_update_cleans_all_owned_objects(tmp_path, failure):
    service, engine = service_at(tmp_path)
    service.create(CreateOptions("dev", image="image", start=True))
    # Also exercise removal of a snapshot from a previous successful update.
    service.update("dev", UpdateOptions(env=("OLD=1",)), restart=True)
    engine.fail = failure
    options = (
        UpdateOptions(resources=(("cpus", 2),))
        if failure == "resources"
        else UpdateOptions(ports=("8080:80",))
    )
    with pytest.raises(RuntimeError):
        service.update("dev", options, restart=True)
    service.delete("dev")
    assert not engine.objects and not engine.images
    assert not service.store.exists("dev")


@pytest.mark.parametrize("failure", ["delete", "deleted_reply", "image_cleanup"])
def test_interrupted_update_deletion_is_retryable(tmp_path, failure):
    service, engine = service_at(tmp_path)
    service.create(CreateOptions("dev", image="image", start=True))
    engine.fail = "start"
    with pytest.raises(RuntimeError):
        service.update("dev", UpdateOptions(ports=("8080:80",)), restart=True)
    engine.fail = failure
    with pytest.raises(RuntimeError):
        service.delete("dev")
    assert service.store.load("dev").operation is Operation.DELETING
    with pytest.raises(ValueError, match="retry mim delete"):
        service.recover("dev")
    service.delete("dev")
    assert not engine.objects and not engine.images


def test_delete_lost_allocation_reply_checks_ownership(tmp_path):
    service, engine = service_at(tmp_path)
    old = service.create(CreateOptions("dev", image="image"))
    engine.fail = "allocated_reply"
    with pytest.raises(RuntimeError):
        service.update("dev", UpdateOptions(ports=("8080:80",)))
    candidate = service.store.load("dev").update.candidate
    cid = engine.names[candidate.backend_name]
    engine.objects[cid] = replace(engine.objects[cid], labels=(("mimchine", "1"),))
    with pytest.raises(ValueError, match="ownership"):
        service.delete("dev")
    assert cid in engine.objects and old.target.container_id in engine.objects
    assert service.store.load("dev").operation is Operation.DELETING


@pytest.mark.parametrize("runner", ["podman", "docker"])
@pytest.mark.parametrize("ownership", ["owned", "foreign", "changed_context"])
def test_snapshot_cleanup_checks_context_and_ownership(runner, ownership):
    from uuid import uuid4
    from support import IMAGE_ID, RecordingProcessRunner, context
    from mimchine.engines import get_engine

    token = str(uuid4())
    process = RecordingProcessRunner(engine=runner)
    process.inspect_stdout = json.dumps(
        [
            {
                "Id": IMAGE_ID,
                "Config": {
                    "Labels": {
                        "mimchine.snapshot": token
                        if ownership != "foreign"
                        else str(uuid4())
                    }
                },
            }
        ]
    )
    engine = get_engine(runner, process)
    machine = make_machine(engine=runner)
    if ownership == "changed_context":
        target = replace(
            machine.target,
            context=context("docker" if runner == "podman" else "podman"),
        )
        # Simulate a different daemon/store without modifying the saved binding.
        engine.context = lambda: target.context
    if ownership == "owned":
        engine.remove_rootfs(machine, token=token)
        assert process.calls[-1] == (*engine.prefix, "image", "rm", IMAGE_ID)
    else:
        with pytest.raises(ValueError, match="context|ownership"):
            engine.remove_rootfs(machine, token=token)
        assert not any("rm" in call for call in process.calls)
        if ownership == "changed_context":
            assert not process.calls


def test_schema_four_read_does_not_write(tmp_path):
    store = MachineStore(tmp_path / "machines")
    data = make_machine().to_data()
    data["schema_version"] = 4
    for key in ("generation", "rootfs", "update"):
        data.pop(key)
    path = store.record_path("dev")
    path.parent.mkdir(parents=True)
    original = json.dumps(data).encode()
    path.write_bytes(original)
    assert store.load("dev").generation is None
    assert path.read_bytes() == original


def test_update_cli_uses_same_options_and_unlimited(monkeypatch):
    calls = []

    class Service:
        def update(self, name, options, *, restart):
            calls.append((name, options, restart))
            return type("Result", (), {"effect": "resources"})()

    monkeypatch.setattr(cli.MachineService, "default", Service)
    result = CliRunner().invoke(
        cli.app,
        ["update", "dev", "--port", "8080:80", "--memory", "unlimited", "--restart"],
    )
    assert result.exit_code == 0, result.output
    assert calls[0][1].resources == (("memory_mib", None),)
    assert calls[0][1].ports == ("8080:80",)
    assert calls[0][2]


def test_clearing_cpu_or_memory_requires_explicit_replacement():
    spec = MachineSpec(
        "dev", "image", "podman", resources=ResourceSpec(cpus=2, memory_mib=256)
    )
    assert (
        plan_update(spec, UpdateOptions(resources=(("memory_mib", None),))).effect
        == "replace"
    )
    assert (
        plan_update(spec, UpdateOptions(resources=(("cpus", None),))).effect
        == "replace"
    )
    assert (
        plan_update(spec, UpdateOptions(resources=(("memory_mib", 512),))).effect
        == "resources"
    )


def test_legacy_port_can_be_replaced_with_an_explicit_listener():
    spec = MachineSpec("dev", "image", "podman", ports=(PortBind(8080, 80),))
    result = plan_update(
        spec, UpdateOptions(remove_ports=("8080:80",), ports=("127.0.0.1:8080:80",))
    )
    assert result.after.ports == (PortBind(8080, 80, "127.0.0.1"),)


@pytest.mark.parametrize("stage", ["stopped", "snapshot", "allocated", "committed"])
def test_recover_after_journal_save_failure(tmp_path, monkeypatch, stage):
    service, engine = service_at(tmp_path)
    service.create(CreateOptions("dev", image="image", start=True))
    save = service.store.save
    armed = True

    def fail_once(machine):
        nonlocal armed
        if armed and machine.update and machine.update.stage == stage:
            armed = False
            raise OSError("synthetic state write failure")
        return save(machine)

    monkeypatch.setattr(service.store, "save", fail_once)
    with pytest.raises(OSError):
        service.update("dev", UpdateOptions(ports=("8080:80",)), restart=True)
    service.recover("dev")
    assert len(engine.objects) == len(engine.images) == 1
    assert service.store.load("dev").operation is Operation.READY


def test_creation_overrides_profile_by_mount_listener_and_variable(tmp_path):
    service, engine = service_at(tmp_path)
    service.config = AppConfig(
        Defaults(),
        {
            "dev": {
                "image": "image",
                "workspace": f"{tmp_path}:/work",
                "port": "8080:80",
                "env": ["MODE=profile"],
            }
        },
    )
    machine = service.create(
        CreateOptions(
            "dev",
            profile="dev",
            mounts=(f"{tmp_path}:/work:ro",),
            ports=("8080:81",),
            env=("MODE=cli",),
        )
    )
    assert machine.spec.mounts[0].kind == "workspace"
    assert machine.spec.mounts[0].read_only
    assert machine.spec.ports == (PortBind(8080, 81, "127.0.0.1"),)
    assert machine.spec.env == ("MODE=cli",)


def test_never_started_candidate_reset_is_journaled(tmp_path):
    service, engine = service_at(tmp_path)
    service.create(CreateOptions("dev", image="image", start=True))
    engine.fail = "start"
    with pytest.raises(RuntimeError):
        service.update("dev", UpdateOptions(ports=("8080:80",)), restart=True)
    failed = service.store.load("dev").target
    engine.fail = "delete"
    with pytest.raises(RuntimeError):
        service.recover("dev")
    assert service.store.load("dev").update.stage == "retrying"
    service.recover("dev")
    assert service.store.load("dev").target != failed
    assert len(engine.objects) == len(engine.images) == 1


def test_recovery_never_discards_candidate_that_has_run(tmp_path):
    service, engine = service_at(tmp_path)
    service.create(CreateOptions("dev", image="image", start=True))
    engine.fail = "delete"
    with pytest.raises(RuntimeError):
        service.update("dev", UpdateOptions(ports=("8080:80",)), restart=True)
    failed = service.store.load("dev")
    engine.stop(failed.update.candidate)
    service.recover("dev")
    assert service.store.load("dev").target == failed.target


def test_gvisor_updates_fail_before_touching_engine(tmp_path):
    from test_gvisor_execution import pending
    from support import CID, IMAGE_ID, context
    from mimchine.domain import ContainerAllocation, IdentityMode, MachineBinding

    service, engine = service_at(tmp_path)
    machine = pending(tmp_path, identity=IdentityMode.ROOT)
    allocation = ContainerAllocation(CID, context(), machine.labels)
    machine = replace(
        machine,
        operation=Operation.READY,
        creation_context=None,
        binding=MachineBinding(allocation, IMAGE_ID, str(machine.gvisor.entrypoint)),
    )
    service.store.save(machine)
    original = service.store.read(machine.name)
    with pytest.raises(ValueError, match="only shell"):
        service.update(machine.name, UpdateOptions(resources=(("cpus", 2),)))
    assert service.store.read(machine.name) == original
    assert not engine.calls and not engine.inspected
