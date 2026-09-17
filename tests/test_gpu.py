from __future__ import annotations

from pathlib import Path

import pytest

from mimchine.gpu import GpuHost, inspect_gpu_host, podman_gpu_args


def test_podman_gpu_args_include_every_gpu_and_access_requirement() -> None:
    host = GpuHost(
        platform="Linux",
        devices=(
            Path("/dev/kfd"),
            Path("/dev/dri/renderD128"),
            Path("/dev/dri/renderD129"),
        ),
        nvidia=True,
        nvidia_cdi=True,
        rootless=True,
        selinux_enforcing=True,
    )

    assert podman_gpu_args(host) == (
        "--device",
        "/dev/kfd",
        "--device",
        "/dev/dri/renderD128",
        "--device",
        "/dev/dri/renderD129",
        "--device",
        "nvidia.com/gpu=all",
        "--group-add",
        "keep-groups",
        "--security-opt",
        "label=disable",
    )


def test_inspect_gpu_host_discovers_render_nodes_and_nvidia_cdi(
    tmp_path: Path, monkeypatch
) -> None:
    dev = tmp_path / "dev"
    (dev / "dri").mkdir(parents=True)
    (dev / "dri" / "renderD129").touch()
    (dev / "dri" / "renderD128").touch()
    (dev / "kfd").touch()
    (dev / "nvidiactl").touch()
    cdi = tmp_path / "cdi"
    cdi.mkdir()
    (cdi / "nvidia.yaml").write_text("kind: nvidia.com/gpu\ndevices:\n  - name: all\n")
    monkeypatch.setattr("mimchine.gpu.platform.system", lambda: "Linux")

    host = inspect_gpu_host(dev_root=dev, cdi_dirs=(cdi,))

    assert host.devices == (
        dev / "kfd",
        dev / "dri" / "renderD128",
        dev / "dri" / "renderD129",
    )
    assert host.nvidia_cdi


def test_nvidia_requires_cdi() -> None:
    host = GpuHost(
        platform="Linux",
        devices=(),
        nvidia=True,
        nvidia_cdi=False,
        rootless=True,
        selinux_enforcing=False,
    )

    with pytest.raises(ValueError, match="NVIDIA CDI is not configured"):
        podman_gpu_args(host)
