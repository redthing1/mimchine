from __future__ import annotations

import os
import json
import stat
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, TypeVar
from uuid import uuid4

from .builders import Builder, get_builder
from .config import AppConfig, Defaults, load_config, validate_builder, validate_runner
from .domain import (
    BuildSpec,
    ContainerAllocation,
    ExecSpec,
    GvisorBinding,
    IdentityMode,
    IsolationMode,
    Machine,
    MachineBinding,
    MachineObservation,
    MachineSpec,
    MissingContainer,
    MountSpec,
    NetworkMode,
    Operation,
    PortBind,
    ResourceSpec,
    RuntimeState,
    UpdateJournal,
    UpdateOptions,
    verify_observation,
)
from .engines import Engine, get_engine
from .gvisor import find_runtime, verify_runtime
from .isolation import GvisorPolicy, MachinePlan, control_paths, plan_machine
from .log import logger
from .mounts import (
    map_host_path_to_guest,
    parse_home_share_spec,
    parse_mount_spec,
    parse_workspace_spec,
)
from .paths import data_dir
from .profiles import Profile, load_profile
from .shell_state import ShellStateManager
from .shells import enter_shell_command, normalize_shell
from .state import MachineStore
from .updates import (
    UpdatePlan,
    explicit_port,
    plan_update,
    port_key,
    validate_mounts_ports,
)

T = TypeVar("T")


@dataclass(frozen=True)
class BuildOptions:
    image: str
    file: Path
    context: Path
    builder: str | None = None
    platform: str | None = None
    build_args: tuple[str, ...] = ()
    no_cache: bool = False


@dataclass(frozen=True)
class CreateOptions:
    name: str
    image: str | None = None
    runner: str | None = None
    profile: str | None = None
    workspaces: tuple[str, ...] = ()
    home_shares: tuple[str, ...] = ()
    mounts: tuple[str, ...] = ()
    ports: tuple[str, ...] = ()
    env: tuple[str, ...] = ()
    workdir: str | None = None
    shell: str | None = None
    network: NetworkMode | None = None
    ssh_agent: bool | None = None
    gpu: bool | None = None
    job_limits: bool | None = None
    cpus: int | None = None
    memory_mib: int | None = None
    swap_mib: int | None = None
    tasks: int | None = None
    identity: IdentityMode | None = None
    shell_state: bool | None = None
    container_args: tuple[str, ...] = ()
    start: bool = False
    isolation: IsolationMode | None = None


@dataclass(frozen=True)
class MachineView:
    machine: Machine
    state: RuntimeState
    problem: str | None = None
    observation: MachineObservation | MissingContainer | None = None


@dataclass(frozen=True)
class MachineSummary:
    name: str
    view: MachineView | None = None
    problem: str | None = None


class BuildService:
    def __init__(self, config: AppConfig, builders: dict[str, Builder] | None = None):
        self.config = config
        self.builders = builders or {}

    @classmethod
    def default(cls) -> "BuildService":
        return cls(load_config())

    def build(self, options: BuildOptions) -> None:
        builder_name = validate_builder(options.builder or self.config.defaults.builder)
        builder = self.builders.get(builder_name) or get_builder(builder_name)
        spec = BuildSpec(
            image=options.image,
            file=options.file,
            context=options.context,
            platform=options.platform,
            build_args=options.build_args,
            no_cache=options.no_cache,
        )
        builder.build(spec)


