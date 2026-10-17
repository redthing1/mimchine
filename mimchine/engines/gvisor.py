"""Apply and verify gVisor policy within Podman's ordinary object lifecycle."""

from __future__ import annotations

import json
import os
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path

from ..config import GvisorRuntime
from ..domain import (
    IdentityMode,
    ImageUser,
    Machine,
    MachineObservation,
    MountSpec,
    NetworkMode,
)
from ..gvisor import verify_runtime
from ..isolation import CAPABILITIES, control_paths, validate_gvisor
from ..process import ProcessResult, ProcessRunner
from ..resources import SystemdBudget
from ..scope import cgroup_path, check_limits
from ..state import atomic_write, ensure_directory
from .lifecycle import IDENTITY_PROBE_ARGV, KEEPALIVE_ARGV

ENGINE_POLICY = (
    "[containers]\ndefault_capabilities = [\n"
    + "".join(f'  "{name.removeprefix("CAP_")}",\n' for name in sorted(CAPABILITIES))
    + """  {append=false},
]
annotations = [{append=false}]
devices = [{append=false}]
volumes = [{append=false}]
mounts = [{append=false}]
env = [{append=false}]
env_host = false
http_proxy = false
privileged = false
init = false
read_only = false
cgroup_conf = [{append=false}]
default_sysctls = [{append=false}]
apparmor_profile = "unconfined"

[engine]
env = [{append=false}]
hooks_dir = [{append=false}]

[engine.runtimes_flags]
runsc = []

[network]
pasta_options = [{append=false}]
"""
)


