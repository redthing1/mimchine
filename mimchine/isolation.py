"""Offline isolation-policy compiler.

Plans are review artifacts, never executable capabilities. In particular, a
gVisor plan cannot authorize a launch until the engine/binding and host-cgroup
verification described in its requirements have been implemented and qualified.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import GvisorRuntime
from .domain import IdentityMode, IsolationMode, MachineSpec, MountSpec, NetworkMode
from .engines.commands import create_command

GVISOR_NOT_QUALIFIED = (
    "gVisor execution is not enabled: runtime and host enforcement require qualification; "
    "use --isolation gvisor --plan to inspect the candidate policy"
)


@dataclass(frozen=True)
class Requirement:
    name: str
    detail: str

    def to_data(self) -> dict[str, str]:
        return {"name": self.name, "detail": self.detail}


@dataclass(frozen=True)
class GvisorPolicy:
    """Settings owned by mim, distinct from operator-supplied installation paths."""

    runtime: GvisorRuntime
    network: NetworkMode
    ssh_agent: bool

    @property
    def flags(self) -> tuple[str, ...]:
        return (
            "platform=systrap",
            f"network={'none' if self.network is NetworkMode.NONE else 'sandbox'}",
            "directfs=false",
            "overlay2=none",
            "file-access-mounts=shared",
            "ignore-cgroups=false",
            "host-settings=check",
            "allow-flag-override=false",
            "allow-suid=false",
            "net-raw=false",
            "host-fifo=none",
            "character-device-policy=emulated-only",
            f"host-uds={'open' if self.ssh_agent else 'none'}",
        )

    @property
    def prefix(self) -> tuple[str, ...]:
        path = str(self.runtime.path) if self.runtime.path else "<runsc-path>"
        return (
            "podman",
            "--remote=false",
            "--runtime",
            path,
            *(f"--runtime-flag={flag}" for flag in self.flags),
        )


@dataclass(frozen=True)
class MachinePlan:
    spec: MachineSpec
    create: tuple[str, ...]
    lifecycle: tuple[tuple[str, tuple[str, ...]], ...]
    requirements: tuple[Requirement, ...]
    policy: GvisorPolicy | None = None

    def to_data(self) -> dict[str, Any]:
        spec = self.spec.to_data()
        return {
            "plan_version": 1,
            "status": "unqualified" if self.policy else "preview",
            "execution_enabled": False,
            "isolation": self.spec.isolation.value,
            "machine": spec,
            "runtime": None
            if self.policy is None
            else {
                "path": None
                if self.policy.runtime.path is None
                else str(self.policy.runtime.path),
                "flags": list(self.policy.flags),
            },
            "command_templates": {
                "create": list(self.create),
                **{action: list(argv) for action, argv in self.lifecycle},
            },
            "requirements": [
                requirement.to_data() for requirement in self.requirements
            ],
        }


def plan_machine(
    spec: MachineSpec,
    runtime: GvisorRuntime,
    *,
    protected_paths: tuple[Path, ...],
    history_mount: MountSpec | None = None,
) -> MachinePlan:
    """Compile known intent; do not infer host capabilities or execute probes."""
    isolation = spec.isolation
    if isolation is IsolationMode.NATIVE:
        return _native_plan(spec)
    _validate_gvisor(spec, runtime, protected_paths, history_mount)
    policy = GvisorPolicy(runtime, spec.network, spec.ssh_agent)
    engine_args = (
        "--tz=local",
        "--pids-limit=1024",
        "--security-opt=no-new-privileges",
        # runsc currently rejects an OCI SELinux process label. This is local
        # to this candidate container; do not change host LSM configuration.
        "--security-opt=label=disable",
        *(("--network=pasta",) if spec.network is NetworkMode.DEFAULT else ()),
    )
    feature_args = _ssh_agent_template() if spec.ssh_agent else ()
    command = create_command(
        spec,
        prefix=policy.prefix,
        identity_args=_identity_template(spec),
        engine_args=engine_args,
        feature_args=feature_args,
        publish_address="127.0.0.1",
        relabel_shell_state=False,
        labels=(("mimchine.id", "<machine-id>"),),
    )
    requirements = _gvisor_requirements()
    if runtime.path is None:
        requirements = (
            Requirement(
                "runtime-path",
                "Configure an absolute [runtimes.gvisor].path to an installed runsc distribution.",
            ),
            *requirements,
        )
    if spec.ssh_agent:
        requirements += (
            Requirement(
                "host-socket-grant",
                "SSH-agent forwarding enables host-uds=open for all reachable host sockets; "
                "a read-only socket mount does not restrict agent operations. Audit every bind mount.",
            ),
        )
    return MachinePlan(
        spec=spec,
        create=command,
        lifecycle=_lifecycle_templates(policy.prefix, "<container-id>"),
        requirements=requirements,
        policy=policy,
    )


def _validate_gvisor(
    spec: MachineSpec,
    runtime: GvisorRuntime,
    protected_paths: tuple[Path, ...],
    history_mount: MountSpec | None,
) -> None:
    if spec.engine != "podman":
        raise ValueError("gVisor isolation requires rootless Podman on native Linux")
    if spec.gpu:
        raise ValueError("gVisor isolation excludes GPU forwarding")
    if spec.job_limits:
        raise ValueError(
            "gVisor isolation cannot enforce the existing per-command cgroup memory limits"
        )
    if spec.network is NetworkMode.HOST:
        raise ValueError("gVisor isolation excludes host networking")
    if spec.container_args:
        raise ValueError(
            "gVisor isolation excludes raw container arguments; use typed machine options"
        )
    runtime_paths = (
        ()
        if runtime.path is None
        else (
            runtime.path.parent.resolve(),
            runtime.path.resolve().parent,
        )
    )
    for mount in spec.mounts:
        source = mount.source.resolve()
        if set(mount.options) - {"z", "Z"}:
            raise ValueError(
                "gVisor isolation supports only z/Z mount options in this prototype"
            )
        if (
            source.is_relative_to("/dev")
            or source.is_relative_to("/proc")
            or source.is_relative_to("/sys")
        ):
            raise ValueError(
                f"gVisor isolation excludes host kernel/device mounts: {mount.source}"
            )
        # A symlinked runtime's entry directory and actual distribution both
        # matter. The history grant never grants access to either one.
        for path in runtime_paths:
            if source.is_relative_to(path) or path.is_relative_to(source):
                raise ValueError(
                    f"mount [{mount.source}] overlaps protected control path [{path}]"
                )
        # The exact generated history grant may cross its own control parents,
        # but cannot redirect to or contain another protected control path.
        history_grant = mount == history_mount and not mount.source.is_symlink()
        for control in protected_paths:
            path = control.resolve()
            if source.is_relative_to(path) or path.is_relative_to(source):
                if (
                    history_grant
                    and mount.source.is_relative_to(control.absolute())
                    and source.is_relative_to(path)
                ):
                    continue
                raise ValueError(
                    f"mount [{mount.source}] overlaps protected control path [{path}]"
                )


def _native_plan(spec: MachineSpec) -> MachinePlan:
    prefix = (
        (spec.engine, "--remote=false") if spec.engine == "podman" else (spec.engine,)
    )
    features: tuple[str, ...] = ()
    requirements = [
        Requirement(
            "preview",
            "Templates do not probe the host, resolve image identities, or authorize execution.",
        )
    ]
    if spec.ssh_agent:
        features += _ssh_agent_template()
    if spec.gpu:
        features += ("<gpu-arguments>",)
        requirements.append(
            Requirement("gpu-discovery", "Resolve native GPU devices before creation.")
        )
    if spec.job_limits:
        features += ("<job-limit-arguments>",)
        requirements.append(
            Requirement(
                "job-limits",
                "Qualify cgroup v2, systemd and crun; provision the job-limit helper.",
            )
        )
    return MachinePlan(
        spec=spec,
        create=create_command(
            spec,
            prefix=prefix,
            identity_args=_identity_template(spec),
            engine_args=("--tz=local", "--pids-limit=-1")
            if spec.engine == "podman"
            else (),
            feature_args=features,
            labels=(("mimchine.id", "<machine-id>"),),
        ),
        lifecycle=_lifecycle_templates(prefix, "<container-id>"),
        requirements=tuple(requirements),
    )


def _identity_template(spec: MachineSpec) -> tuple[str, ...]:
    if spec.identity is IdentityMode.ROOT:
        return ("--user", "0:0")
    if spec.engine == "podman":
        mapping = (
            "keep-id"
            if spec.identity is IdentityMode.HOST
            else "keep-id:uid=<image-uid>,gid=<image-gid>"
        )
        return ("--userns", mapping)
    if spec.identity is IdentityMode.HOST:
        return ("--user", "<host-uid>:<host-gid>")
    return ()


def _ssh_agent_template() -> tuple[str, ...]:
    return (
        "-v",
        "<ssh-auth-sock>:/mim/ssh-agent.sock:ro",
        "-e",
        "SSH_AUTH_SOCK=/mim/ssh-agent.sock",
    )


def _lifecycle_templates(
    prefix: tuple[str, ...], target: str
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple(
        (action, (*prefix, *args))
        for action, args in (
            ("init", ("init", target)),
            ("start", ("start", target)),
            ("inspect", ("inspect", target)),
            ("exec", ("exec", target, "<command>")),
            ("stop", ("stop", target)),
            ("delete", ("rm", "-f", target)),
        )
    )


def _gvisor_requirements() -> tuple[Requirement, ...]:
    return (
        Requirement(
            "host",
            "Qualify native Linux, local rootless Podman, UID/GID mappings, pasta and delegated cgroup v2 with the user systemd manager. Remote Podman is excluded.",
        ),
        Requirement(
            "distribution",
            "Pin and verify the installed runsc distribution, including sidecars; qualify rootless systemd cgroup support, potentially with an upstream patch set.",
        ),
        Requirement(
            "runtime-policy",
            "Bind the runtime artifact and fixed flags to the machine across every lifecycle call. Qualify ambient containers.conf, image annotations, OCI hooks, devices, mounts and namespace defaults against the effective OCI spec.",
        ),
        Requirement(
            "identity",
            "Resolve image UID/GID without executing an untrusted image through a native runtime. Any execution-based probe must use the same qualified isolation policy.",
        ),
        Requirement(
            "binding",
            "Capture full container ID and machine ownership label at create; persist engine context and policy identity. Validate observation before start/exec/stop/delete; reject name reuse.",
        ),
        Requirement(
            "prestart",
            "Create -> bind ID -> podman init -> verify effective OCI spec and host cgroups -> start -> verify running. Repeat checks after every OCI reinitialization; init itself is trusted runtime setup.",
        ),
        Requirement(
            "resources",
            "Verify host memory.max/cpu.max and coverage of Sentry, Gofer and helpers. Qualify enforcement and failure paths in a disposable environment; guest cgroup CPU/memory writes are not evidence.",
        ),
        Requirement(
            "lifecycle",
            "Qualify writable rootfs persistence, host-edited binds, stop/start, concurrent operations and crash recovery. Rootfs overlay is owned by Podman; host-side watcher notifications may require polling.",
        ),
        Requirement(
            "authority",
            "Audit writable workspaces, credentials, network destinations and forwarded sockets as explicit host authority. Netstack is not an egress allowlist; NoNewPrivileges disables guest sudo/setuid elevation.",
        ),
    )