class MachineService:
    def __init__(
        self,
        config: AppConfig,
        store: MachineStore,
        shell_state: ShellStateManager,
        engines: dict[str, Engine] | None = None,
    ):
        self.config, self.store, self.shell_state = config, store, shell_state
        self.engines = engines or {}

    @classmethod
    def default(cls) -> "MachineService":
        app_data = data_dir()
        return cls(
            load_config(),
            MachineStore(app_data / "machines"),
            ShellStateManager(app_data / "shell-state"),
        )

    def create(self, options: CreateOptions) -> Machine:
        spec = self._resolve(options)
        self._plan(spec)
        engine = self._engine(spec.engine)
        with self.store.lock(spec.name):
            if self.store.exists(spec.name):
                raise ValueError(f"machine [{spec.name}] already exists")
            gvisor = None
            if spec.isolation is IsolationMode.GVISOR:
                if sys.platform != "linux" or os.getuid() == 0:
                    raise ValueError("gVisor requires local rootless Podman on Linux")
                profile = GvisorPolicy(
                    self.config.gvisor, spec.network, spec.ssh_agent
                ).profile
                runtime = verify_runtime(find_runtime(self.config.gvisor.path), profile)
                agent = None
                if spec.ssh_agent:
                    path = os.environ.get("SSH_AUTH_SOCK")
                    if not path:
                        raise ValueError(
                            "SSH_AUTH_SOCK is required for SSH-agent forwarding"
                        )
                    agent = Path(path).resolve(strict=True)
                    if not stat.S_ISSOCK(agent.stat().st_mode):
                        raise ValueError("SSH_AUTH_SOCK must name a Unix socket")
                gvisor = GvisorBinding(
                    runtime.distribution,
                    runtime.manifest_sha256,
                    profile,
                    engine.resolve_image(spec.image),
                    (self.store.machine_dir(spec.name) / "gvisor").resolve(),
                    agent_socket=agent,
                    history_source=self.shell_state.path_for(spec.name).resolve()
                    if spec.shell_state
                    else None,
                )
            machine = Machine(
                str(uuid4()),
                spec,
                _now(),
                creation_context=engine.context(),
                gvisor=gvisor,
            )
            self.store.save(machine)
            return self._continue_creation(machine, engine, start=options.start)

    def _continue_creation(
        self, machine: Machine, engine: Engine, *, start: bool
    ) -> Machine:
        try:
            if machine.gvisor and machine.spec.identity is IdentityMode.IMAGE:
                probe = engine.allocate_identity_probe(machine)
                machine = replace(machine, identity_probe=probe)
                self.store.save(machine)
                user = engine.initialize_identity_probe(machine)
                machine = replace(
                    machine, gvisor=replace(machine.gvisor, image_user=user)
                )
                self.store.save(machine)
                engine.delete_identity_probe(machine)
                machine = replace(machine, identity_probe=None)
                self.store.save(machine)
            if machine.spec.shell_state:
                self.shell_state.ensure(machine.name)
            allocation = engine.allocate(machine)
            machine = replace(machine, allocation=allocation)
            self.store.save(machine)
            machine = self._complete_creation(machine, engine)
            if start:
                self._ensure_running(machine, engine)
            return machine
        except Exception as exc:
            self._remember_error(machine, exc)
            raise

    def _complete_creation(self, machine: Machine, engine: Engine) -> Machine:
        observation = self._observe(machine, engine)
        if observation is None:
            raise ValueError(f"allocated container for [{machine.name}] is missing")
        binding = MachineBinding(
            machine.target, observation.image_id, observation.runtime
        )
        machine = replace(
            machine,
            binding=binding,
            allocation=None,
            operation=Operation.READY,
            error=None,
            creation_context=None,
        )
        self.store.save(machine)
        return machine

    def recover(self, name: str) -> Machine:
        with self.store.lock(name):
            machine = self.store.load(name)
            engine = self._engine(machine.spec.engine)
            if machine.operation is Operation.DELETING:
                raise ValueError(
                    f"machine [{name}] has an incomplete deletion; retry mim delete"
                )
            if machine.update is not None:
                return self._continue_update(machine, engine, recovering=True)
            if machine.operation is Operation.READY:
                if self._observe(machine, engine) is None:
                    raise ValueError(
                        "bound container is missing; recovery did not replace it"
                    )
                return machine
            machine = self._recover_identity_probe(machine, engine)
            if machine.identity_probe is not None:
                engine.delete_identity_probe(machine)
                machine = replace(machine, identity_probe=None)
                self.store.save(machine)
            if machine.target is None:
                machine = self._recover_allocation(machine, engine)
                if machine.target is None:
                    raise ValueError(
                        f"no allocated object for [{name}]; delete the incomplete reservation"
                    )
            return self._complete_creation(machine, engine)

    def _recover_identity_probe(self, machine: Machine, engine: Engine) -> Machine:
        if (
            not machine.gvisor
            or machine.identity_probe is not None
            or machine.binding is not None
        ):
            return machine
        observed = engine.observe(machine.probe_name)
        if observed.context != machine.creation_context:
            raise ValueError(
                "identity preparation context does not match its transaction"
            )
        if isinstance(observed, MissingContainer):
            return machine
        allocation = ContainerAllocation(
            observed.container_id, observed.context, machine.probe_labels
        )
        observed.verify(allocation)
        machine = replace(machine, identity_probe=allocation)
        self.store.save(machine)
        return machine

    def _recover_allocation(self, machine: Machine, engine: Engine) -> Machine:
        # Only an unfinished allocation transaction may resolve its reserved
        # name, and only with its unique creation ownership token.
        observed = engine.observe(machine.name)
        if observed.context != machine.creation_context:
            raise ValueError("engine context does not match the creation transaction")
        if isinstance(observed, MissingContainer):
            return machine
        labels = machine.labels
        allocation = ContainerAllocation(
            observed.container_id, observed.context, labels
        )
        observed.verify(allocation)
        machine = replace(machine, allocation=allocation)
        self.store.save(machine)
        return machine

    def plan(self, options: CreateOptions) -> MachinePlan:
        return self._plan(self._resolve(options))

    def plan_update(self, name: str, options: UpdateOptions) -> UpdatePlan:
        return self._update_plan(self.store.load(name), options)

    def _update_plan(self, machine: Machine, options: UpdateOptions) -> UpdatePlan:
        if machine.operation is not Operation.READY:
            raise ValueError(f"machine [{machine.name}] needs recovery before updating")
        plan = plan_update(machine.spec, options)
        self._plan(plan.after)
        if machine.gvisor and plan.effect not in {"none", "metadata"}:
            raise ValueError("gVisor machines support only shell-preference updates")
        if plan.effect == "replace":
            if (
                machine.spec.container_args
                or machine.spec.gpu
                or machine.spec.job_limits
            ):
                raise ValueError(
                    "replacement does not support raw container arguments, GPU or job limits"
                )
            if machine.spec.ssh_agent:
                raise ValueError("replacement does not support SSH-agent forwarding")
            if any(p.address is None for p in plan.after.ports):
                raise ValueError(
                    "replace legacy port rules with explicit addresses before updating"
                )
        return plan

    def update(
        self, name: str, options: UpdateOptions, *, restart: bool = False
    ) -> UpdatePlan:
        with self.store.lock(name):
            machine = self.store.load(name)
            plan = self._update_plan(machine, options)
            if plan.effect == "none":
                return plan
            if plan.effect == "metadata":
                self.store.save(replace(machine, spec=plan.after))
                return plan
            engine = self._engine(machine.spec.engine)
            observed = self._observe(machine, engine)
            if observed is None or observed.state not in {
                RuntimeState.RUNNING,
                RuntimeState.STOPPED,
            }:
                raise ValueError("update requires a bound running or stopped container")
            running = observed.state is RuntimeState.RUNNING
            if plan.effect == "replace":
                if running and not restart:
                    raise ValueError(
                        "this update replaces the container and interrupts sessions; use --restart or stop the machine first"
                    )
                engine.prepare_replacement(machine)
            journal = UpdateJournal(
                machine, plan.after, str(uuid4()), running, plan.effect
            )
            machine = replace(
                machine, operation=Operation.UPDATING, update=journal, error=None
            )
            self.store.save(machine)
            self._continue_update(machine, engine)
            return plan

    def _continue_update(
        self, machine: Machine, engine: Engine, *, recovering: bool = False
    ) -> Machine:
        try:
            journal = machine.update
            before = journal.before
            if journal.effect == "resources":
                after = replace(before, spec=journal.desired)
                engine.update_resources(before, after)
                self.store.save(after)
                return after
            if recovering and journal.stage == "committed" and journal.running:
                observed = self._observe(journal.candidate, engine)
                if observed is not None and _never_started(observed):
                    # Failed Docker network activation can leave a container
                    # that starts on retry without its port forwarding. Only a
                    # candidate proven never to have run may be reallocated.
                    journal = replace(journal, stage="retrying")
                    machine = replace(
                        before, operation=Operation.UPDATING, update=journal
                    )
                    self.store.save(machine)
            if journal.stage == "retrying":
                candidate = journal.candidate
                observed = self._observe(candidate, engine)
                if observed is not None:
                    if not _never_started(observed):
                        raise ValueError(
                            "replacement has run; refusing to discard its filesystem"
                        )
                    engine.delete(candidate)
                    if self._observe(candidate, engine) is not None:
                        raise ValueError("failed candidate removal was not confirmed")
                candidate = replace(
                    candidate,
                    operation=Operation.CREATING,
                    binding=None,
                    creation_context=before.target.context,
                )
                journal = replace(journal, stage="snapshot", candidate=candidate)
                machine = replace(machine, update=journal)
                self.store.save(machine)
            if journal.stage == "prepared":
                observed = self._observe(before, engine)
                if observed is None:
                    raise ValueError(
                        "original container disappeared before preservation"
                    )
                if observed.state is RuntimeState.RUNNING:
                    engine.stop(before)
                elif observed.state is not RuntimeState.STOPPED:
                    raise ValueError("original container state is unknown")
                observed = self._observe(before, engine)
                if observed is None or observed.state is not RuntimeState.STOPPED:
                    raise ValueError("original container did not stop")
                journal = replace(journal, stage="stopped")
                machine = replace(machine, update=journal)
                self.store.save(machine)
            if journal.stage == "stopped":
                rootfs = engine.preserve_rootfs(
                    before, journal.token, self.store.machine_dir(machine.name)
                )
                candidate = Machine(
                    before.id,
                    journal.desired,
                    before.created_at,
                    creation_context=before.target.context,
                    generation=journal.token,
                    rootfs=rootfs,
                )
                journal = replace(journal, stage="snapshot", candidate=candidate)
                machine = replace(machine, update=journal)
                self.store.save(machine)
            if journal.stage == "snapshot":
                candidate = journal.candidate
                # Only this journal's reserved unique name may recover a lost
                # allocation reply. All later operations use its exact ID.
                observed = engine.observe(candidate.backend_name)
                if observed.context != candidate.creation_context:
                    raise ValueError("engine context changed during replacement")
                if isinstance(observed, MissingContainer):
                    allocation = engine.allocate(candidate)
                else:
                    allocation = ContainerAllocation(
                        observed.container_id, observed.context, candidate.labels
                    )
                    observed.verify(allocation)
                candidate = replace(candidate, allocation=allocation)
                journal = replace(journal, stage="allocated", candidate=candidate)
                machine = replace(machine, update=journal)
                self.store.save(machine)
            if journal.stage == "allocated":
                candidate = journal.candidate
                observed = self._observe(candidate, engine)
                if observed is None:
                    raise ValueError("replacement allocation disappeared")
                if (
                    observed.image_id.removeprefix("sha256:")
                    != candidate.rootfs.image_id.removeprefix("sha256:")
                    or observed.runtime != before.binding.runtime
                ):
                    raise ValueError(
                        "replacement image or runtime differs from the preserved binding"
                    )
                candidate = replace(
                    candidate,
                    operation=Operation.READY,
                    allocation=None,
                    creation_context=None,
                    binding=MachineBinding(
                        candidate.target, observed.image_id, observed.runtime
                    ),
                )
                engine.verify_replacement(candidate)
                journal = replace(journal, stage="committed", candidate=candidate)
                machine = replace(
                    candidate, operation=Operation.UPDATING, update=journal
                )
                self.store.save(machine)
            candidate = journal.candidate
            if journal.running:
                self._ensure_running(candidate, engine)
            observed = self._observe(before, engine, strict=False)
            if observed is not None:
                if observed.state is not RuntimeState.STOPPED:
                    raise ValueError(
                        "old allocation unexpectedly running; cleanup refused"
                    )
                engine.delete(before)
                if self._observe(before, engine, strict=False) is not None:
                    raise ValueError("old allocation removal was not confirmed")
            engine.remove_rootfs(before)
            self.store.save(candidate)
            return candidate
        except Exception as exc:
            # Reload the durable journal: never overwrite a later stage with
            # the caller's older in-memory record after a save failure.
            self._remember_error(self.store.load(machine.name), exc)
            raise

    def _plan(self, spec: MachineSpec) -> MachinePlan:
        _validate_create(spec)
        return plan_machine(
            spec,
            self.config.gvisor,
            protected_paths=control_paths(
                self.store.base_dir,
                self.shell_state.base_dir,
            ),
            history_mount=self.shell_state.mount_for(spec.name)
            if spec.shell_state
            else None,
        )

    def start(self, name: str) -> RuntimeState:
        with self.store.lock(name):
            machine = self.store.load(name)
            return self._ensure_running(machine, self._engine(machine.spec.engine))

    def stop(self, name: str) -> RuntimeState:
        with self.store.lock(name):
            machine = self.store.load(name)
            if machine.update:
                raise ValueError(
                    "machine has an interrupted update; run mim recover first"
                )
            engine = self._engine(machine.spec.engine)
            if machine.gvisor:
                engine.stop(machine)
            else:
                observed = self._observe(machine, engine, strict=False)
                if observed is not None and observed.state is RuntimeState.RUNNING:
                    engine.stop(machine)
            observed = self._observe(machine, engine, strict=False)
            if observed is not None and observed.state is not RuntimeState.STOPPED:
                raise ValueError(f"machine [{name}] did not reach a stopped state")
            return RuntimeState.MISSING if observed is None else observed.state

    def delete(self, name: str, *, keep_shell_state: bool = False) -> None:
        with self.store.lock(name):
            machine = self.store.load(name)
            engine = self._engine(machine.spec.engine)
            if machine.update:
                self._delete_update(machine, engine, keep_shell_state=keep_shell_state)
                return
            machine = self._recover_identity_probe(machine, engine)
            if machine.target is None:
                machine = self._recover_allocation(machine, engine)
            remove_container = machine.target is not None and (
                machine.gvisor is not None
                or self._observe(machine, engine, strict=False) is not None
            )
            machine = replace(machine, operation=Operation.DELETING, error=None)
            self.store.save(machine)
            try:
                if remove_container:
                    engine.delete(machine)
                    if self._observe(machine, engine, strict=False) is not None:
                        raise ValueError(
                            "container removal was not confirmed; binding retained"
                        )
                if machine.identity_probe is not None:
                    engine.delete_identity_probe(machine)
                    machine = replace(machine, identity_probe=None)
                    self.store.save(machine)
                if machine.gvisor:
                    engine.release(machine)
                if machine.rootfs:
                    engine.remove_rootfs(machine)
                if not keep_shell_state:
                    self.shell_state.delete(name)
                self.store.delete(name)
            except Exception as exc:
                self._remember_error(machine, exc)
                raise

    def _delete_update(
        self, machine: Machine, engine: Engine, *, keep_shell_state: bool
    ) -> None:
        # Persist deletion intent before removing anything. Recovery must never
        # resume an update whose source or candidate is being deleted.
        machine = replace(machine, operation=Operation.DELETING, error=None)
        self.store.save(machine)
        try:
            journal = machine.update
            candidate = journal.candidate
            if candidate and candidate.target is None:
                observed = engine.observe(candidate.backend_name)
                if observed.context != candidate.creation_context:
                    raise ValueError(
                        "engine context changed during replacement cleanup"
                    )
                if not isinstance(observed, MissingContainer):
                    allocation = ContainerAllocation(
                        observed.container_id, observed.context, candidate.labels
                    )
                    observed.verify(allocation)
                    candidate = replace(candidate, allocation=allocation)
                    journal = replace(journal, candidate=candidate)
                    machine = replace(machine, update=journal)
                    self.store.save(machine)
            for record in (candidate, journal.before):
                if (
                    record
                    and record.target
                    and self._observe(record, engine, strict=False)
                ):
                    engine.delete(record)
                    if self._observe(record, engine, strict=False) is not None:
                        raise ValueError(
                            "container removal was not confirmed; binding retained"
                        )
            if journal.effect == "replace":
                # Import may have succeeded before its reply or journal save.
                if candidate:
                    engine.remove_rootfs(candidate)
                else:
                    engine.remove_rootfs(journal.before, token=journal.token)
            engine.remove_rootfs(journal.before)
            if not keep_shell_state:
                self.shell_state.delete(machine.name)
            self.store.delete(machine.name)
        except Exception as exc:
            self._remember_error(self.store.load(machine.name), exc)
            raise

    def _execution_machine(
        self, name: str, *, memory_mib: int | None = None
    ) -> Machine:
        # Lock preparation, not an interactive session: independent exec/SSH
        # sessions must remain concurrent. Engines recheck binding at dispatch.
        with self.store.lock(name):
            machine = self.store.load(name)
            if memory_mib is not None and not machine.spec.job_limits:
                raise ValueError(
                    "command memory limits require creation with --job-limits"
                )
            self._ensure_running(
                machine, self._engine(machine.spec.engine), verify=False
            )
            return machine

    def enter(self, name: str, shell: str | None = None) -> None:
        machine = self._execution_machine(name)
        spec = machine.spec
        env = list(_session_env(spec))
        for variable in ("TERM", "COLORTERM"):
            if value := os.environ.get(variable):
                env.append(f"{variable}={value}")
        self._engine(spec.engine).exec(
            machine,
            ExecSpec(
                enter_shell_command(shell or spec.shell or self.config.defaults.shell),
                interactive=True,
                tty=True,
                env=tuple(env),
                workdir=_mapped_cwd(spec.mounts) or _session_workdir(spec),
            ),
        )

    def exec(self, name: str, spec: ExecSpec) -> None:
        # Reject unsupported requests before starting a stopped machine.
        machine = self._execution_machine(name, memory_mib=spec.memory_mib)
        self._engine(machine.spec.engine).exec(machine, spec)

    def ssh_session(
        self,
        name: str,
        *,
        original_command: str | None,
        tty: bool,
        term: str | None = None,
    ) -> None:
        machine = self._execution_machine(name)
        spec = machine.spec
        env = (*_session_env(spec), *((f"TERM={term}",) if term else ()))
        command = (
            enter_shell_command(spec.shell or self.config.defaults.shell)
            if original_command is None
            else ("sh", "-c", original_command)
        )
        self._engine(spec.engine).exec(
            machine,
            ExecSpec(
                command,
                interactive=True,
                tty=tty,
                env=env,
                workdir=_session_workdir(spec),
            ),
        )

    def _observe(
        self, machine: Machine, engine: Engine, *, strict: bool = True
    ) -> MachineObservation | None:
        if machine.target is None:
            raise ValueError(
                f"machine [{machine.name}] needs recovery before observation"
            )
        return verify_observation(
            engine.observe(machine.target.container_id),
            machine.target,
            machine.binding if strict else None,
        )

    def _ensure_running(
        self, machine: Machine, engine: Engine, *, verify: bool = True
    ) -> RuntimeState:
        if machine.operation is not Operation.READY:
            raise ValueError(
                f"machine [{machine.name}] is {machine.operation.value}; recover or finish deletion first"
            )
        observed = self._observe(machine, engine)
        if observed is None:
            raise ValueError(
                f"machine [{machine.name}] is missing; its binding has been retained"
            )
        if observed.state is RuntimeState.STOPPED:
            engine.start(machine)
            observed = self._observe(machine, engine)
            if observed is None or observed.state is not RuntimeState.RUNNING:
                raise ValueError(f"machine [{machine.name}] failed to start")
        elif observed.state is not RuntimeState.RUNNING:
            raise ValueError(f"machine [{machine.name}] state is unknown")
        elif machine.gvisor and verify:
            engine.check(machine)
        if machine.error:
            self.store.save(replace(machine, error=None))
        return RuntimeState.RUNNING

    def inspect(self, name: str) -> MachineView:
        with self.store.lock(name):
            machine = self.store.load(name)
            if machine.target is None:
                return MachineView(
                    machine,
                    RuntimeState.UNKNOWN,
                    machine.error or "creation needs recovery",
                )
            snapshot = None
            try:
                snapshot = self._engine(machine.spec.engine).observe(
                    machine.target.container_id
                )
                observed = verify_observation(snapshot, machine.target, machine.binding)
                return MachineView(
                    machine,
                    RuntimeState.MISSING if observed is None else observed.state,
                    machine.error if machine.operation is not Operation.READY else None,
                    snapshot,
                )
            except (ValueError, RuntimeError, OSError) as exc:
                return MachineView(machine, RuntimeState.UNKNOWN, str(exc), snapshot)

    def list(self) -> list[MachineSummary]:
        rows = []
        for name in self.store.names():
            try:
                rows.append(MachineSummary(name, self.inspect(name)))
            except (ValueError, KeyError, TypeError, OSError) as exc:
                # Listing can explain an unreadable record without decoding an
                # older execution model or hiding other healthy machines.
                rows.append(MachineSummary(name, problem=str(exc)))
        return rows

    def _remember_error(self, machine: Machine, error: Exception) -> None:
        try:
            self.store.save(replace(machine, error=str(error)))
        except Exception as exc:
            logger.warning(
                "could not persist recovery state for [%s]: %s", machine.name, exc
            )

    def _engine(self, name: str) -> Engine:
        return self.engines.get(name) or get_engine(name)

    def _resolve(self, options: CreateOptions) -> MachineSpec:
        profile = load_profile(self.config, options.profile)
        image = _first(options.image, profile.image)
        if image is None:
            raise ValueError("image is required")

        runner = validate_runner(
            _first(
                options.runner,
                profile.runner,
                self.config.defaults.runner,
            )
        )
        network = _first(
            options.network,
            profile.network,
            self.config.defaults.network,
        )
        resources = _resource_spec(options, profile, self.config.defaults)
        isolation = _first(
            options.isolation, profile.isolation, self.config.defaults.isolation
        )
        mounts = _mounts(options, profile)
        shell_state_enabled = _bool_option(
            options.shell_state, profile.shell_state, True
        )
        if shell_state_enabled:
            mounts = (*mounts, self.shell_state.mount_for(options.name))

        return MachineSpec(
            name=options.name,
            image=image,
            engine=runner,
            isolation=isolation,
            mounts=mounts,
            ports=_ports(options, profile),
            env=_env(options, profile),
            workdir=_first(options.workdir, profile.workdir),
            shell=normalize_shell(_first(options.shell, profile.shell)),
            network=network,
            identity=_first(
                options.identity,
                profile.identity,
                IdentityMode.IMAGE,
            ),
            resources=resources,
            shell_state=shell_state_enabled,
            ssh_agent=_bool_option(
                options.ssh_agent,
                profile.ssh_agent,
                False,
            ),
            gpu=_bool_option(
                options.gpu,
                profile.gpu,
                False,
            ),
            job_limits=_bool_option(options.job_limits, profile.job_limits, False),
            container_args=_container_args(options, profile),
        )


