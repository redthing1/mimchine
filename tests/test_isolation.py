from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mimchine.cli import app
from mimchine.config import AppConfig, Defaults, GvisorRuntime, load_config
from mimchine.domain import (
    IdentityMode,
    IsolationMode,
    MachineSpec,
    MountSpec,
    NetworkMode,
    PortBind,
    ResourceSpec,
)
from mimchine.isolation import plan_machine
from mimchine.process import ProcessRunner
from mimchine.services import CreateOptions, MachineService
from mimchine.shell_state import ShellStateManager
from mimchine.state import MachineStore


@pytest.fixture(autouse=True)
def forbid_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline planning or rejected gVisor creation invoked a process")

    monkeypatch.setattr(ProcessRunner, "run", forbidden)


def machine(**kwargs) -> MachineSpec:
    return replace(
        MachineSpec(
            name="dev",
            image="example:dev",
            engine="podman",
        ),
        **kwargs,
    )


def gvisor_plan(record: MachineSpec, **kwargs):
    return plan_machine(
        replace(record, isolation=IsolationMode.GVISOR),
        GvisorRuntime(Path("/opt/runsc/release/runsc")),
        protected_paths=kwargs.get("protected_paths", ()),
    )


def test_gvisor_policy_preserves_rootfs_and_host_editable_workspaces(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    record = machine(
        mounts=(MountSpec(workspace, "/work/project"),),
        ports=(PortBind(8080, 80),),
        resources=ResourceSpec(cpus=2, memory_mib=1024),
        env=("MODE=dev",),
        workdir="/work/project",
    )

    plan = gvisor_plan(record)
    data = plan.to_data()

    assert data["status"] == "preview"
    assert data["execution_enabled"] is False
    assert plan.create[:2] == ("podman", "--remote=false")
    assert "mimchine.id=<machine-id>" in plan.create
    assert plan.policy.profile == "online"
    assert plan.policy.entrypoint == "/opt/runsc/release/profiles/online/runsc"
    assert not any(arg.startswith("--runtime-flag") for arg in plan.create)
    assert "--pids-limit=-1" in plan.create
    assert "--memory-swap=-1" in plan.create
    assert "--security-opt=no-new-privileges" in plan.create
    assert "127.0.0.1:8080:80" in plan.create
    assert f"{workspace}:/work/project:rw" in plan.create
    assert plan.create[plan.create.index("--memory") + 1] == "1024m"
    assert plan.create[plan.create.index("--cpus") + 1] == "2"
    assert "--network=pasta:--no-splice" in plan.create
    assert "<image-uid>" in " ".join(plan.create)
    assert data["machine"]["resources"] == ResourceSpec(2, 1024).to_data()


@pytest.mark.parametrize(
    "engine,isolation",
    [
        ("podman", IsolationMode.NATIVE),
        ("docker", IsolationMode.NATIVE),
        ("podman", IsolationMode.GVISOR),
    ],
)
@pytest.mark.parametrize(
    "resources,expected",
    [
        (ResourceSpec(), ()),
        (ResourceSpec(cpus=3), ("--cpus", "3")),
        (ResourceSpec(memory_mib=512), ("--memory", "512m", "--memory-swap=-1")),
        (
            ResourceSpec(memory_mib=512, swap_mib=0),
            ("--memory", "512m", "--memory-swap=512m"),
        ),
        (
            ResourceSpec(memory_mib=512, swap_mib=128),
            ("--memory", "512m", "--memory-swap=640m"),
        ),
        (ResourceSpec(tasks=256), ("--pids-limit=256",)),
    ],
)
def test_resource_arguments_have_no_isolation_specific_defaults(
    engine, isolation, resources, expected
):
    spec = machine(engine=engine, isolation=isolation, resources=resources)
    plan = plan_machine(
        spec, GvisorRuntime(Path("/opt/runsc/release/runsc")), protected_paths=()
    )
    args = plan.create
    for item in expected:
        assert item in args
    assert ("--cpus" in args) == (resources.cpus is not None)
    assert ("--memory" in args) == (resources.memory_mib is not None)
    assert any(item.startswith("--memory-swap=") for item in args) == (
        resources.memory_mib is not None
    )
    assert f"--pids-limit={resources.tasks or -1}" in args


def test_gvisor_swap_only_policy_does_not_invent_a_memory_ceiling():
    plan = gvisor_plan(machine(resources=ResourceSpec(swap_mib=0)))
    assert "--memory" not in plan.create
    assert not any(item.startswith("--memory-swap=") for item in plan.create)
    assert plan.spec.resources == ResourceSpec(swap_mib=0)


@pytest.mark.parametrize("engine", ["podman", "docker"])
def test_native_swap_only_policy_is_refused_before_allocating(engine):
    with pytest.raises(ValueError, match="swap limits require a memory limit"):
        plan_machine(
            machine(engine=engine, resources=ResourceSpec(swap_mib=0)),
            GvisorRuntime(),
            protected_paths=(),
        )


def test_every_gvisor_lifecycle_template_has_the_same_policy_and_id():
    plan = gvisor_plan(machine())
    prefix = plan.policy.prefix

    assert plan.create[: len(prefix)] == prefix
    for action, command in plan.lifecycle:
        assert command[: len(prefix)] == prefix, action
        assert "<container-id>" in command
        assert "dev" not in command
    assert {action for action, _ in plan.lifecycle} == {
        "init",
        "start",
        "inspect",
        "exec",
        "stop",
        "delete",
    }
    assert {requirement.name for requirement in plan.requirements} >= {
        "host",
        "distribution",
        "runtime-policy",
        "binding",
        "identity",
        "prestart",
        "resources",
    }


def test_offline_network_does_not_fall_back_to_host_network():
    plan = gvisor_plan(machine(network=NetworkMode.NONE))

    assert plan.policy.profile == "offline"
    assert "--network=pasta:--no-splice" not in plan.create
    assert plan.create[plan.create.index("--network") + 1] == "none"


def test_ssh_agent_grant_is_explicit_and_has_an_authority_requirement():
    ordinary = gvisor_plan(machine())
    forwarded = gvisor_plan(machine(ssh_agent=True))

    assert ordinary.policy.profile == "online"
    assert forwarded.policy.profile == "online-agent"
    assert "<ssh-auth-sock>:/mim/ssh-agent.sock:ro" in forwarded.create
    assert any(item.name == "host-socket-grant" for item in forwarded.requirements)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"engine": "docker"}, "rootless Podman"),
        ({"gpu": True}, "excludes GPU"),
        ({"job_limits": True}, "per-command cgroup"),
        ({"network": NetworkMode.HOST}, "host networking"),
        ({"container_args": ("--privileged",)}, "raw container arguments"),
        ({"container_args": ("--runtime=crun",)}, "raw container arguments"),
        (
            {"container_args": ("--annotation=dev.gvisor.flag.directfs=true",)},
            "raw container arguments",
        ),
    ],
)
def test_gvisor_rejects_incompatible_authority(changes, message):
    with pytest.raises(ValueError, match=message):
        gvisor_plan(machine(**changes))


