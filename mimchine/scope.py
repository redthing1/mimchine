"""Verify machine ownership and requested ceilings immediately before Podman."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def cgroup_path(
    pid: int, *, proc: Path = Path("/proc"), root: Path = Path("/sys/fs/cgroup")
) -> Path:
    lines = (proc / str(pid) / "cgroup").read_text().splitlines()
    entries = [line[3:] for line in lines if line.startswith("0::")]
    if (
        len(entries) != 1
        or not entries[0].startswith("/")
        or ".." in Path(entries[0]).parts
    ):
        raise ValueError("expected a host cgroup v2 process path")
    return root / entries[0].lstrip("/")


def check_limits(
    group: Path,
    *,
    cpus: int | None = None,
    memory_mib: int | None = None,
    swap_mib: int | None = None,
    tasks: int | None = None,
) -> None:
    for name, value in (
        ("memory.max", None if memory_mib is None else memory_mib * 1024 * 1024),
        ("memory.swap.max", None if swap_mib is None else swap_mib * 1024 * 1024),
        ("pids.max", tasks),
    ):
        if value is not None and (group / name).read_text().strip() != str(value):
            raise ValueError(f"{name} budget is not enforced in {group}")
    if cpus is not None:
        cpu = (group / "cpu.max").read_text().split()
        if len(cpu) != 2 or cpu[0] == "max" or int(cpu[0]) != cpus * int(cpu[1]):
            raise ValueError(f"CPU budget is not enforced in {group}")


def identity(group: Path) -> tuple[int, int]:
    metadata = group.stat()
    return metadata.st_dev, metadata.st_ino


def main() -> None:
    unit, device, inode, policy, *command = sys.argv[1:]
    group = cgroup_path(os.getpid())
    owners = [path for path in (group, *group.parents) if path.name == unit]
    if len(owners) != 1 or not command:
        raise ValueError("Podman invocation is outside the machine resource owner")
    owner = owners[0]
    expected = int(device), int(inode)
    if identity(owner) != expected:
        raise ValueError("machine resource owner was replaced before dispatch")
    check_limits(owner, **json.loads(policy))
    if identity(owner) != expected:
        raise ValueError("machine resource owner changed during verification")
    os.execvp(command[0], command)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        print(f"mim resource check: {error}", file=sys.stderr)
        sys.exit(125)
