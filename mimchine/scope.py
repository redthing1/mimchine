"""Check the inherited host budget immediately before invoking Podman."""

from __future__ import annotations

import os
import sys
from pathlib import Path

HOST_TASK_LIMIT = 1024


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


def check_limits(group: Path, cpus: int, memory_mib: int) -> None:
    memory = (group / "memory.max").read_text().strip()
    swap = (group / "memory.swap.max").read_text().strip()
    cpu = (group / "cpu.max").read_text().split()
    tasks = (group / "pids.max").read_text().strip()
    if (
        memory != str(memory_mib * 1024 * 1024)
        or swap != "0"
        or tasks != str(HOST_TASK_LIMIT)
    ):
        raise ValueError(f"memory, swap or task budget is not enforced in {group}")
    if len(cpu) != 2 or cpu[0] == "max" or int(cpu[0]) != cpus * int(cpu[1]):
        raise ValueError(f"CPU budget is not enforced in {group}")


def main() -> None:
    unit, cpu, memory, *command = sys.argv[1:]
    group = cgroup_path(os.getpid())
    owners = [path for path in (group, *group.parents) if path.name == unit]
    if len(owners) != 1 or not command:
        raise ValueError("Podman invocation is outside the machine resource owner")
    check_limits(owners[0], int(cpu), int(memory))
    os.execvp(command[0], command)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        print(f"mim resource check: {error}", file=sys.stderr)
        sys.exit(125)