@pytest.mark.parametrize("read_only", [True, False])
@pytest.mark.parametrize("relationship", ["parent", "child", "exact", "symlink"])
def test_mounts_cannot_expose_runtime_control_paths(tmp_path, read_only, relationship):
    control = tmp_path / "control"
    control.mkdir()
    source = {"parent": tmp_path, "exact": control, "child": control / "child"}.get(
        relationship
    )
    if relationship == "symlink":
        source = tmp_path / "link"
        source.symlink_to(control, target_is_directory=True)

    with pytest.raises(ValueError, match="overlaps protected control path"):
        gvisor_plan(
            machine(mounts=(MountSpec(source, "/shared", read_only=read_only),)),
            protected_paths=(control,),
        )


def test_runtime_distribution_is_a_protected_mount_source():
    with pytest.raises(ValueError, match="overlaps protected control path"):
        gvisor_plan(machine(mounts=(MountSpec(Path("/opt/runsc"), "/shared"),)))


def test_runtime_symlink_entry_and_distribution_are_both_protected(tmp_path):
    distribution = tmp_path / "distribution"
    distribution.mkdir()
    binary = distribution / "runsc"
    binary.touch()
    entry_dir = tmp_path / "entry"
    entry_dir.mkdir()
    entry = entry_dir / "runsc"
    entry.symlink_to(binary)
    for source in (entry_dir, distribution):
        with pytest.raises(ValueError, match="overlaps protected control path"):
            plan_machine(
                machine(
                    mounts=(MountSpec(source, "/shared"),),
                    isolation=IsolationMode.GVISOR,
                ),
                GvisorRuntime(entry),
                protected_paths=(),
            )


