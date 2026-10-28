"""Opt-in updates using fresh machines and an already-pulled Alpine image.

MIM_UPDATE_LIVE=1 MIM_UPDATE_RUNNER=podman MIM_UPDATE_IMAGE=sha256:... \
    uv run pytest -q tests/test_updates_live.py

For Docker, select a disposable daemon with DOCKER_HOST. No existing machines
or unfiltered container lists are read. Every machine has a unique test name,
temporary Mim state and cleanup through its exact owned binding.
"""

from __future__ import annotations

import os
import re
import socket
import time
import urllib.request
from uuid import uuid4

import pytest

from mimchine.config import AppConfig, Defaults
from mimchine.domain import (
    ExecSpec,
    IdentityMode,
    Operation,
    RuntimeState,
    UpdateOptions,
)
from mimchine.engines import get_engine
from mimchine.services import CreateOptions, MachineService
from mimchine.shell_state import ShellStateManager
from mimchine.state import MachineStore

pytestmark = [
    pytest.mark.live_updates,
    pytest.mark.skipif(
        os.environ.get("MIM_UPDATE_LIVE") != "1",
        reason="live updates require explicit opt-in",
    ),
]


@pytest.fixture
def live(tmp_path):
    runner = os.environ.get("MIM_UPDATE_RUNNER", "podman")
    image = os.environ.get("MIM_UPDATE_IMAGE", "")
    if not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}|.+@sha256:[a-f0-9]{64}", image):
        pytest.fail("MIM_UPDATE_IMAGE must be an existing immutable Alpine image")
    engine = get_engine(runner)
    # Inspect only the explicitly supplied fixture image; never pull implicitly.
    image = engine._image_metadata(image)["Id"]
    service = MachineService(
        AppConfig(Defaults(), {}),
        MachineStore(tmp_path / "machines"),
        ShellStateManager(tmp_path / "history"),
        {runner: engine},
    )
    name = "mim-update-test-" + uuid4().hex
    service.create(
        CreateOptions(
            name,
            image=image,
            runner=runner,
            identity=IdentityMode.ROOT,
            start=True,
            memory_mib=256,
            env=("PATH=/bin:/usr/bin", "TO_REMOVE=one"),
        )
    )
    try:
        yield service, engine, name
    finally:
        if service.store.exists(name):
            service.delete(name)


def execute(service, name, script):
    service.exec(name, ExecSpec(("/bin/sh", "-ec", script)))


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def response(port):
    # Loopback fixture traffic must not use ambient HTTP proxies.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(50):
        try:
            with opener.open(f"http://127.0.0.1:{port}/", timeout=0.5) as reply:
                return reply.read()
        except OSError:
            time.sleep(0.1)
    pytest.fail("synthetic published service was not reachable")


SERVER = r"""mkdir -p /usr/local/bin
cat >/usr/local/bin/mimchine-start <<'HOOK'
#!/bin/sh
while :; do
  printf 'HTTP/1.1 200 OK\r\nContent-Length: 13\r\nConnection: close\r\n\r\nsynthetic-ok\n' | nc -l -p 80 -w 1 >/dev/null
done
HOOK
chmod 755 /usr/local/bin/mimchine-start
"""


