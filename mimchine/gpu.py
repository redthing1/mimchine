from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path


CDI_DIRS = (Path("/etc/cdi"), Path("/var/run/cdi"))
NVIDIA_CDI_DEVICE = "nvidia.com/gpu=all"


@dataclass(frozen=True)
class GpuHost:
    platform: str
    devices: tuple[Path, ...]
    nvidia: bool
    nvidia_cdi: bool
    rootless: bool
    selinux_enforcing: bool

    @property
    def podman_devices(self) -> tuple[str, ...]:
        devices = tuple(map(str, self.devices))
        if self.nvidia:
            devices += (NVIDIA_CDI_DEVICE,)
        return devices

    def require_ready(self) -> None:
        if self.platform != "Linux":
            raise ValueError(
                f"automatic GPU forwarding requires native Linux, not {self.platform}"
            )
        if self.nvidia and not self.nvidia_cdi:
            raise ValueError(
                "NVIDIA CDI is not configured; install NVIDIA Container Toolkit"
            )
        if not self.podman_devices:
            raise ValueError("no GPU devices were found")


def inspect_gpu_host(
    *,
    dev_root: Path = Path("/dev"),
    cdi_dirs: tuple[Path, ...] = CDI_DIRS,
) -> GpuHost:
    render_nodes = tuple(sorted((dev_root / "dri").glob("renderD*")))
    kfd = dev_root / "kfd"
    devices = ((kfd,) if kfd.exists() and render_nodes else ()) + render_nodes
    return GpuHost(
        platform=_platform(),
        devices=devices,
        nvidia=(dev_root / "nvidiactl").exists(),
        nvidia_cdi=_has_nvidia_cdi(cdi_dirs),
        rootless=os.geteuid() != 0,
        selinux_enforcing=_selinux_enforcing(),
    )


def podman_gpu_args(host: GpuHost | None = None) -> tuple[str, ...]:
    host = host or inspect_gpu_host()
    host.require_ready()

    args: list[str] = []
    for device in host.podman_devices:
        args.extend(("--device", device))
    if host.rootless:
        args.extend(("--group-add", "keep-groups"))
    if host.selinux_enforcing:
        args.extend(("--security-opt", "label=disable"))
    return tuple(args)


def _platform() -> str:
    system = platform.system()
    if system == "Linux" and "microsoft" in platform.release().lower():
        return "WSL"
    return system


def _selinux_enforcing() -> bool:
    try:
        return Path("/sys/fs/selinux/enforce").read_text().strip() == "1"
    except OSError:
        return False


def _has_nvidia_cdi(directories: tuple[Path, ...]) -> bool:
    for directory in directories:
        for path in directory.glob("*") if directory.is_dir() else ():
            if path.suffix not in {".json", ".yaml", ".yml"}:
                continue
            try:
                text = path.read_text()
            except OSError:
                continue
            compact = "".join(text.split()).replace('"', "").replace("'", "")
            if "kind:nvidia.com/gpu" in compact and "name:all" in compact:
                return True
    return False