def test_shell_state_kind_is_not_an_authority_grant(tmp_path):
    control = tmp_path / "control"
    with pytest.raises(ValueError, match="overlaps protected control path"):
        gvisor_plan(
            machine(
                mounts=(MountSpec(control, "/mim/shell-state", kind="shell_state"),)
            ),
            protected_paths=(control,),
        )


@pytest.mark.parametrize("redirect", [None, "leaf", "parent"])
def test_generated_history_grant_cannot_redirect_to_another_control_path(
    tmp_path, redirect
):
    data = tmp_path / "data"
    history_root = data / "shell-state"
    control = tmp_path / "config"
    control.mkdir()
    data.mkdir()
    if redirect == "parent":
        history_root.symlink_to(control, target_is_directory=True)
    else:
        history_root.mkdir()
        if redirect == "leaf":
            (history_root / "dev").symlink_to(control, target_is_directory=True)
    history = ShellStateManager(history_root).mount_for("dev")

    def plan():
        return plan_machine(
            machine(isolation=IsolationMode.GVISOR, mounts=(history,)),
            GvisorRuntime(),
            protected_paths=(data, history_root, control),
            history_mount=history,
        )

    if redirect:
        with pytest.raises(ValueError, match="overlaps protected control path"):
            plan()
    else:
        assert plan().spec.mounts == (history,)


def test_generated_history_grant_cannot_contain_a_protected_child(tmp_path):
    history = ShellStateManager(tmp_path).mount_for("dev")
    with pytest.raises(ValueError, match="overlaps protected control path"):
        plan_machine(
            machine(isolation=IsolationMode.GVISOR, mounts=(history,)),
            GvisorRuntime(),
            protected_paths=(tmp_path, history.source / "credentials"),
            history_mount=history,
        )


@pytest.mark.parametrize("source", ["/dev", "/proc", "/sys", "/dev/kvm"])
def test_no_host_device_or_kernel_mounts(source):
    with pytest.raises(ValueError, match="kernel/device mounts"):
        gvisor_plan(machine(mounts=(MountSpec(Path(source), "/host"),)))


def test_no_unreviewed_mount_options(tmp_path):
    with pytest.raises(ValueError, match="mount options"):
        gvisor_plan(
            machine(mounts=(MountSpec(tmp_path, "/shared", options=("rshared",)),))
        )


def test_runtime_must_be_an_absolute_operator_supplied_path():
    with pytest.raises(ValueError, match="must be absolute"):
        GvisorRuntime(Path("runsc"))


def test_missing_runtime_is_reported_without_searching_path():
    plan = plan_machine(
        machine(isolation=IsolationMode.GVISOR), GvisorRuntime(), protected_paths=()
    )

    assert plan.to_data()["runtime"]["path"] is None
    assert plan.requirements[0].name == "runtime-path"
    assert "<gvisor-directory>/profiles/online/runsc" in plan.create


