from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from mimchine.config import AppConfig, Defaults
from mimchine.domain import (
    ExecSpec,
    IdentityMode,
    IdentitySpec,
    ImageSource,
    ImageSourceKind,
    NetworkMode,
    ResourceSpec,
    RunnerCapabilities,
    RuntimeState,
    RuntimeStatus,
)
from mimchine.services import CreateOptions, MachineService
from mimchine.shells import AUTO_ENTER_SHELL_COMMAND
from mimchine.shell_state import ShellStateManager
from mimchine.smolvm_images import PruneResult
from mimchine.state import MachineStore


CAPS = RunnerCapabilities(
    image_sources=(
        ImageSourceKind.OCI_REFERENCE,
        ImageSourceKind.SMOLMACHINE,
    ),
    offline_oci_references=True,
    directory_mounts=True,
    file_mounts=True,
    published_ports=True,
    outbound_network=True,
    restricted_network=True,
    host_network=True,
    ssh_agent=True,
    gpu_vulkan=True,
    root_identity=True,
    host_identity=True,
)


@dataclass
class FakeRunner:
    name: str = "podman"
    capabilities: RunnerCapabilities = CAPS
    state: RuntimeState = RuntimeState.STOPPED
    create_error: Exception | None = None
    delete_error: Exception | None = None
    created: list = field(default_factory=list)
    started: list = field(default_factory=list)
    execs: list = field(default_factory=list)
    deleted: list = field(default_factory=list)

    def create(self, record):
        if self.create_error is not None:
            raise self.create_error
        self.created.append(record)

    def start(self, record):
        self.started.append(record)
        self.state = RuntimeState.RUNNING

    def stop(self, record):
        self.state = RuntimeState.STOPPED

    def delete(self, record):
        self.deleted.append(record)
        if self.delete_error is not None:
            raise self.delete_error

    def exec(self, record, spec):
        self.execs.append((record, spec))

    def inspect(self, record):
        return RuntimeStatus(record.name, record.runner, record.backend_id, self.state)


