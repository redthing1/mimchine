"""Check local prerequisites without initializing an engine or running workloads."""

from __future__ import annotations

import os
import pwd
import re
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import AppConfig, validate_runner
from .domain import IsolationMode, NetworkMode
from .gvisor import find_runtime, verify_runtime
from .process import ProcessResult, ProcessRunner
from .profiles import load_profile


@dataclass(frozen=True)
class Finding:
    status: Literal["ok", "warn", "error"]
    name: str
    detail: str


def inspect_host(
    config: AppConfig,
    *,
    runner: str | None = None,
    isolation: IsolationMode | None = None,
    profile: str | None = None,
    no_net: bool = False,
    process: ProcessRunner | None = None,
) -> tuple[Finding, ...]:
    selected = load_profile(config, profile)
    runner = runner or selected.runner or config.defaults.runner
    validate_runner(runner)
    isolation = isolation or selected.isolation or config.defaults.isolation
    network = (
        NetworkMode.NONE if no_net else selected.network or config.defaults.network
    )
    process = process or ProcessRunner()
    findings = []

    if runner == "podman" and sys.platform != "linux":
        return (
            Finding(
                "error",
                "Platform",
                "Podman requires local Linux; use --runner docker on macOS.",
            ),
        )
    if isolation is IsolationMode.GVISOR and (
        runner != "podman" or sys.platform != "linux" or os.getuid() == 0
    ):
        return (
            Finding(
                "error",
                "Isolation",
                "gVisor requires local Linux and Podman under an ordinary user account.",
            ),
        )
    if isolation is IsolationMode.GVISOR and network is NetworkMode.HOST:
        return (
            Finding(
                "error",
                "Networking",
                "gVisor does not support host networking; select default or none.",
            ),
        )

    result = _probe(process, runner, "--version")
    available = result.returncode == 0 and bool(result.stdout.strip())
    findings.append(
        Finding(
            "ok" if available else "error",
            "Podman" if runner == "podman" else "Docker client",
            result.stdout.strip().splitlines()[0]
            if available
            else f"Cannot run {runner} --version; install or repair the {runner} package.",
        )
    )
    if runner == "docker":
        return tuple(findings)

    if os.getuid() != 0:
        missing = [
            name for name in ("newuidmap", "newgidmap") if not shutil.which(name)
        ]
        findings.append(
            Finding(
                "error" if missing else "ok",
                "Rootless helpers",
                f"Missing {', '.join(missing)}; install your distribution's uidmap/shadow-utils package."
                if missing
                else "newuidmap and newgidmap found",
            )
        )
        findings.append(_subordinate_ids(process))
        if network is NetworkMode.DEFAULT:
            findings.append(_network(process, isolation))
    else:
        findings.append(
            Finding(
                "warn",
                "Account",
                "Running as root; use an ordinary account for rootless Podman.",
            )
        )

    if isolation is IsolationMode.GVISOR:
        try:
            runtime = verify_runtime(find_runtime(config.gvisor.path), "offline")
            findings.append(
                Finding(
                    "ok",
                    "gVisor",
                    f"{runtime.version}; distribution integrity verified",
                )
            )
        except ValueError as error:
            findings.append(Finding("error", "gVisor", str(error)))
        findings.append(_user_systemd(process))
        cgroup_v2 = Path("/sys/fs/cgroup/cgroup.controllers").is_file()
        findings.append(
            Finding(
                "ok" if cgroup_v2 else "error",
                "Cgroup v2",
                "Unified hierarchy available"
                if cgroup_v2
                else "Enable the unified cgroup v2 hierarchy.",
            )
        )
    else:
        runtime = next((name for name in ("crun", "runc") if shutil.which(name)), None)
        findings.append(
            Finding(
                "ok" if runtime else "warn",
                "OCI runtime",
                f"{runtime} found"
                if runtime
                else "No crun or runc on PATH; install a runtime or check Podman's configured runtime path.",
            )
        )
    if not shutil.which("conmon"):
        findings.append(
            Finding(
                "warn",
                "Container monitor",
                "No conmon on PATH; install conmon or check Podman's configured helper path.",
            )
        )
    return tuple(findings)


def _probe(process: ProcessRunner, *args: str) -> ProcessResult:
    try:
        return process.run(
            args, capture=True, check=False, cwd="/", env={"LC_ALL": "C"}, timeout=3
        )
    except OSError:
        return ProcessResult(tuple(args), 126)


