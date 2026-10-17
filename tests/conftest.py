from pathlib import Path

import pytest

from mimchine.process import ProcessRunner


@pytest.fixture(autouse=True)
def no_live_container_engine(monkeypatch):
    """Default unit tests must never operate a developer's real engine."""
    run = ProcessRunner.run
    forbidden = {"podman", "docker", "runsc", "runc", "crun", "systemctl"}

    def guarded(self, args, **kwargs):
        if args and Path(str(args[0])).name in forbidden:
            pytest.fail("unit test attempted to invoke a live container engine/runtime")
        return run(self, args, **kwargs)

    monkeypatch.setattr(ProcessRunner, "run", guarded)