def test_create_merges_profile_and_cli_into_record(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = FakeRunner()
    service = _service(
        tmp_path,
        runner,
        profiles={
            "dev": {
                "image": "fedora:latest",
                "runner": "podman",
                "workspace": str(workspace),
                "env": ["PROFILE=1"],
                "network": "none",
                "identity": "host",
                "shell": "bash -l",
            }
        },
    )

    record = service.create(
        CreateOptions(
            name="dev",
            profile="dev",
            env=("CLI=1",),
            ports=("8080:80",),
        )
    )

    assert runner.created == [record]
    assert service.store.load("dev") == record
    assert record.image.value == "fedora:latest"
    assert record.network.mode is NetworkMode.NONE
    assert record.identity.mode is IdentityMode.HOST
    assert record.env == ("PROFILE=1", "CLI=1")
    assert [mount.kind for mount in record.mounts] == ["workspace", "shell_state"]
    assert record.shell == "bash -l"


def test_create_merges_resource_defaults_profile_and_cli(tmp_path: Path) -> None:
    runner = FakeRunner()
    config = AppConfig(
        defaults=Defaults(
            resources=ResourceSpec(
                cpus=2,
                memory_mib=4096,
                storage_gib=16,
                overlay_gib=8,
            )
        ),
        profiles={
            "vm": {
                "image": "fedora:latest",
                "cpus": 4,
                "memory": 8192,
            }
        },
    )
    service = MachineService(
        config,
        MachineStore(tmp_path / "machines"),
        ShellStateManager(tmp_path / "shell-state"),
        {"podman": runner},
    )

    record = service.create(
        CreateOptions(
            name="dev",
            profile="vm",
            resources=ResourceSpec(cpus=6),
        )
    )

    assert record.resources.cpus == 6
    assert record.resources.memory_mib == 8192
    assert record.resources.storage_gib == 16
    assert record.resources.overlay_gib == 8


def test_profile_can_disable_shell_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = FakeRunner()
    service = _service(
        tmp_path,
        runner,
        profiles={
            "isolated": {
                "image": "fedora:latest",
                "workspace": str(workspace),
                "shell_state": False,
            }
        },
    )

    record = service.create(CreateOptions(name="box", profile="isolated"))

    assert record.shell_state.enabled is False
    assert [mount.kind for mount in record.mounts] == ["workspace"]
    assert not (tmp_path / "shell-state" / "box").exists()


def test_cli_can_enable_shell_state_over_profile(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = _service(
        tmp_path,
        runner,
        profiles={
            "isolated": {
                "image": "fedora:latest",
                "shell_state": False,
            }
        },
    )

    record = service.create(
        CreateOptions(name="box", profile="isolated", shell_state=True)
    )

    assert record.shell_state.enabled is True
    assert [mount.kind for mount in record.mounts] == ["shell_state"]
    assert (tmp_path / "shell-state" / "box").exists()


def test_create_expands_home_share_mounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_home = tmp_path / "home" / "fed"
    dev = host_home / "Dev"
    work = host_home / "Downloads" / "Work"
    dev.mkdir(parents=True)
    work.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(host_home))

    runner = FakeRunner()
    service = _service(
        tmp_path,
        runner,
        profiles={"binavibe": {"home_share": str(dev)}},
    )

    record = service.create(
        CreateOptions(
            name="binavibe",
            image="localhost/binavibe:latest",
            profile="binavibe",
            home_shares=(str(work),),
        )
    )

    assert [
        (mount.source, mount.target, mount.kind)
        for mount in record.mounts
        if mount.kind == "home_share"
    ] == [
        (dev.resolve(), str(dev.resolve()), "home_share"),
        (dev.resolve(), "/home/user/Dev", "home_share"),
        (work.resolve(), str(work.resolve()), "home_share"),
        (work.resolve(), "/home/user/Downloads/Work", "home_share"),
    ]


def test_enter_starts_machine_and_execs_shell_from_mapped_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = FakeRunner()
    service = _service(tmp_path, runner)
    service.create(
        CreateOptions(name="dev", image="alpine", workspaces=(str(workspace),))
    )
    monkeypatch.chdir(workspace)

    service.enter("dev")

    assert runner.started
    record, spec = runner.execs[0]
    assert record.name == "dev"
    assert spec.command == AUTO_ENTER_SHELL_COMMAND
    assert spec.interactive is True
    assert spec.tty is True
    assert spec.workdir == "/work/workspace"
    assert spec.env == ("MIM_MACHINE=dev", "MIM_RUNNER=podman")


def test_enter_uses_explicit_shell_from_record(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = _service(tmp_path, runner)
    service.create(CreateOptions(name="dev", image="alpine", shell="zsh -l"))

    service.enter("dev")

    assert runner.execs[0][1].command == ("zsh", "-l")
    assert runner.execs[0][1].env == (
        "MIM_MACHINE=dev",
        "MIM_RUNNER=podman",
        "HISTFILE=/mim/shell-state/.zsh_history",
    )


def test_enter_does_not_set_shell_state_env_when_disabled(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = _service(
        tmp_path,
        runner,
        profiles={
            "isolated": {
                "image": "alpine",
                "shell": "zsh -l",
                "shell_state": False,
            }
        },
    )
    service.create(CreateOptions(name="dev", profile="isolated"))

    service.enter("dev")

    assert runner.execs[0][1].command == ("zsh", "-l")
    assert runner.execs[0][1].env == ("MIM_MACHINE=dev", "MIM_RUNNER=podman")


def test_enter_shell_flag_overrides_record_shell(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = _service(tmp_path, runner)
    service.create(CreateOptions(name="dev", image="alpine", shell="zsh -l"))

    service.enter("dev", "auto")

    assert runner.execs[0][1].command == AUTO_ENTER_SHELL_COMMAND
    assert runner.execs[0][1].env == ("MIM_MACHINE=dev", "MIM_RUNNER=podman")


def test_enter_uses_config_default_shell(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = MachineService(
        AppConfig(defaults=Defaults(shell="bash -l"), profiles={}),
        MachineStore(tmp_path / "machines"),
        ShellStateManager(tmp_path / "shell-state"),
        {"podman": runner},
    )
    record = service.create(CreateOptions(name="dev", image="alpine"))

    service.enter("dev")

    assert record.shell is None
    assert runner.execs[0][1].command == ("bash", "-l")
    assert runner.execs[0][1].env == (
        "MIM_MACHINE=dev",
        "MIM_RUNNER=podman",
        "HISTFILE=/mim/shell-state/.bash_history",
    )


def test_exec_starts_machine_before_running_command(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = _service(tmp_path, runner)
    service.create(CreateOptions(name="dev", image="alpine"))

    service.exec("dev", ExecSpec(("echo", "hello")))

    assert runner.started
    assert runner.execs[0][1].command == ("echo", "hello")


def test_exec_rejects_missing_backend_with_clear_error(tmp_path: Path) -> None:
    runner = FakeRunner(state=RuntimeState.MISSING)
    service = _service(tmp_path, runner)
    service.create(CreateOptions(name="dev", image="alpine"))

    with pytest.raises(ValueError, match="backend \\[dev\\] is missing"):
        service.exec("dev", ExecSpec(("echo", "hello")))


def test_rejects_runner_unsupported_file_mount(tmp_path: Path) -> None:
    file_path = tmp_path / "config"
    file_path.write_text("x", encoding="utf-8")
    caps = RunnerCapabilities(
        image_sources=(
            ImageSourceKind.OCI_REFERENCE,
            ImageSourceKind.SMOLMACHINE,
        ),
        offline_oci_references=True,
        directory_mounts=True,
        file_mounts=False,
        published_ports=True,
        outbound_network=True,
        restricted_network=True,
        host_network=True,
        ssh_agent=True,
        gpu_vulkan=True,
        root_identity=True,
        host_identity=True,
    )
    service = _service(tmp_path, FakeRunner(capabilities=caps))

    with pytest.raises(ValueError, match="file mounts"):
        service.create(
            CreateOptions(
                name="dev",
                image="alpine",
                mounts=(f"{file_path}:/config:ro",),
            )
        )


def test_rejects_runner_unsupported_image_source(tmp_path: Path) -> None:
    caps = RunnerCapabilities(
        image_sources=(ImageSourceKind.OCI_REFERENCE,),
        offline_oci_references=True,
        directory_mounts=True,
        file_mounts=True,
        published_ports=True,
        outbound_network=True,
        restricted_network=True,
        host_network=True,
        ssh_agent=True,
        gpu_vulkan=True,
        root_identity=True,
        host_identity=True,
    )
    service = _service(tmp_path, FakeRunner(capabilities=caps))

    with pytest.raises(ValueError, match="image source"):
        service.create(
            CreateOptions(
                name="dev",
                image=str(tmp_path / "tool.smolmachine"),
            )
        )

    assert not (tmp_path / "shell-state" / "dev").exists()


def test_rejects_missing_smolmachine_file(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = _service(tmp_path, runner)

    with pytest.raises(ValueError, match="\\.smolmachine file does not exist"):
        service.create(
            CreateOptions(
                name="dev",
                image=str(tmp_path / "tool.smolmachine"),
            )
        )

    assert runner.created == []
    assert not (tmp_path / "shell-state" / "dev").exists()


def test_rejects_offline_oci_reference_when_runner_requires_network(
    tmp_path: Path,
) -> None:
    caps = RunnerCapabilities(
        image_sources=(ImageSourceKind.OCI_REFERENCE,),
        offline_oci_references=False,
        directory_mounts=True,
        file_mounts=True,
        published_ports=True,
        outbound_network=True,
        restricted_network=True,
        host_network=True,
        ssh_agent=True,
        gpu_vulkan=True,
        root_identity=True,
        host_identity=True,
    )
    runner = FakeRunner(name="smolvm", capabilities=caps)
    service = MachineService(
        AppConfig(defaults=Defaults(runner="smolvm"), profiles={}),
        MachineStore(tmp_path / "machines"),
        ShellStateManager(tmp_path / "shell-state"),
        {"smolvm": runner},
    )

    with pytest.raises(ValueError, match="needs networking to start OCI references"):
        service.create(
            CreateOptions(
                name="dev",
                image="alpine",
                network=NetworkMode.NONE,
            )
        )

    assert runner.created == []
    assert not (tmp_path / "shell-state" / "dev").exists()


def test_imported_smolvm_image_can_be_created_without_network(tmp_path: Path) -> None:
    caps = RunnerCapabilities(
        image_sources=(
            ImageSourceKind.OCI_REFERENCE,
            ImageSourceKind.SMOLMACHINE,
        ),
        offline_oci_references=True,
        directory_mounts=True,
        file_mounts=True,
        published_ports=True,
        outbound_network=True,
        restricted_network=True,
        host_network=True,
        ssh_agent=True,
        gpu_vulkan=True,
        root_identity=True,
        host_identity=True,
    )
    runner = FakeRunner(name="smolvm", capabilities=caps)
    images = FakeSmolvmImages()
    service = MachineService(
        AppConfig(defaults=Defaults(builder="podman", runner="smolvm"), profiles={}),
        MachineStore(tmp_path / "machines"),
        ShellStateManager(tmp_path / "shell-state"),
        {"smolvm": runner},
        smolvm_images=images,
    )

    record = service.create(
        CreateOptions(
            name="dev",
            image="mim_codex",
            network=NetworkMode.NONE,
        )
    )

    assert images.materialized == [("mim_codex", "podman")]
    assert record.image.kind is ImageSourceKind.OCI_REFERENCE
    assert record.image.value == "mim_codex"
    assert runner.created == [record]


def test_rejects_runner_unsupported_root_identity(tmp_path: Path) -> None:
    caps = RunnerCapabilities(
        image_sources=(ImageSourceKind.OCI_REFERENCE,),
        offline_oci_references=True,
        directory_mounts=True,
        file_mounts=True,
        published_ports=True,
        outbound_network=True,
        restricted_network=True,
        host_network=True,
        ssh_agent=True,
        gpu_vulkan=True,
        root_identity=False,
        host_identity=True,
    )
    runner = FakeRunner(name="smolvm", capabilities=caps)
    service = MachineService(
        AppConfig(defaults=Defaults(runner="smolvm"), profiles={}),
        MachineStore(tmp_path / "machines"),
        ShellStateManager(tmp_path / "shell-state"),
        {"smolvm": runner},
    )

    with pytest.raises(ValueError, match="does not support root identity"):
        service.create(
            CreateOptions(
                name="dev",
                image="alpine",
                identity=IdentitySpec(IdentityMode.ROOT),
            )
        )

    assert runner.created == []


def test_prune_delegates_to_smolvm_images(tmp_path: Path) -> None:
    images = FakeSmolvmImages()
    service = MachineService(
        AppConfig(defaults=Defaults(), profiles={}),
        MachineStore(tmp_path / "machines"),
        ShellStateManager(tmp_path / "shell-state"),
        {"podman": FakeRunner()},
        smolvm_images=images,
    )

    result = service.prune(dry_run=True)

    assert result == PruneResult(
        image_refs=0,
        image_entries=0,
        staging_entries=0,
        bytes_reclaimable=0,
        dry_run=True,
    )
    assert images.pruned == [True]


def test_create_cleans_backend_and_shell_state_when_save_fails(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = FakeRunner()
    service = MachineService(
        AppConfig(defaults=Defaults(), profiles={}),
        FailingStore(tmp_path / "machines"),
        ShellStateManager(tmp_path / "shell-state"),
        {"podman": runner},
    )

    with pytest.raises(RuntimeError, match="save failed"):
        service.create(
            CreateOptions(name="dev", image="alpine", workspaces=(str(workspace),))
        )

    assert runner.created[0].name == "dev"
    assert runner.deleted[0].name == "dev"
    assert not (tmp_path / "shell-state" / "dev").exists()


def test_create_failure_does_not_delete_unowned_backend(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = FakeRunner(create_error=RuntimeError("backend create failed"))
    service = _service(tmp_path, runner)

    with pytest.raises(RuntimeError, match="backend create failed"):
        service.create(
            CreateOptions(name="dev", image="alpine", workspaces=(str(workspace),))
        )

    assert runner.deleted == []
    assert not service.store.exists("dev")
    assert not (tmp_path / "shell-state" / "dev").exists()


def test_create_failure_preserves_existing_shell_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = FakeRunner(create_error=RuntimeError("backend create failed"))
    service = _service(tmp_path, runner)
    shell_state_path = tmp_path / "shell-state" / "dev"
    shell_state_path.mkdir(parents=True)
    history = shell_state_path / ".zsh_history"
    history.write_text("kept\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="backend create failed"):
        service.create(
            CreateOptions(name="dev", image="alpine", workspaces=(str(workspace),))
        )

    assert history.read_text(encoding="utf-8") == "kept\n"


def test_save_failure_cleanup_error_does_not_mask_save_error(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = FakeRunner(delete_error=RuntimeError("backend cleanup failed"))
    service = MachineService(
        AppConfig(defaults=Defaults(), profiles={}),
        FailingStore(tmp_path / "machines"),
        ShellStateManager(tmp_path / "shell-state"),
        {"podman": runner},
    )

    with pytest.raises(RuntimeError, match="save failed"):
        service.create(
            CreateOptions(name="dev", image="alpine", workspaces=(str(workspace),))
        )

    assert runner.deleted[0].name == "dev"
    assert not (tmp_path / "shell-state" / "dev").exists()


def _service(tmp_path: Path, runner: FakeRunner, profiles=None) -> MachineService:
    config = AppConfig(defaults=Defaults(), profiles=profiles or {})
    return MachineService(
        config,
        MachineStore(tmp_path / "machines"),
        ShellStateManager(tmp_path / "shell-state"),
        {"podman": runner},
    )


class FailingStore(MachineStore):
    def save(self, record) -> None:
        raise RuntimeError("save failed")


class FakeSmolvmImages:
    def __init__(self):
        self.materialized: list[tuple[str, str]] = []
        self.pruned: list[bool] = []

    def materialize(self, image: ImageSource, *, builder: str) -> None:
        self.materialized.append((image.value, builder))

    def prune(self, *, dry_run: bool) -> PruneResult:
        self.pruned.append(dry_run)
        return PruneResult(0, 0, 0, 0, dry_run)
