from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from support import CID, FakeEngine, context
from test_services import _service

from mimchine.domain import ExecSpec, Operation, RuntimeState
from mimchine.services import CreateOptions


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("container_id", "c" * 64, "container ID"),
        ("image_id", "sha256:" + "c" * 64, "container image"),
        ("runtime", "runsc", "OCI runtime"),
        ("labels", (("mimchine", "1"),), "ownership"),
        (
            "context",
            replace(context(), identity=(("host", "another-host"),)),
            "engine context",
        ),
    ],
)
def test_exec_rejects_binding_drift_before_start(tmp_path, field, value, message):
    engine = FakeEngine()
    service = _service(tmp_path, engine)
    service.create(CreateOptions("dev", image="alpine"))
    engine.observation = replace(engine.observation, **{field: value})
    with pytest.raises(ValueError, match=message):
        service.exec("dev", ExecSpec(("true",)))
    assert not engine.started and not engine.execs
    view = service.inspect("dev")
    assert view.state is RuntimeState.UNKNOWN
    assert message in view.problem
    assert view.observation is not None


def test_missing_id_is_never_replaced_by_lookup_of_the_name(tmp_path):
    engine = FakeEngine()
    service = _service(tmp_path, engine)
    service.create(CreateOptions("dev", image="alpine"))
    engine.state = RuntimeState.MISSING
    engine.inspected.clear()
    with pytest.raises(ValueError, match="missing"):
        service.exec("dev", ExecSpec(("true",)))
    assert engine.inspected == [CID]
    assert service.store.exists("dev")


def test_engine_error_is_visible_as_unknown_and_does_not_start(tmp_path):
    engine = FakeEngine()
    service = _service(tmp_path, engine)
    service.create(CreateOptions("dev", image="alpine"))
    engine.observation_error = RuntimeError("engine inaccessible")
    with pytest.raises(RuntimeError, match="engine inaccessible"):
        service.start("dev")
    assert not engine.started
    view = service.inspect("dev")
    assert view.state is RuntimeState.UNKNOWN
    assert view.problem == "engine inaccessible"


def test_listing_surfaces_old_and_corrupt_records_without_hiding_new_machines(tmp_path):
    engine = FakeEngine()
    service = _service(tmp_path, engine)
    machine = service.create(CreateOptions("dev", image="alpine"))
    for name, contents in (("old", '{"schema_version": 2}'), ("corrupt", "not-json")):
        path = service.store.record_path(name)
        path.parent.mkdir(parents=True)
        path.write_text(contents)
    rows = {row.name: row for row in service.list()}
    assert rows["dev"].view.machine == machine
    assert "mim migrate old --plan" in rows["old"].problem
    assert rows["corrupt"].problem
    assert rows["old"].view is None and rows["corrupt"].view is None


def test_missing_container_in_a_different_context_does_not_erase_binding(tmp_path):
    engine = FakeEngine()
    service = _service(tmp_path, engine)
    machine = service.create(CreateOptions("dev", image="alpine"))
    engine.state = RuntimeState.MISSING
    engine.context_override = replace(context(), identity=(("host", "different"),))
    with pytest.raises(ValueError, match="engine context"):
        service.delete("dev")
    assert service.store.load("dev").binding == machine.binding
    assert service.shell_state.path_for("dev").exists()


def test_lost_allocation_cannot_be_discarded_after_context_switch(tmp_path):
    engine = FakeEngine(create_error=RuntimeError("allocation interrupted"))
    service = _service(tmp_path, engine)
    with pytest.raises(RuntimeError):
        service.create(CreateOptions("dev", image="alpine"))
    engine.context_override = replace(context(), identity=(("host", "different"),))
    with pytest.raises(ValueError, match="creation transaction"):
        service.delete("dev")
    assert service.store.exists("dev")


def test_failed_creation_keeps_reservation_for_targeted_recovery(tmp_path):
    engine = FakeEngine(create_error=RuntimeError("creation failed"))
    service = _service(tmp_path, engine)
    with pytest.raises(RuntimeError):
        service.create(CreateOptions("dev", image="alpine"))
    machine = service.store.load("dev")
    assert machine.operation is Operation.CREATING
    assert machine.error == "creation failed"
    assert machine.target is None
    service.delete("dev")
    assert not engine.deleted
    assert not service.store.exists("dev")


def test_recovery_of_lost_id_requires_original_unique_ownership(tmp_path):
    engine = FakeEngine()
    service = _service(tmp_path, engine)
    machine = service.create(CreateOptions("dev", image="alpine"))
    service.store.save(
        replace(
            machine,
            binding=None,
            operation=Operation.CREATING,
            creation_context=machine.target.context,
        )
    )
    recovered = service.recover("dev")
    assert recovered.target.container_id == CID
    assert recovered.operation is Operation.READY
    assert "dev" in engine.inspected


