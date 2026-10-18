"""Pure isolation policy, grant validation and offline command templates."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import GvisorRuntime
from .domain import IdentityMode, IsolationMode, MachineSpec, MountSpec, NetworkMode
from .engines.commands import create_command
from .paths import cache_dir, config_dir, data_dir

PASTA_NETWORK = "--network=pasta:--no-splice"


def control_paths(*additional: Path) -> tuple[Path, ...]:
    return (
        config_dir(),
        data_dir(),
        cache_dir(),
        Path(__file__).resolve().parent,
        Path(sys.prefix),
        Path(sys.base_prefix),
        *(Path(directory).absolute() for directory in os.get_exec_path()),
        config_dir().parent / "containers",
        data_dir().parent / "containers",
        Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"),
        *additional,
    )


CAPABILITIES = frozenset(
    f"CAP_{name}"
    for name in (
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
        "FSETID",
        "KILL",
        "NET_BIND_SERVICE",
        "SETFCAP",
        "SETGID",
        "SETPCAP",
        "SETUID",
        "SYS_CHROOT",
    )
)


def gvisor_create_args(unit: str) -> tuple[str, ...]:
    return (
        "--cgroupns=private",
        "--cgroups=enabled",
        "--cgroup-parent",
        unit,
        "--pid=private",
        "--ipc=private",
        "--uts=private",
        "--systemd=false",
        "--security-opt=no-new-privileges",
        "--security-opt=label=disable",
        "--security-opt=apparmor=unconfined",
        "--http-proxy=false",
        "--image-volume=ignore",
        "--pull=never",
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
    def profile(self) -> str:
        network = "offline" if self.network is NetworkMode.NONE else "online"
        return network + ("-agent" if self.ssh_agent else "")

    @property
    def entrypoint(self) -> str:
        root = (
            str(self.runtime.path.parent) if self.runtime.path else "<gvisor-directory>"
        )
        return f"{root}/profiles/{self.profile}/runsc"

    @property
    def prefix(self) -> tuple[str, ...]:
        return (
            "podman",
            "--remote=false",
            "--cgroup-manager=systemd",
            "--runtime",
            self.entrypoint,
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
            "status": "preview",
            "execution_enabled": False,
            "isolation": self.spec.isolation.value,
            "machine": spec,
            "runtime": None
            if self.policy is None
            else {
                "path": None
                if self.policy.runtime.path is None
                else str(self.policy.runtime.path),
                "profile": self.policy.profile,
                "entrypoint": self.policy.entrypoint,
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
    validate_gvisor(
        spec, runtime, protected_paths=protected_paths, history_mount=history_mount
    )
    policy = GvisorPolicy(runtime, spec.network, spec.ssh_agent)
    engine_args = (
        *gvisor_create_args("mim<machine-uuid>.slice"),
        "--tz=local",
        *((PASTA_NETWORK,) if spec.network is NetworkMode.DEFAULT else ()),
    )
    feature_args = _ssh_agent_template() if spec.ssh_agent else ()
    command = create_command(
        spec,
        prefix=(
            *policy.prefix,
            "--hooks-dir",
            "<control-dir>/hooks",
            "--default-mounts-file",
            "<control-dir>/mounts.conf",
        ),
        identity_args=_identity_template(spec),
        engine_args=engine_args,
        feature_args=feature_args,
        publish_address="127.0.0.1",
        relabel_shell_state=False,
        labels=(("mimchine.id", "<machine-id>"), ("mimchine.role", "machine")),
    )
    requirements = _gvisor_requirements()
    if runtime.path is None:
        requirements = (
            Requirement(
                "runtime-path",
                "Discover an installed Mim gVisor release in /usr/lib/mim-gvisor, or configure an absolute [runtimes.gvisor].path.",
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


def validate_gvisor(
    spec: MachineSpec,
    runtime: GvisorRuntime,
    *,
    protected_paths: tuple[Path, ...],
    history_mount: MountSpec | None = None,
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
    if spec.ports and spec.network is NetworkMode.NONE:
        raise ValueError("gVisor port publishing requires networking")
    targets = [mount.target for mount in spec.mounts]
    if spec.ssh_agent:
        targets.append("/mim/ssh-agent.sock")
    if len(targets) != len(set(targets)):
        raise ValueError("gVisor filesystem grant destinations must be unique")
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
        target = Path(mount.target)
        if (
            ".." in target.parts
            or str(target) != mount.target.rstrip("/")
            or target == Path("/")
            or any(
                target == kernel or target.is_relative_to(kernel)
                for kernel in (Path("/dev"), Path("/proc"), Path("/sys"))
            )
        ):
            raise ValueError(
                f"gVisor isolation excludes kernel or noncanonical mount targets: {mount.target}"
            )
        if set(mount.options) - {"z", "Z"}:
            raise ValueError("Mim's gVisor mode accepts only z/Z mount options")
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
            engine_args=("--tz=local",) if spec.engine == "podman" else (),
            feature_args=features,
            labels=(("mimchine.id", "<machine-id>"),),
        ),
        lifecycle=_lifecycle_templates(prefix, "<container-id>"),
        requirements=tuple(requirements),
    )


def _identity_template(spec: MachineSpec) -> tuple[str, ...]:
    if spec.isolation is IsolationMode.GVISOR:
        if spec.identity is IdentityMode.ROOT:
            return ("--userns=host", "--user=0:0")
        uid, gid = (
            ("<host-uid>", "<host-gid>")
            if spec.identity is IdentityMode.HOST
            else ("<image-uid>", "<image-gid>")
        )
        return (f"--userns=keep-id:uid={uid},gid={gid}", f"--user={uid}:{gid}")
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
            "Local rootless Podman on Linux, delegated cgroup v2, systemd 254+, and pasta with --no-splice are required.",
        ),
        Requirement(
            "distribution",
            "Verify the installed Mim gVisor distribution manifest, sidecars and selected fixed runtime profile; bind its canonical path and manifest digest.",
        ),
        Requirement(
            "runtime-policy",
            "Use a fixed runtime profile and private engine policy; verify effective privileges, namespaces, devices, hooks and filesystem grants.",
        ),
        Requirement(
            "identity",
            "Resolve image UID/GID from initialized OCI metadata before running image code.",
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
            "Verify machine cgroup ownership and every requested resource ceiling covering Sentry, Gofer, networking and concurrent exec helpers before dispatch.",
        ),
        Requirement(
            "lifecycle",
            "Podman retains writable rootfs storage across stop/start; host-side workspace changes remain visible. Filesystem watchers may need polling.",
        ),
        Requirement(
            "authority",
            "Audit writable workspaces, credentials, network destinations and forwarded sockets as explicit host authority. Netstack is not an egress allowlist; NoNewPrivileges disables guest sudo/setuid elevation.",
        ),
    )