def test_live_update_preservation_and_ports(live, tmp_path):
    service, engine, name = live
    source = tmp_path / "shared"
    source.mkdir()
    (source / "hello").write_text("from-host")
    execute(
        service,
        name,
        SERVER
        + """
echo rootfs-survived >/var/tmp/marker
chmod 640 /var/tmp/marker
chown 123:456 /var/tmp/marker
ln /var/tmp/marker /var/tmp/hardlink
ln -s marker /var/tmp/symlink
rm /etc/alpine-release
echo history-survived >/mim/shell-state/synthetic-history
mkdir -p /usr/local/lib/synthetic
cp /bin/busybox /usr/local/lib/synthetic/busybox
""",
    )
    old = service.store.load(name)
    port = unused_port()
    options = UpdateOptions(
        mounts=(f"{source}:/shared:ro,z",),
        ports=(f"{port}:80",),
        env=('UPDATED=two words $HOME "quotes"',),
        unset_env=("TO_REMOVE", "PATH"),
    )
    original = service.store.read(name)
    plan = service.plan_update(name, options)
    assert plan.effect == "replace" and service.store.read(name) == original
    with pytest.raises(ValueError, match="--restart"):
        service.update(name, options)
    assert service.store.read(name) == original
    service.update(name, options, restart=True)
    current = service.store.load(name)
    assert current.id == old.id and current.target != old.target
    execute(
        service,
        name,
        r"""
test "$(cat /var/tmp/marker)" = rootfs-survived
test "$(stat -c '%a:%u:%g' /var/tmp/marker)" = 640:123:456
test "$(stat -c %i /var/tmp/marker)" = "$(stat -c %i /var/tmp/hardlink)"
test "$(readlink /var/tmp/symlink)" = marker
test ! -e /etc/alpine-release
test "$(cat /shared/hello)" = from-host
! touch /shared/blocked 2>/dev/null
test "${TO_REMOVE+x}" != x
test "$PATH" != /bin:/usr/bin
test "$UPDATED" = 'two words $HOME "quotes"'
test "$(cat /mim/shell-state/synthetic-history)" = history-survived
test "$(/usr/local/lib/synthetic/busybox echo installed-tool)" = installed-tool
""",
    )
    assert response(port) == b"synthetic-ok\n"
    service.update(
        name,
        UpdateOptions(
            resources=(
                ("cpus", 1),
                ("memory_mib", 384),
                ("swap_mib", 0),
                ("tasks", 256),
            )
        ),
    )
    assert service.store.load(name).target == current.target
    service.update(
        name,
        UpdateOptions(
            resources=(
                ("cpus", None),
                ("memory_mib", None),
                ("swap_mib", None),
                ("tasks", None),
            )
        ),
        restart=True,
    )
    unlimited = service.store.load(name)
    service.update(name, UpdateOptions(resources=(("cpus", 2),)))
    assert service.store.load(name).target == unlimited.target
    execute(service, name, "echo second-edit >/var/tmp/second")
    service.update(
        name,
        UpdateOptions(remove_mounts=("/shared",), remove_ports=(f"{port}:80",)),
        restart=True,
    )
    execute(
        service,
        name,
        "test ! -e /shared/hello; test ! -e /etc/alpine-release; test $(cat /var/tmp/second) = second-edit",
    )
    with socket.socket() as connection:
        connection.settimeout(0.5)
        assert connection.connect_ex(("127.0.0.1", port)) != 0
    assert engine._image_metadata(current.rootfs.image_id, missing_ok=True) is None
    service.stop(name)
    service.update(name, UpdateOptions(workdir="/var/tmp"))
    assert service.inspect(name).state is RuntimeState.STOPPED
    snapshot = service.store.load(name).rootfs
    service.delete(name)
    assert engine._image_metadata(snapshot.image_id, missing_ok=True) is None
    assert (source / "hello").read_text() == "from-host"


def test_live_recovery_after_lost_allocation_reply(live, monkeypatch):
    service, engine, name = live
    execute(service, name, "echo retained >/var/tmp/recovery-marker")
    allocate = engine.allocate
    allocations = []

    def lost_reply(machine):
        result = allocate(machine)
        allocations.append(result)
        raise RuntimeError("synthetic lost allocation reply")

    monkeypatch.setattr(engine, "allocate", lost_reply)
    with pytest.raises(RuntimeError, match="lost allocation"):
        service.update(name, UpdateOptions(env=("RECOVERED=yes",)), restart=True)
    assert service.store.load(name).operation is Operation.UPDATING
    monkeypatch.setattr(engine, "allocate", allocate)
    service.recover(name)
    assert service.store.load(name).target == allocations[0]
    execute(
        service,
        name,
        'test "$RECOVERED" = yes; test "$(cat /var/tmp/recovery-marker)" = retained',
    )


