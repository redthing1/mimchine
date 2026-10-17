from __future__ import annotations

import json
from dataclasses import replace

import pytest
from support import CID, IMAGE_ID, FakeEngine, configuration, context, make_machine

from mimchine.domain import (
    IsolationMode,
    MachineObservation,
    MountSpec,
    NetworkMode,
    Operation,
    PortBind,
    ResourceSpec,
    RuntimeState,
)
from mimchine.migration import MigrationPlan, MigrationService
from mimchine.state import MachineStore, MigrationRequiredError


@pytest.fixture
def adoption(tmp_path):
    store = MachineStore(tmp_path / "machines")
    machine = make_machine()
    spec = machine.spec
    data = spec.to_data()
    data.pop("engine")
    data.pop("isolation")
    data.update(schema_version=2, runner="podman", created_at=machine.created_at)
    source = (json.dumps(data, indent=2) + "\n").encode()
    path = store.record_path("dev")
    path.parent.mkdir(parents=True)
    path.write_bytes(source)
    engine = FakeEngine(state=RuntimeState.RUNNING)
    engine.observation = MachineObservation(
        CID,
        context(),
        IMAGE_ID,
        "crun",
        (("mimchine", "1"),),
        RuntimeState.RUNNING,
        configuration(spec),
    )
    return MigrationService(store, {"podman": engine}), engine, source


def test_plan_is_read_only_and_shows_exact_target(adoption):
    service, engine, source = adoption
    plan = service.plan("dev")
    assert service.store.read("dev") == source
    assert not (service.store.base_dir / ".locks").exists()
    assert plan.machine.target.container_id == CID
    assert plan.machine.spec.isolation is IsolationMode.NATIVE
    assert plan.observed_state is RuntimeState.RUNNING
    assert MigrationPlan.from_data(plan.to_data()) == plan
    assert engine.inspected == ["dev"]
    assert not engine.created and not engine.started and not engine.deleted


def test_adoption_preserves_container_and_saves_recoverable_backup(adoption):
    service, engine, source = adoption
    plan = service.plan("dev")
    adopted = service.apply("dev", plan)
    assert service.store.load("dev") == adopted
    assert adopted.operation is Operation.READY
    assert adopted.target.container_id == CID
    assert adopted.target.labels == (("mimchine", "1"),)
    assert (
        service.store.machine_dir("dev") / "machine.schema-2.json"
    ).read_bytes() == source
    assert engine.state is RuntimeState.RUNNING
    assert engine.inspected == ["dev", CID]
    assert not engine.created and not engine.started and not engine.deleted
    assert service.apply("dev", plan) == adopted


def test_changed_record_invalidates_reviewed_plan(adoption):
    service, engine, source = adoption
    plan = service.plan("dev")
    service.store.record_path("dev").write_bytes(source + b"\n")
    with pytest.raises(ValueError, match="record changed"):
        service.apply("dev", plan)
    assert not engine.created and not engine.deleted


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("container_id", "c" * 64, "container ID"),
        ("image_id", "sha256:" + "c" * 64, "container image"),
        ("runtime", "runsc", "OCI runtime"),
        ("labels", (("mimchine", "other"),), "ownership"),
        (
            "context",
            replace(context(), identity=(("host", "another-host"),)),
            "engine context",
        ),
        ("configuration", '{"changed": true}', "configuration changed"),
    ],
)
def test_target_drift_invalidates_reviewed_plan(adoption, field, value, message):
    service, engine, source = adoption
    plan = service.plan("dev")
    engine.observation = replace(engine.observation, **{field: value})
    with pytest.raises(ValueError, match=message):
        service.apply("dev", plan)
    assert service.store.read("dev") == source


def test_missing_target_does_not_trigger_name_lookup_on_apply(adoption):
    service, engine, source = adoption
    plan = service.plan("dev")
    engine.state = RuntimeState.MISSING
    with pytest.raises(ValueError, match="planned container is missing"):
        service.apply("dev", plan)
    assert engine.inspected == ["dev", CID]
    assert service.store.read("dev") == source


def test_unowned_named_container_cannot_be_adopted(adoption):
    service, engine, source = adoption
    engine.observation = replace(engine.observation, labels=())
    with pytest.raises(ValueError, match="ownership label"):
        service.plan("dev")
    assert service.store.read("dev") == source


def test_changed_intent_in_plan_is_rejected(adoption):
    service, _, source = adoption
    plan = service.plan("dev")
    changed = replace(plan.machine, spec=replace(plan.machine.spec, image="different"))
    with pytest.raises(ValueError, match="preserve the original intent"):
        service.apply("dev", replace(plan, machine=changed))
    assert service.store.read("dev") == source


