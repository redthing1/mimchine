from dataclasses import replace
from uuid import uuid4

import pytest
from support import IMAGE_ID, RecordingProcessRunner, make_machine, replacement_engine

from mimchine.domain import MountSpec, Operation, RootfsImage
from mimchine.engines import get_engine


@pytest.mark.parametrize(
    "features",
    [
        {"gpu": True},
        {"job_limits": True},
        {"container_args": ("--volume=/host:/extra:ro",)},
    ],
)
def test_replacement_checks_replayed_feature_binds(monkeypatch, tmp_path, features):
    engine, machine, data = replacement_engine(monkeypatch, tmp_path, **features)
    extra = {"Type": "bind", "Source": "/host", "Destination": "/extra", "RW": False}
    data["mounts"].append(extra)
    engine.prepare_replacement(machine, desired=machine.spec)
    engine.verify_replacement(machine, before=machine)
    desired = replace(
        machine.spec, mounts=(*machine.spec.mounts, MountSpec(tmp_path, "/extra"))
    )
    with pytest.raises(ValueError, match="conflicts"):
        engine.prepare_replacement(machine, desired=desired)


@pytest.mark.parametrize("kind", ["volume", "tmpfs"])
def test_replacement_rejects_non_bind_storage_even_with_raw_args(
    monkeypatch, tmp_path, kind
):
    engine, machine, data = replacement_engine(
        monkeypatch, tmp_path, container_args=("--cap-drop=all",)
    )
    data["mounts"].append({"Type": kind, "Destination": "/extra"})
    with pytest.raises(ValueError, match="volumes"):
        engine.prepare_replacement(machine, desired=machine.spec)


@pytest.mark.parametrize("change", ["missing", "source", "mode", "devices"])
def test_replacement_refuses_changed_feature_bindings(monkeypatch, tmp_path, change):
    engine, before, data = replacement_engine(monkeypatch, tmp_path, gpu=True)
    data["mounts"].append(
        {"Type": "bind", "Source": "/driver", "Destination": "/driver", "RW": False}
    )
    original_observe = engine._observe_target
    original = original_observe(before)
    candidate = replace(before, spec=replace(before.spec, shell="bash"))
    monkeypatch.setattr(
        engine,
        "_observe_target",
        lambda target: original if target is before else original_observe(target),
    )
    if change == "missing":
        data["mounts"].pop()
    elif change == "source":
        data["mounts"][-1]["Source"] = "/other-driver"
    elif change == "mode":
        data["mounts"][-1]["RW"] = True
    else:
        data["boundary"] = {"devices": [{"PathOnHost": "/dev/other"}]}
    with pytest.raises(ValueError, match="differ from original"):
        engine.verify_replacement(candidate, before=before)


def test_replacement_allocation_replays_features_and_raw_arguments(
    monkeypatch, tmp_path
):
    process = RecordingProcessRunner()
    engine = get_engine("podman", process)
    monkeypatch.setattr(engine, "_gpu_args", lambda: ("--device=/dev/test-gpu",))
    monkeypatch.setattr(
        engine,
        "_job_limits_args",
        lambda spec: ("--cgroupns=private", "-v", "/helper:/mim/limit:ro"),
    )
    token = str(uuid4())
    machine = replace(
        make_machine(
            gpu=True,
            job_limits=True,
            container_args=("--cap-drop=all",),
            mounts=(MountSpec(tmp_path, "/data"),),
        ),
        generation=token,
        binding=None,
        operation=Operation.CREATING,
        creation_context=make_machine().target.context,
        rootfs=RootfsImage(IMAGE_ID, token, "crun", ("--user", "1001:1002")),
    )
    engine.allocate(machine)
    command = next(call for call in process.calls if "create" in call)
    for value in (
        "--device=/dev/test-gpu",
        "--cgroupns=private",
        "/helper:/mim/limit:ro",
        "--cap-drop=all",
        f"{tmp_path}:/data:rw",
        IMAGE_ID,
    ):
        assert value in command
    assert command[command.index("--user") + 1] == "1001:1002"
    assert command[command.index("--runtime") + 1] == "crun"