@pytest.mark.parametrize("failure", ["snapshot_reply", "allocation_reply", "cleanup"])
def test_live_delete_interrupted_update(live, monkeypatch, failure):
    from mimchine.domain import MissingContainer

    service, engine, name = live
    allocations = [service.store.load(name).target.container_id]
    images = []
    preserve, allocate, delete = engine.preserve_rootfs, engine.allocate, engine.delete
    armed = True

    def interrupt(stage):
        nonlocal armed
        if armed and failure == stage:
            armed = False
            raise RuntimeError("synthetic interrupted update")

    def snapshot(*args):
        result = preserve(*args)
        images.append(result.image_id)
        interrupt("snapshot_reply")
        return result

    def allocation(machine):
        result = allocate(machine)
        allocations.append(result.container_id)
        interrupt("allocation_reply")
        return result

    def cleanup(machine):
        interrupt("cleanup")
        return delete(machine)

    monkeypatch.setattr(engine, "preserve_rootfs", snapshot)
    monkeypatch.setattr(engine, "allocate", allocation)
    monkeypatch.setattr(engine, "delete", cleanup)
    with pytest.raises(RuntimeError, match="synthetic interrupted"):
        service.update(name, UpdateOptions(env=("DELETE_TEST=yes",)), restart=True)
    service.delete(name)
    assert not service.store.exists(name)
    assert all(isinstance(engine.observe(cid), MissingContainer) for cid in allocations)
    assert all(
        engine._image_metadata(image, missing_ok=True) is None for image in images
    )


def test_live_recovery_after_port_conflict(live):
    service, engine, name = live
    execute(service, name, SERVER)
    port = unused_port()
    blocker = "mim-update-test-" + uuid4().hex
    image = service.store.load(name).binding.image_id
    service.create(
        CreateOptions(
            blocker,
            image=image,
            runner=engine.name,
            identity=IdentityMode.ROOT,
            ports=(f"{port}:80",),
            start=True,
        )
    )
    try:
        with pytest.raises((RuntimeError, ValueError)):
            service.update(name, UpdateOptions(ports=(f"{port}:80",)), restart=True)
        assert service.store.load(name).update is not None
    finally:
        service.delete(blocker)
    service.recover(name)
    execute(service, name, "wget -qO- http://127.0.0.1:80/")
    assert response(port) == b"synthetic-ok\n"


def test_live_host_identity_survives_replacement(live):
    service, engine, name = live
    # A separate identity fixture is created from the explicitly selected image.
    service.delete(name)
    image = engine._image_metadata(os.environ["MIM_UPDATE_IMAGE"])["Id"]
    service.create(
        CreateOptions(
            name,
            image=image,
            runner=engine.name,
            identity=IdentityMode.HOST,
            start=True,
        )
    )
    execute(service, name, "id -u >/tmp/synthetic-uid; id -g >/tmp/synthetic-gid")
    service.update(name, UpdateOptions(env=("IDENTITY=retained",)), restart=True)
    execute(
        service,
        name,
        'test "$(id -u)" = "$(cat /tmp/synthetic-uid)"; test "$(id -g)" = "$(cat /tmp/synthetic-gid)"',
    )


def test_live_cli_preview_and_apply(live, tmp_path, monkeypatch):
    import json
    from typer.testing import CliRunner
    from mimchine import cli

    service, engine, name = live
    monkeypatch.setattr(cli.MachineService, "default", lambda: service)
    errors = []
    monkeypatch.setattr(cli.logger, "error", errors.append)
    source = tmp_path / "cli-share"
    source.mkdir()
    (source / "file").write_text("cli-fixture")
    arguments = [
        "update",
        name,
        "--mount",
        f"{source}:/cli-share:ro,z",
        "--env",
        "CLI_SECRET=synthetic-private",
    ]
    before = service.store.read(name)
    runner = CliRunner()
    preview = runner.invoke(cli.app, [*arguments, "--plan"])
    assert preview.exit_code == 0, preview.output
    assert "synthetic-private" not in preview.output
    assert json.loads(preview.output)["effect"] == "replace"
    assert service.store.read(name) == before
    refused = runner.invoke(cli.app, arguments)
    assert refused.exit_code != 0
    assert "--restart" in errors[-1]
    result = runner.invoke(cli.app, [*arguments, "--restart"])
    assert result.exit_code == 0, result.output
    assert f"Updated {name}." in result.output
    execute(
        service,
        name,
        'test "$(cat /cli-share/file)" = cli-fixture; test "$CLI_SECRET" = synthetic-private',
    )
    repeated = runner.invoke(cli.app, arguments)
    assert repeated.exit_code == 0 and "No changes." in repeated.output
