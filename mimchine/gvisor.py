"""Identify an operator-installed Mim gVisor distribution without executing it."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

PROFILES = ("offline", "offline-agent", "online", "online-agent")
INSTALL_DIRECTORY = Path("/usr/lib/mim-gvisor")
RELEASE_DIRECTORY = re.compile(r"([0-9]+)\.([0-9]+)-mim\.([0-9]+)")
REQUIRED_FILES = {
    "runsc",
    "containerd-shim-runsc-v1",
    "gvisor-bin/gvisor_sentry",
    "gvisor-bin/checkpointgofer",
    "gvisor-bin/runsc-metric-server",
    "gvisor-bin/runsc-fd-parking",
    "gvisor-bin/gvisor-sentry-prewarmer",
    "LICENSE",
    *(f"profiles/{profile}/runsc" for profile in PROFILES),
}


@dataclass(frozen=True)
class GvisorRuntimeIdentity:
    distribution: Path
    manifest_sha256: str
    version: str
    source_commit: str
    profile: str
    flags: tuple[str, ...]

    @property
    def entrypoint(self) -> Path:
        return self.distribution / "profiles" / self.profile / "runsc"


def find_runtime(
    configured: Path | None, *, directory: Path = INSTALL_DIRECTORY
) -> Path:
    """Select the installed package for a new machine, or an explicit path.

    Existing bindings use their canonical reference, never discovery. A broken
    selected release must fail verification rather than silently fall back.
    """
    if configured is not None:
        return configured
    candidates = []
    try:
        # Prefer the package's stable location over older coinstalled releases.
        # A partial or broken package must fail verification, not fall back.
        if any(
            path.exists() or path.is_symlink()
            for path in (directory / "runsc", directory / "manifest.json", directory / "profiles")
        ):
            return directory / "runsc"
        if directory.is_dir():
            for entry in directory.iterdir():
                match = RELEASE_DIRECTORY.fullmatch(entry.name)
                if match and entry.is_dir():
                    candidates.append((tuple(map(int, match.groups())), entry))
    except OSError as error:
        raise ValueError(f"cannot discover gVisor in {directory}: {error}") from error
    if not candidates:
        raise ValueError(
            f"no Mim gVisor release installed in {directory}; "
            "install a runtime package or configure [runtimes.gvisor].path"
        )
    return max(candidates)[1] / "runsc"


def verify_runtime(
    path: Path, profile: str, *, expected_manifest: str | None = None
) -> GvisorRuntimeIdentity:
    """Check content identity; installation trust remains the operator's decision.

    A configured selector may be a symlink. The returned reference always names
    its canonical distribution, so moving that selector cannot upgrade a bound
    machine. Checksums detect changes; they do not authenticate a publisher or
    establish effective OCI grants and host resource enforcement.
    """
    if not path.is_absolute() or profile not in PROFILES:
        raise ValueError("gVisor requires an absolute runsc path and a known profile")
    try:
        binary = path.resolve(strict=True)
        if binary.name != "runsc":
            raise ValueError("distribution entry must be named runsc")
        root = binary.parent
        for directory in (root, *root.parents):
            metadata = directory.stat()
            if metadata.st_uid not in (0, os.getuid()) or (
                metadata.st_mode & 0o022
                and not (
                    directory != root
                    and metadata.st_uid == 0
                    and metadata.st_mode & stat.S_ISVTX
                )
            ):
                raise ValueError(
                    f"runtime directory permits untrusted writes: {directory}"
                )
        manifest_path = root / "manifest.json"
        if (
            manifest_path.is_symlink()
            or not manifest_path.is_file()
            or manifest_path.stat().st_size > 1024 * 1024
        ):
            raise ValueError("invalid distribution manifest")
        content = manifest_path.read_bytes()
        _check_file(manifest_path)
        digest = hashlib.sha256(content).hexdigest()
        if expected_manifest is not None and digest != expected_manifest:
            raise ValueError("distribution manifest differs from the bound identity")
        manifest = json.loads(content, object_pairs_hook=_unique_keys)
        if (
            type(manifest["policy_version"]) is not int
            or manifest["policy_version"] != 1
        ):
            raise ValueError("unsupported runtime policy version")
        if manifest["architecture"] != platform.machine():
            raise ValueError("runtime architecture does not match this host")
        version, commit = manifest["version"], manifest["source_commit"]
        if not isinstance(version, str) or not re.fullmatch(
            r"[0-9][a-z0-9.+-]*", version
        ):
            raise ValueError("invalid runtime version")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("invalid runtime source identity")
        files = manifest["files"]
        if (
            not isinstance(files, dict)
            or not REQUIRED_FILES <= files.keys()
            or len(files) > 128
        ):
            raise ValueError("incomplete runtime distribution")
        for name, expected in files.items():
            relative = PurePosixPath(name)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or str(relative) != name
            ):
                raise ValueError("invalid distribution file path")
            installed = root / name
            if installed.resolve(strict=True) != installed or not installed.is_file():
                raise ValueError(f"distribution file must be a regular file: {name}")
            _check_file(installed)
            for directory in installed.parents:
                if directory == root:
                    break
                metadata = directory.stat()
                if metadata.st_uid not in (0, os.getuid()) or metadata.st_mode & 0o022:
                    raise ValueError(
                        f"runtime directory permits untrusted writes: {directory}"
                    )
            if not isinstance(expected, str) or not re.fullmatch(
                r"[0-9a-f]{64}", expected
            ):
                raise ValueError(f"invalid file digest: {name}")
            with installed.open("rb") as source:
                actual = hashlib.file_digest(source, "sha256").hexdigest()
            if actual != expected:
                raise ValueError(f"distribution file checksum mismatch: {name}")
            if name != "LICENSE" and not installed.stat().st_mode & 0o111:
                raise ValueError(
                    f"distribution executable has no execute permission: {name}"
                )
        profiles = manifest["profiles"]
        if not isinstance(profiles, dict) or set(profiles) != set(PROFILES):
            raise ValueError("invalid runtime profiles")
        for name, settings in profiles.items():
            if (
                not isinstance(settings, dict)
                or settings["path"] != f"profiles/{name}/runsc"
                or not isinstance(settings["flags"], list)
            ):
                raise ValueError("invalid runtime profile settings")
            if not settings["flags"] or any(
                not isinstance(flag, str) or not flag for flag in settings["flags"]
            ):
                raise ValueError("invalid runtime profile flags")
        return GvisorRuntimeIdentity(
            root, digest, version, commit, profile, tuple(profiles[profile]["flags"])
        )
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError(
            f"cannot verify gVisor distribution at {path}: {error}"
        ) from error


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate manifest key: {key}")
        result[key] = value
    return result


def _check_file(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in (0, os.getuid())
        or metadata.st_mode & 0o022
        or metadata.st_nlink != 1
    ):
        raise ValueError(
            f"runtime file must be owned, singly linked and protected from other writers: {path}"
        )