def test_podman_snapshot_reuses_layers_and_keeps_image_defaults(monkeypatch, tmp_path):
    engine, machine, _ = replacement_engine(monkeypatch, tmp_path)
    engine.process = RecordingProcessRunner()
    token = str(uuid4())
    defaults = {"Env": ["DEFAULT=base"], "WorkingDir": "/base"}

    def metadata(image, *, missing_ok=False):
        if image == IMAGE_ID:
            return {"Id": IMAGE_ID, "Config": defaults}
        if not any("commit" in call for call in engine.process.calls):
            return None
        return {"Id": IMAGE_ID, "Config": {"Labels": {"mimchine.snapshot": token}}}

    monkeypatch.setattr(engine, "_image_metadata", metadata)
    snapshot = engine.preserve_rootfs(machine, token, tmp_path)
    assert snapshot.image_env == ("DEFAULT=base",)
    assert snapshot.image_workdir == "/base"
    assert RootfsImage.from_data(snapshot.to_data()) == snapshot
    commit = engine.process.calls[0]
    assert "commit" in commit and "--include-volumes=false" in commit
    assert "--pause=false" in commit
    assert not any(
        "export" in call or "import" in call for call in engine.process.calls
    )
    assert not list(tmp_path.glob("rootfs-*.tar"))
    # A lost reply reuses the owned snapshot rather than committing again.
    assert engine.preserve_rootfs(machine, token, tmp_path) == snapshot
    assert len(engine.process.calls) == 1


def test_snapshot_allocation_restores_defaults_without_old_overrides(monkeypatch):
    process = RecordingProcessRunner()
    engine = get_engine("podman", process)
    token = str(uuid4())
    original = make_machine(env=("MODE=new",))
    snapshot = RootfsImage(
        IMAGE_ID, token, "crun", (), ("MODE=base", "DEFAULT=base"), "/base"
    )
    machine = replace(
        original,
        operation=Operation.CREATING,
        binding=None,
        creation_context=original.target.context,
        generation=token,
        rootfs=snapshot,
    )
    engine.allocate(machine)
    command = next(c for c in process.calls if "create" in c)
    assert "--unsetenv-all" in command
    assert command.index("MODE=base") < command.index("MODE=new")
    assert "DEFAULT=base" in command
    assert command[command.index("-w") + 1] == "/base"
    legacy = snapshot.to_data()
    legacy.pop("image_env")
    legacy.pop("image_workdir")
    assert RootfsImage.from_data(legacy).image_env is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("user", "other"),
        ("id_mappings", {"UidMap": ["0:0:65536"]}),
        ("network", "host"),
        ("security", {"Privileged": True}),
        ("memory", 1024),
        ("pids_limit", 10),
        ("nano_cpus", 1_000_000_000),
    ],
)
def test_replacement_rejects_changed_identity_security_or_limits(
    monkeypatch, tmp_path, field, value
):
    engine, before, data = replacement_engine(monkeypatch, tmp_path)
    observe = engine._observe_target
    original = observe(before)
    candidate = replace(before, spec=replace(before.spec, shell="bash"))
    monkeypatch.setattr(
        engine, "_observe_target", lambda m: original if m is before else observe(m)
    )
    data[field] = value
    with pytest.raises(ValueError):
        engine.verify_replacement(candidate, before=before)


@pytest.mark.parametrize(
    "runner,swap,accepted",
    [
        ("podman", 0, True),
        ("podman", -1, True),
        ("docker", -1, True),
        ("docker", 0, False),
    ],
)
def test_swap_verification_uses_backend_semantics(runner, swap, accepted):
    from mimchine.domain import ResourceSpec

    engine = get_engine(runner, RecordingProcessRunner(engine=runner))
    machine = make_machine(engine=runner, resources=ResourceSpec(memory_mib=256))
    data = {"memory": 256 * 1024 * 1024, "memory_swap": swap}
    if accepted:
        engine._verify_resources(machine, data)
    else:
        with pytest.raises(ValueError, match="swap"):
            engine._verify_resources(machine, data)


@pytest.mark.parametrize(
    "config",
    [
        {"Env": ["LINES=first\nsecond"]},
        {"Healthcheck": {"Test": ["CMD", "true"]}},
        {"OnBuild": ["RUN true"]},
    ],
)
@pytest.mark.parametrize("runner", ["podman", "docker"])
def test_image_configuration_uses_backend_capabilities(
    monkeypatch, tmp_path, config, runner
):
    engine, machine, _ = replacement_engine(monkeypatch, tmp_path, runner=runner)
    monkeypatch.setattr(engine, "_image_metadata", lambda image: {"Config": config})
    if runner == "podman":
        engine.prepare_replacement(machine, desired=machine.spec)
    else:
        with pytest.raises(ValueError):
            engine.prepare_replacement(machine, desired=machine.spec)


def test_preflight_rejects_duplicate_bind_destinations(monkeypatch, tmp_path):
    engine, machine, data = replacement_engine(monkeypatch, tmp_path)
    data["mounts"].append(data["mounts"][0])
    with pytest.raises(ValueError, match="ambiguous"):
        engine.prepare_replacement(machine, desired=machine.spec)