def _network(process: ProcessRunner, isolation: IsolationMode) -> Finding:
    if shutil.which("pasta"):
        if isolation is IsolationMode.GVISOR:
            result = _probe(process, "pasta", "--help")
            if result.returncode not in (0, 1):
                return Finding(
                    "warn",
                    "Networking",
                    "Cannot check pasta capabilities; check the passt installation or retry.",
                )
            if "--no-splice" not in result.stdout + result.stderr:
                return Finding(
                    "error",
                    "Networking",
                    "pasta must support --no-splice; update the passt package.",
                )
        return Finding("ok", "Networking", "pasta found (passt)")
    if isolation is IsolationMode.NATIVE and shutil.which("slirp4netns"):
        return Finding(
            "warn",
            "Networking",
            "slirp4netns found; confirm Podman is configured to use it. Current defaults use pasta (passt).",
        )
    return Finding(
        "warn",
        "Networking",
        "No pasta on PATH; install passt or check Podman's configured helper path.",
    )


def _subordinate_ids(process: ProcessRunner) -> Finding:
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return Finding(
            "error",
            "Subordinate IDs",
            "The current UID has no user account; use an account known to the system's user database.",
        )
    if shutil.which("getsubids"):
        for args in (("getsubids", user), ("getsubids", "-g", user)):
            result = _probe(process, *args)
            if result.returncode != 0:
                return Finding(
                    "warn",
                    "Subordinate IDs",
                    "Cannot query subordinate IDs; check your account's subid configuration.",
                )
            if not result.stdout.strip():
                return Finding(
                    "error",
                    "Subordinate IDs",
                    "Assign subordinate UID and GID ranges to your account through /etc/subuid and /etc/subgid or your subid provider.",
                )
            ranges = [
                re.fullmatch(
                    rf"\s*\d+:\s+{re.escape(user)}\s+\d+\s+([1-9]\d*)\s*", line
                )
                for line in result.stdout.splitlines()
            ]
            if not all(ranges):
                return Finding(
                    "warn",
                    "Subordinate IDs",
                    "Cannot interpret getsubids output; check your account's subid configuration.",
                )
        return Finding("ok", "Subordinate IDs", "UID and GID ranges assigned")
    try:
        # A non-file NSS provider needs libsubid's query tool; local files alone
        # cannot establish the ranges that Podman will actually receive.
        try:
            nss = Path("/etc/nsswitch.conf").read_text()
        except FileNotFoundError:
            nss = ""
        providers = re.search(r"^\s*subid\s*:\s*([^#\n]+)", nss, re.MULTILINE)
        if providers and providers[1].strip() != "files":
            return Finding(
                "warn",
                "Subordinate IDs",
                "Install getsubids to check your configured subid provider.",
            )
        for name in ("subuid", "subgid"):
            lines = Path(f"/etc/{name}").read_text().splitlines()
            if not any(
                len(parts := line.split(":")) == 3
                and parts[0] in (user, str(os.getuid()))
                and parts[1].isdigit()
                and parts[2].isdigit()
                and int(parts[2]) > 0
                for line in lines
            ):
                return Finding(
                    "error",
                    "Subordinate IDs",
                    "Assign subordinate UID and GID ranges to your account in /etc/subuid and /etc/subgid.",
                )
    except FileNotFoundError:
        return Finding(
            "error",
            "Subordinate IDs",
            "Assign subordinate UID and GID ranges to your account in /etc/subuid and /etc/subgid.",
        )
    except OSError:
        return Finding(
            "warn",
            "Subordinate IDs",
            "Cannot read subordinate ID configuration; check /etc/subuid and /etc/subgid or install getsubids.",
        )
    return Finding("ok", "Subordinate IDs", "UID and GID ranges assigned")


def _user_systemd(process: ProcessRunner) -> Finding:
    missing = [
        name
        for name in ("systemctl", "systemd-run", "busctl")
        if not shutil.which(name)
    ]
    if missing:
        return Finding(
            "error",
            "User systemd",
            f"Missing {', '.join(missing)}; install the systemd client tools.",
        )
    client = _probe(process, "systemd-run", "--help")
    if client.returncode:
        return Finding(
            "error",
            "User systemd",
            "Cannot check systemd-run capabilities; check the systemd client installation or retry.",
        )
    if "--expand-environment" not in client.stdout:
        return Finding(
            "error",
            "User systemd",
            "systemd-run must support --expand-environment; install systemd 254 or newer.",
        )
    result = _probe(
        process,
        "busctl",
        "--user",
        "--timeout=3",
        "--auto-start=no",
        "--allow-interactive-authorization=no",
        "call",
        "org.freedesktop.systemd1",
        "/org/freedesktop/systemd1",
        "org.freedesktop.DBus.Properties",
        "Get",
        "ss",
        "org.freedesktop.systemd1.Manager",
        "Version",
    )
    try:
        variant, kind, version = shlex.split(result.stdout)
        if result.returncode or (variant, kind) != ("v", "s") or not version:
            raise ValueError
    except ValueError:
        return Finding(
            "error",
            "User systemd",
            "Cannot reach the user manager; check your login session, XDG_RUNTIME_DIR and user D-Bus connection.",
        )
    # The manager's version is informational, not a feature-detection API.
    return Finding("ok", "User systemd", f"{version}; user manager reachable")