def test_failed_record_commit_keeps_original_and_backup(adoption, monkeypatch):
    service, _, source = adoption
    plan = service.plan("dev")

    def fail(machine):
        raise OSError("record commit failed")

    monkeypatch.setattr(service.store, "save", fail)
    with pytest.raises(OSError, match="commit failed"):
        service.apply("dev", plan)
    assert service.store.read("dev") == source
    assert (
        service.store.machine_dir("dev") / "machine.schema-2.json"
    ).read_bytes() == source


def test_old_records_are_never_decoded_by_normal_execution(adoption):
    service, engine, source = adoption
    with pytest.raises(MigrationRequiredError, match="mim migrate dev --plan"):
        service.store.load("dev")
    assert not engine.inspected
    assert service.store.read("dev") == source


def test_running_state_changes_do_not_require_touching_container(adoption):
    service, engine, _ = adoption
    plan = service.plan("dev")
    engine.state = RuntimeState.STOPPED
    service.apply("dev", plan)
    assert not engine.started and not engine.deleted


def test_plan_for_different_machine_cannot_be_applied(adoption):
    service, _, _ = adoption
    with pytest.raises(ValueError, match="does not name"):
        service.apply("other", service.plan("dev"))


@pytest.mark.parametrize(
    "changes",
    [
        {"source_sha256": "not-a-digest"},
        {"observed_configuration": "[]"},
        {"observed_state": RuntimeState.MISSING},
    ],
)
def test_adoption_plan_invariants_apply_to_the_service_api_too(adoption, changes):
    service, _, _ = adoption
    with pytest.raises(ValueError):
        replace(service.plan("dev"), **changes)


def test_adoption_plan_cannot_commit_a_deletion_state(adoption):
    service, _, source = adoption
    plan = service.plan("dev")
    with pytest.raises(ValueError, match="existing native machine"):
        replace(plan, machine=replace(plan.machine, operation=Operation.DELETING))
    assert service.store.read("dev") == source


@pytest.mark.parametrize(
    "changed", [None, "mounts", "ports", "memory", "cpu_quota", "network"]
)
def test_adoption_checks_declared_mounts_publications_and_budgets(
    adoption, tmp_path, changed
):
    service, engine, _ = adoption
    spec = replace(
        make_machine().spec,
        mounts=(MountSpec(tmp_path, "/work/dev"),),
        ports=(PortBind(8080, 80),),
        resources=ResourceSpec(2, 512),
        network=NetworkMode.NONE,
    )
    data = spec.to_data()
    data.pop("engine")
    data.pop("isolation")
    data.update(schema_version=2, runner="podman", created_at="2026-01-01")
    data["ports"] = [{"host": 8080, "guest": 80}]
    source = json.dumps(data).encode()
    service.store.record_path("dev").write_bytes(source)
    observed = json.loads(configuration(spec))
    if changed is not None:
        observed[changed] = {
            "mounts": [],
            "ports": {},
            "memory": 1,
            "cpu_quota": 1,
            "network": "host",
        }[changed]
    engine.observation = replace(
        engine.observation, configuration=json.dumps(observed, sort_keys=True)
    )
    if changed is not None:
        with pytest.raises(ValueError, match="does not match"):
            service.plan("dev")
        assert service.store.read("dev") == source
    else:
        plan = service.plan("dev")
        assert service.apply("dev", plan).spec == spec


def test_migration_cli_requires_reviewed_plan_before_applying(
    adoption, monkeypatch, tmp_path
):
    from typer.testing import CliRunner

    from mimchine.cli import app

    service, engine, source = adoption
    monkeypatch.setattr(
        "mimchine.paths.data_dir", lambda: service.store.base_dir.parent
    )
    monkeypatch.setattr("mimchine.migration.get_engine", lambda name: engine)
    result = CliRunner().invoke(app, ["migrate", "dev", "--plan"])
    assert result.exit_code == 0, result.output
    plan = json.loads(result.stdout)
    assert plan["observed_state"] == "running"
    assert service.store.read("dev") == source
    path = tmp_path / "adoption.json"
    path.write_text(result.stdout)
    applied = CliRunner().invoke(app, ["migrate", "dev", "--apply", str(path)])
    assert applied.exit_code == 0, applied.output
    assert service.store.load("dev").target.container_id == CID
    assert not engine.started and not engine.deleted