def test_configuration_loads_isolation_policy_and_external_runtime(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("""[defaults]
isolation = 'gvisor'
[runtimes.gvisor]
path = '/opt/runsc/release/runsc'
""")

    config = load_config(path)

    assert config.defaults.isolation is IsolationMode.GVISOR
    assert config.gvisor.path == Path("/opt/runsc/release/runsc")


@pytest.mark.parametrize(
    "toml",
    [
        "[runtimes.gvisor]\npath = 'runsc'",
        "[runtimes.gvisor]\npath = ''",
        "[runtimes.gvisor]\nflags = ['ignore-cgroups=true']",
        "[runtimes.other]\npath = '/bin/other'",
        "[defaults]\nisolation = 'something'",
    ],
)
def test_invalid_runtime_policy_configuration_is_rejected(tmp_path, toml):
    path = tmp_path / "config.toml"
    path.write_text(toml)
    with pytest.raises(ValueError):
        load_config(path)


def service(tmp_path, config=None):
    return MachineService(
        config or AppConfig(Defaults(), {}),
        MachineStore(tmp_path / "machines"),
        ShellStateManager(tmp_path / "shell-state"),
    )


def test_profile_and_cli_isolation_precedence_without_state_writes(tmp_path):
    config = AppConfig(
        Defaults(isolation=IsolationMode.GVISOR),
        {"dev": {"image": "example:dev", "isolation": "native"}},
    )
    svc = service(tmp_path, config)

    assert (
        svc.plan(CreateOptions("dev", profile="dev")).spec.isolation
        is IsolationMode.NATIVE
    )
    assert (
        svc.plan(
            CreateOptions("dev", profile="dev", isolation=IsolationMode.GVISOR)
        ).spec.isolation
        is IsolationMode.GVISOR
    )
    assert (
        svc.plan(CreateOptions("dev", image="example:dev")).spec.isolation
        is IsolationMode.GVISOR
    )
    assert not svc.store.base_dir.exists()
    assert not svc.shell_state.base_dir.exists()


@pytest.mark.parametrize("start", [False, True])
def test_missing_gvisor_runtime_is_rejected_before_provisioning(tmp_path, start):
    svc = service(tmp_path)
    svc.config = replace(svc.config, gvisor=GvisorRuntime(tmp_path / "missing/runsc"))

    with pytest.raises(ValueError, match="cannot verify gVisor distribution"):
        svc.create(
            CreateOptions(
                "dev", image="example:dev", isolation=IsolationMode.GVISOR, start=start
            )
        )

    assert not svc.store.exists("dev")
    assert not svc.shell_state.base_dir.exists()


def test_plan_does_not_read_or_migrate_existing_machine_records(tmp_path):
    svc = service(tmp_path)
    path = svc.store.record_path("dev")
    path.parent.mkdir(parents=True)
    original = b"existing records are deliberately not read during planning"
    path.write_bytes(original)

    plan = svc.plan(
        CreateOptions("dev", image="example:dev", isolation=IsolationMode.GVISOR)
    )

    assert plan.spec.isolation is IsolationMode.GVISOR
    assert path.read_bytes() == original
    assert not svc.shell_state.base_dir.exists()


def test_plan_cli_needs_no_installed_engine_and_creates_no_xdg_state(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = CliRunner().invoke(
        app,
        [
            "create",
            "dev",
            "--image",
            "example:dev",
            "--isolation",
            "gvisor",
            "--plan",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["isolation"] == "gvisor"
    assert data["status"] == "preview"
    assert data["execution_enabled"] is False
    assert list(tmp_path.iterdir()) == []


def test_native_preview_preserves_existing_default_network_and_port_semantics():
    record = machine(identity=IdentityMode.ROOT, ports=(PortBind(8080, 80),))
    plan = plan_machine(record, GvisorRuntime(), protected_paths=())

    assert plan.policy is None
    assert plan.create[:2] == ("podman", "--remote=false")
    assert "mimchine.id=<machine-id>" in plan.create
    assert plan.to_data()["status"] == "preview"
    assert "8080:80" in plan.create
    assert "127.0.0.1:8080:80" not in plan.create
    assert "--network=pasta" not in plan.create
    assert record.to_data()["isolation"] == "native"


def test_offline_gvisor_cannot_publish_ports():
    with pytest.raises(ValueError, match="publishing requires networking"):
        gvisor_plan(machine(network=NetworkMode.NONE, ports=(PortBind(8080, 80),)))


@pytest.mark.parametrize("agent", [False, True])
def test_gvisor_grant_destinations_cannot_shadow_each_other(tmp_path, agent):
    target = "/mim/ssh-agent.sock" if agent else "/work"
    mounts = (
        (MountSpec(tmp_path, target),)
        if agent
        else (MountSpec(tmp_path, target), MountSpec(tmp_path / "other", target))
    )
    with pytest.raises(ValueError, match="destinations must be unique"):
        gvisor_plan(machine(mounts=mounts, ssh_agent=agent))


def test_gvisor_workspace_cannot_modify_host_command_search_path(tmp_path, monkeypatch):
    directory = tmp_path / "host-bin"
    directory.mkdir()
    monkeypatch.setenv("PATH", str(directory))
    with pytest.raises(ValueError, match="protected control path"):
        service(tmp_path).plan(
            CreateOptions(
                "dev",
                image="example",
                isolation=IsolationMode.GVISOR,
                workspaces=(str(directory),),
            )
        )