def _validate_create(spec: MachineSpec) -> None:
    validate_mounts_ports(spec)
    if spec.job_limits and spec.engine != "podman":
        raise ValueError(
            "job limits require Podman on native Linux with cgroup v2 and crun"
        )
    if spec.ports and spec.network is NetworkMode.HOST:
        raise ValueError("port publishing cannot be used with host networking")
    if spec.gpu and spec.engine != "podman":
        raise ValueError(f"engine [{spec.engine}] does not support GPU forwarding")


def _session_env(spec: MachineSpec) -> tuple[str, ...]:
    return (
        f"MIM_MACHINE={spec.name}",
        f"MIM_RUNNER={spec.engine}",
        f"MIM_SHELL_STATE={int(spec.shell_state)}",
    )


def _mounts(options: CreateOptions, profile: Profile) -> tuple[MountSpec, ...]:
    resolved = {}
    for layer in (profile, options):
        mounts = [parse_workspace_spec(v) for v in layer.workspaces]
        for value in layer.home_shares:
            mounts.extend(parse_home_share_spec(value))
        mounts.extend(parse_mount_spec(v) for v in layer.mounts)
        seen = set()
        for mount in mounts:
            if mount.target in seen:
                raise ValueError(f"duplicate mount destination: {mount.target}")
            seen.add(mount.target)
            if mount.target in resolved and resolved[mount.target].kind == "workspace":
                if not mount.source.is_dir():
                    raise ValueError("workspace source must be a directory")
                mount = replace(mount, kind="workspace")
            resolved[mount.target] = mount
    return tuple(resolved.values())