class GvisorSandbox:
    def __init__(
        self, process: ProcessRunner, machine: Machine, *, probe: bool = False
    ):
        if machine.gvisor is None:
            raise ValueError("gVisor preparation requires a bound distribution")
        self.process, self.machine, self.probe = process, machine, probe
        self.binding = machine.gvisor
        self.budget = SystemdBudget(process)
        self._verified = False

    @property
    def prefix(self) -> tuple[str, ...]:
        profile = "offline" if self.probe else self.binding.profile
        return (
            "podman",
            "--remote=false",
            "--cgroup-manager=systemd",
            "--runtime",
            str(self.binding.directory / "profiles" / profile / "runsc"),
            "--hooks-dir",
            str(self.binding.control_dir / "hooks"),
            "--default-mounts-file",
            str(self.binding.control_dir / "mounts.conf"),
        )

    @property
    def env(self) -> dict[str, str]:
        return {
            "CONTAINERS_CONF_OVERRIDE": str(
                self.binding.control_dir / "containers.conf"
            )
        }

    def distribution(self) -> None:
        if not self._verified:
            verify_runtime(
                self.binding.directory / "runsc",
                self.binding.profile,
                expected_manifest=self.binding.manifest_sha256,
            )
            self._verified = True

    def control_files(self) -> None:
        directory = self.binding.control_dir
        for path in (directory, directory / "hooks"):
            ensure_directory(path)
            metadata = path.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or path.resolve() != path
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077
            ):
                raise ValueError(
                    f"gVisor control directory must be private and owned: {path}"
                )
        if any((directory / "hooks").iterdir()):
            raise ValueError("gVisor OCI hook directory must be empty")
        for name, content in (
            ("containers.conf", ENGINE_POLICY.encode()),
            ("mounts.conf", b""),
        ):
            path = directory / name
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                atomic_write(path, content)
                continue
            if (
                not stat.S_ISREG(metadata.st_mode)
                or path.resolve() != path
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077
                or metadata.st_nlink != 1
            ):
                raise ValueError(
                    f"gVisor control file must be private, owned and singly linked: {path}"
                )
            # These files are generated inputs to the current Mim policy.
            # Updating them never substitutes for checking the effective OCI spec.
            if path.read_bytes() != content:
                atomic_write(path, content)

    def validate_grants(self) -> None:
        context = (
            self.machine.target.context
            if self.machine.target
            else self.machine.creation_context
        )
        paths = dict(context.identity)
        validate_gvisor(
            self.machine.spec,
            GvisorRuntime(self.binding.directory / "runsc"),
            protected_paths=control_paths(
                self.binding.control_dir.parent.parent,
                Path(paths["storage"]),
                Path(paths["run_root"]),
            ),
            history_mount=MountSpec(
                self.binding.history_source, "/mim/shell-state", kind="shell_state"
            )
            if self.binding.history_source
            else None,
        )

    def prepare(self) -> None:
        self.distribution()
        self.control_files()
        self.validate_grants()
        self.budget.ensure(self.machine)

    @contextmanager
    def grants(self):
        """Hold each authorized source open until its actual Gofer mount is checked."""
        sources = (
            {}
            if self.probe
            else {mount.target: mount.source for mount in self.machine.spec.mounts}
        )
        if not self.probe and self.binding.agent_socket:
            sources["/mim/ssh-agent.sock"] = self.binding.agent_socket
        with ExitStack() as stack:
            identities = {}
            for target, source in sources.items():
                if source.resolve(strict=True) != source:
                    raise ValueError(
                        f"filesystem grant is no longer canonical: {source}"
                    )
                descriptor = os.open(source, os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW)
                stack.callback(os.close, descriptor)
                if Path(os.readlink(f"/proc/self/fd/{descriptor}")) != source:
                    raise ValueError(
                        f"filesystem grant changed while opening its source: {source}"
                    )
                metadata = os.fstat(descriptor)
                if stat.S_ISLNK(metadata.st_mode):
                    raise ValueError(
                        f"filesystem grant became a symbolic link: {source}"
                    )
                identities[target] = (metadata.st_dev, metadata.st_ino)
            self.validate_grants()
            yield identities

    def run(
        self, args: tuple[str, ...], *, scoped: bool = True, **kwargs
    ) -> ProcessResult:
        command = (*self.prefix, *args)
        return self.process.run(
            self.budget.command(self.machine, command) if scoped else command,
            env=self.env,
            cwd="/",
            **kwargs,
        )

    def identity_args(self) -> tuple[str, ...]:
        spec = self.machine.spec
        if spec.identity is IdentityMode.ROOT:
            return ("--userns=host", "--user=0:0")
        user = (
            ImageUser(os.getuid(), os.getgid())
            if spec.identity is IdentityMode.HOST
            else self.binding.image_user
        )
        if user is None:
            raise ValueError("image identity has not been prepared")
        return (
            f"--userns=keep-id:uid={user.uid},gid={user.gid}",
            f"--user={user.uid}:{user.gid}",
        )

    def verify_object(self, observed: MachineObservation) -> dict:
        boundary = json.loads(observed.configuration)["boundary"]
        expected_runtime = (
            self.binding.directory
            / "profiles"
            / ("offline" if self.probe else self.binding.profile)
            / "runsc"
        )
        if observed.runtime != str(expected_runtime) or observed.image_id.removeprefix(
            "sha256:"
        ) != self.binding.image_id.removeprefix("sha256:"):
            raise ValueError(
                "prepared container image or runtime differs from the bound policy"
            )
        if boundary["privileged"] is not False or boundary["devices"] != []:
            raise ValueError("gVisor excludes privileged execution and host devices")
        return boundary

    def verify(
        self, observed: MachineObservation, grants: dict[str, tuple[int, int]]
    ) -> ImageUser:
        boundary = self.verify_object(observed)
        context = dict(observed.context.identity)
        static_dir = (
            Path(context["storage"])
            / "overlay-containers"
            / observed.container_id
            / "userdata"
        )
        config_path = Path(boundary["oci_config"])
        if config_path != static_dir / "config.json":
            raise ValueError("OCI configuration is outside the owned container state")
        effective = json.loads(config_path.read_text())
        self._verify_spec(effective, observed)
        owner = self.budget.verify(self.machine)
        for key in ("pid", "conmon_pid"):
            pid = boundary[key]
            if (
                type(pid) is not int
                or pid <= 0
                or not cgroup_path(pid).is_relative_to(owner)
            ):
                raise ValueError(
                    f"container {key} is outside the machine resource owner"
                )
            uid = next(
                line
                for line in Path(f"/proc/{pid}/status").read_text().splitlines()
                if line.startswith("Uid:")
            )
            if "0" in uid.split()[1:]:
                raise ValueError("gVisor host process has root identity")
        leaf = cgroup_path(boundary["pid"])
        check_limits(
            leaf,
            self.machine.spec.resources.cpus,
            self.machine.spec.resources.memory_mib,
        )
        gofers, pasta = [], []
        for file in owner.rglob("cgroup.procs"):
            for member in file.read_text().split():
                try:
                    proc = Path("/proc") / member
                    args = (proc / "cmdline").read_bytes().split(b"\0")
                    if b"gofer" in args and cgroup_path(int(member)) == leaf:
                        gofers.append(int(member))
                    if (
                        (proc / "comm")
                        .read_text()
                        .strip()
                        .startswith(("pasta", "passt"))
                    ):
                        if b"--no-splice" not in args:
                            raise ValueError(
                                "pasta must deliver packets to gVisor's network stack"
                            )
                        pasta.append(int(member))
                except FileNotFoundError:
                    continue
        if len(gofers) != 1 or (
            not self.probe
            and self.machine.spec.network is NetworkMode.DEFAULT
            and not pasta
        ):
            raise ValueError(
                "required Gofer or networking helper is absent from the machine budget"
            )
        for pid in (boundary["pid"], gofers[0]):
            verify_uid_map(Path(f"/proc/{pid}/uid_map").read_text(), context["uid_map"])
        for target, expected in grants.items():
            mounted = Path(f"/proc/{gofers[0]}/root") / target.lstrip("/")
            actual = mounted.stat()
            if (actual.st_dev, actual.st_ino) != expected:
                raise ValueError(
                    f"Gofer mount differs from the authorized source: {target}"
                )
        self.validate_grants()
        user = effective["process"]["user"]
        return ImageUser(user["uid"], user["gid"])

    def _verify_spec(self, effective: dict, observed: MachineObservation) -> None:
        process, linux = effective["process"], effective["linux"]
        if (
            process.get("noNewPrivileges") is not True
            or process.get("selinuxLabel")
            or process.get("apparmorProfile") not in (None, "", "unconfined")
        ):
            raise ValueError("OCI privilege policy differs from gVisor requirements")
        if any((effective.get("hooks") or {}).values()):
            raise ValueError("gVisor excludes OCI hooks")
        for name, values in process.get("capabilities", {}).items():
            if not set(values) <= CAPABILITIES or (
                process["user"]["uid"] != 0 and name != "bounding" and values
            ):
                raise ValueError("OCI configuration grants unexpected capabilities")
        expected = list(IDENTITY_PROBE_ARGV if self.probe else KEEPALIVE_ARGV)
        if (
            process["args"] != expected
            or linux["cgroupsPath"]
            != f"{self.machine.resource_unit}:libpod:{observed.container_id}"
        ):
            raise ValueError(
                "OCI command or resource parent differs from machine intent"
            )
        namespaces = {item["type"]: item for item in linux["namespaces"]}
        if not {"pid", "network", "ipc", "uts", "mount", "cgroup"} <= namespaces.keys():
            raise ValueError(
                "gVisor requires private process, network, IPC, UTS, mount and cgroup namespaces"
            )
        if any(item.get("path") for item in namespaces.values()):
            raise ValueError("gVisor excludes shared host namespaces")
        devices = {
            "/dev/null": (1, 3),
            "/dev/zero": (1, 5),
            "/dev/full": (1, 7),
            "/dev/random": (1, 8),
            "/dev/urandom": (1, 9),
            "/dev/tty": (5, 0),
        }
        for item in linux.get("devices") or []:
            if item.get("type") != "c" or devices.get(item["path"]) != (
                item["major"],
                item["minor"],
            ):
                raise ValueError("gVisor excludes custom OCI devices")
        self._verify_mounts(effective["mounts"], observed)
        if not self.probe:
            expected_user = (
                ImageUser(0, 0)
                if self.machine.spec.identity is IdentityMode.ROOT
                else ImageUser(os.getuid(), os.getgid())
                if self.machine.spec.identity is IdentityMode.HOST
                else self.binding.image_user
            )
            if (
                ImageUser(process["user"]["uid"], process["user"]["gid"])
                != expected_user
            ):
                raise ValueError("OCI user differs from the prepared machine identity")

    def _verify_mounts(self, mounts: list[dict], observed: MachineObservation) -> None:
        expected = (
            {}
            if self.probe
            else {
                mount.target: (mount.source.resolve(), mount.read_only)
                for mount in self.machine.spec.mounts
            }
        )
        if not self.probe and self.binding.agent_socket:
            expected["/mim/ssh-agent.sock"] = (self.binding.agent_socket, True)
        context = dict(observed.context.identity)
        roots = tuple(
            Path(context[key])
            / "overlay-containers"
            / observed.container_id
            / "userdata"
            for key in ("storage", "run_root")
        )
        managed = {
            "/etc/hosts": "hosts",
            "/etc/hostname": "hostname",
            "/etc/resolv.conf": "resolv.conf",
            "/run/.containerenv": ".containerenv",
            "/dev/shm": "shm",
        }
        virtual = {
            "/proc": "proc",
            "/dev": "tmpfs",
            "/sys": "sysfs",
            "/dev/pts": "devpts",
            "/dev/mqueue": "mqueue",
            "/sys/fs/cgroup": "cgroup",
        }
        seen = set()
        for mount in mounts:
            target, source = mount["destination"], mount.get("source", "")
            if target in seen:
                raise ValueError("OCI mount destination is duplicated")
            seen.add(target)
            if target in expected:
                wanted, read_only = expected[target]
                options = set(mount.get("options", []))
                if (
                    mount["type"] != "bind"
                    or Path(source).resolve() != wanted
                    or {"ro", "rw"} <= options
                    or ("ro" in options) != read_only
                ):
                    raise ValueError(
                        f"OCI mount differs from the declared grant: {target}"
                    )
            elif target in virtual and mount["type"] == virtual[target]:
                continue
            elif (
                target in managed
                and mount["type"] == "bind"
                and Path(source) in {root / managed[target] for root in roots}
            ):
                continue
            elif (
                not self.probe
                and target == "/etc/localtime"
                and Path(source).resolve() == Path("/etc/localtime").resolve()
            ):
                continue
            else:
                raise ValueError(
                    f"OCI configuration contains an undeclared filesystem grant: {target}"
                )
        if not expected.keys() <= seen:
            raise ValueError("OCI configuration omitted a declared filesystem grant")


def verify_uid_map(actual: str, permitted: str) -> None:
    """Runtime namespaces may only map the rootless engine's delegated host IDs."""

    def ranges(value: str) -> list[tuple[int, int]]:
        values = list(map(int, value.split()))
        if not values or len(values) % 3:
            raise ValueError("invalid host user-namespace mapping")
        return [(values[i + 1], values[i + 2]) for i in range(0, len(values), 3)]

    allowed = ranges(permitted)
    if any(
        start <= 0
        or size <= 0
        or not any(
            base <= start and start + size <= base + count for base, count in allowed
        )
        for start, size in ranges(actual)
    ):
        raise ValueError("runtime user namespace exceeds the rootless engine identity")