def test_unrelated_reused_name_is_not_touched_during_recovery(tmp_path):
    engine = FakeEngine()
    service = _service(tmp_path, engine)
    machine = service.create(CreateOptions("dev", image="alpine"))
    service.store.save(
        replace(
            machine,
            binding=None,
            operation=Operation.CREATING,
            creation_context=machine.target.context,
        )
    )
    engine.observation = replace(engine.observation, labels=(("mimchine", "1"),))
    with pytest.raises(ValueError, match="ownership"):
        service.recover("dev")
    with pytest.raises(ValueError, match="ownership"):
        service.delete("dev")
    assert not engine.deleted
    assert service.store.load("dev").operation is Operation.CREATING


def test_delete_requires_confirmed_absence(tmp_path):
    engine = FakeEngine()
    service = _service(tmp_path, engine)
    machine = service.create(CreateOptions("dev", image="alpine"))
    engine.delete = lambda target: engine.deleted.append(target)
    with pytest.raises(ValueError, match="removal was not confirmed"):
        service.delete("dev")
    retained = service.store.load("dev")
    assert retained.binding == machine.binding
    assert retained.operation is Operation.DELETING
    assert service.shell_state.path_for("dev").exists()


def test_cleanup_can_remove_owned_target_after_runtime_drift(tmp_path):
    engine = FakeEngine()
    service = _service(tmp_path, engine)
    service.create(CreateOptions("dev", image="alpine"))
    engine.observation = replace(engine.observation, runtime="damaged-runtime")
    service.delete("dev")
    assert engine.deleted[0].target.container_id == CID
    assert not service.store.exists("dev")


def test_shell_cleanup_failure_retains_deletion_journal_for_retry(
    tmp_path, monkeypatch
):
    from mimchine.shell_state import shutil

    engine = FakeEngine()
    service = _service(tmp_path, engine)
    machine = service.create(CreateOptions("dev", image="alpine"))
    remove = shutil.rmtree
    history = service.shell_state.path_for("dev")

    def fail_history(path, *args, **kwargs):
        if path == history:
            raise PermissionError("history cleanup failed")
        return remove(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "rmtree", fail_history)
        with pytest.raises(PermissionError, match="history cleanup failed"):
            service.delete("dev")
    retained = service.store.load("dev")
    assert retained.operation is Operation.DELETING
    assert retained.binding == machine.binding
    assert retained.error == "history cleanup failed"
    assert history.is_dir()
    service.delete("dev")
    assert not history.exists() and not service.store.exists("dev")
    assert len(engine.deleted) == 1


def test_partial_record_cleanup_restores_deletion_journal(tmp_path, monkeypatch):
    engine = FakeEngine()
    service = _service(tmp_path, engine)
    machine = service.create(CreateOptions("dev", image="alpine"))

    def fail_cleanup(name):
        service.store.record_path(name).unlink()
        raise OSError("record cleanup interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(service.store, "delete", fail_cleanup)
        with pytest.raises(OSError, match="record cleanup interrupted"):
            service.delete("dev")
    retained = service.store.load("dev")
    assert retained.operation is Operation.DELETING
    assert retained.binding == machine.binding
    service.delete("dev")
    assert not service.store.exists("dev")
    assert len(engine.deleted) == 1


def test_inspection_reports_process_access_failure_as_unknown(tmp_path):
    engine = FakeEngine()
    service = _service(tmp_path, engine)
    service.create(CreateOptions("dev", image="alpine"))
    engine.observation_error = PermissionError("engine executable is inaccessible")
    view = service.inspect("dev")
    assert view.state is RuntimeState.UNKNOWN
    assert view.problem == "engine executable is inaccessible"


def test_two_creates_cannot_allocate_the_same_name(tmp_path):
    engine = FakeEngine()
    service = _service(tmp_path, engine)

    def create():
        try:
            return service.create(CreateOptions("dev", image="alpine"))
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert len(engine.created) == 1
    assert (
        sum(
            isinstance(result, str) and "already exists" in result for result in results
        )
        == 1
    )


def test_interactive_exec_does_not_hold_the_lifecycle_lock(tmp_path):
    engine = FakeEngine()
    service = _service(tmp_path, engine)
    service.create(CreateOptions("dev", image="alpine"))
    both_executing = Barrier(2)

    def exec_session(machine, spec):
        both_executing.wait(timeout=3)
        engine.execs.append((machine, spec))

    engine.exec = exec_session
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: service.exec("dev", ExecSpec(("true",))), range(2)))
    assert len(engine.execs) == 2