def _ports(options: CreateOptions, profile: Profile) -> tuple[PortBind, ...]:
    ports = {}
    for values in (profile.ports, options.ports):
        seen = set()
        for value in values:
            port = explicit_port(value)
            key = port_key(port)
            if key in seen:
                raise ValueError("duplicate published host listener")
            seen.add(key)
            ports[key] = port
    return tuple(ports.values())


def _env(options: CreateOptions, profile: Profile) -> tuple[str, ...]:
    env = {}
    for values in (profile.env, options.env):
        seen = set()
        for value in values:
            key = value.partition("=")[0]
            if key in seen:
                raise ValueError(f"duplicate environment override: {key}")
            seen.add(key)
            env[key] = value
    return tuple(env.values())


def _container_args(options: CreateOptions, profile: Profile) -> tuple[str, ...]:
    return profile.container_args + options.container_args


def _resource_spec(
    options: CreateOptions,
    profile: Profile,
    defaults: Defaults,
) -> ResourceSpec:
    return ResourceSpec(
        cpus=_first(
            options.cpus,
            profile.resources.cpus,
            defaults.resources.cpus,
        ),
        memory_mib=_first(
            options.memory_mib,
            profile.resources.memory_mib,
            defaults.resources.memory_mib,
        ),
        swap_mib=_first(
            options.swap_mib, profile.resources.swap_mib, defaults.resources.swap_mib
        ),
        tasks=_first(options.tasks, profile.resources.tasks, defaults.resources.tasks),
    )


def _first(*values: T | None) -> T | None:
    return next((value for value in values if value is not None), None)


def _bool_option(
    value: bool | None,
    profile_value: bool | None,
    default: bool,
) -> bool:
    if value is not None:
        return value
    if profile_value is not None:
        return profile_value
    return default


def _mapped_cwd(mounts: Iterable[MountSpec]) -> str | None:
    return map_host_path_to_guest(Path.cwd(), tuple(mounts))


def _session_workdir(spec: MachineSpec) -> str | None:
    return spec.workdir or next(
        (mount.target for mount in spec.mounts if mount.kind == "workspace"),
        None,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _never_started(observed: MachineObservation) -> bool:
    started = json.loads(observed.configuration).get("started_at")
    return (
        observed.state is RuntimeState.STOPPED
        and isinstance(started, str)
        and started.startswith("0001-01-01T")
    )
