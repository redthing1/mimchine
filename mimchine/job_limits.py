from __future__ import annotations

import hashlib
import os
import platform
import shutil
import tempfile
from pathlib import Path

from .paths import data_dir


HELPER_PATH = "/mim/bin/mim-limit"

# crun places PID 1 and subsequent execs in /payload, leaving the namespace
# root empty so it can distribute the memory controller to child cgroups.
SETUP_SCRIPT = r"""
set -eu
base=/sys/fs/cgroup
fail() { printf 'mim: job limits unavailable: %s\n' "$*" >&2; exit 1; }
test -d "$base/payload" || fail 'missing payload cgroup; recreate with --job-limits'
test -w "$base/cgroup.subtree_control" || fail 'cgroup mount must be writable (check SELinux container_manage_cgroup policy)'
case " $(cat "$base/cgroup.controllers") " in
    *' memory '*) ;;
    *) fail 'memory controller is not delegated by the host' ;;
esac
printf '+memory\n' > "$base/cgroup.subtree_control" || fail 'cannot delegate memory (check SELinux container_manage_cgroup policy)'
mkdir -p "$base/jobs"
printf '+memory\n' > "$base/jobs/cgroup.subtree_control"
# Only the delegation interfaces and jobs directory are handed to the user.
# Container-level resource controls remain owned by the runtime.
chown "$1:$2" "$base/jobs" "$base/jobs/cgroup.procs" "$base/cgroup.procs"
""".strip()


def require_host(info: dict) -> None:
    host = info.get("host", {})
    if (
        platform.system() != "Linux"
        or "microsoft" in platform.release().lower()
        or host.get("cgroupVersion") != "v2"
        or host.get("cgroupManager") != "systemd"
        or host.get("ociRuntime", {}).get("name") != "crun"
    ):
        raise ValueError(
            "job limits require native Linux, cgroup v2, Podman's systemd "
            "cgroup manager, and crun"
        )


def helper_mount_source() -> Path:
    source = Path(__file__).with_name("scripts") / "mim-limit.sh"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    target = data_dir() / "runtime" / f"mim-limit-{digest}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temp:
            temporary = Path(temp.name)
        try:
            shutil.copyfile(source, temporary)
            temporary.chmod(0o755)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return target
