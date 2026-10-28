import os
from pathlib import Path

import pytest

from mimchine.process import ProcessRunner


@pytest.fixture(autouse=True)
def no_live_container_engine(monkeypatch, request):
    """Default unit tests must never operate a developer's real engine."""
    if (
        request.node.get_closest_marker("live_gvisor")
        and os.environ.get("MIM_GVISOR_LIVE") == "1"
    ):
        return
    if (
        request.node.get_closest_marker("live_updates")
        and os.environ.get("MIM_UPDATE_LIVE") == "1"
    ):
        return
    run = ProcessRunner.run
    forbidden = {
        "podman",
        "docker",
        "runsc",
        "runc",
        "crun",
        "systemctl",
        "busctl",
        "systemd-run",
    }

    def guarded(self, args, **kwargs):
        if args and Path(str(args[0])).name in forbidden:
            pytest.fail("unit test attempted to invoke a live container engine/runtime")
        return run(self, args, **kwargs)

    monkeypatch.setattr(ProcessRunner, "run", guarded)
