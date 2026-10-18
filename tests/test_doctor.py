from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mimchine import doctor
from mimchine.config import AppConfig, Defaults
from mimchine.domain import IsolationMode, NetworkMode
from mimchine.process import ProcessResult


class Probes:
    def __init__(self):
        self.calls = []
        self.replies = {
            ("podman", "--version"): (0, "podman version 5.8.4\n", ""),
            ("docker", "--version"): (0, "Docker version 28.0.0\n", ""),
            ("getsubids", "mimtest"): (0, "0: mimtest 100000 65536\n", ""),
            ("getsubids", "-g", "mimtest"): (0, "0: mimtest 200000 65536\n", ""),
            ("pasta", "--help"): (1, "", "Usage: pasta --no-splice\n"),
            ("systemd-run", "--help"): (0, "--expand-environment=BOOL", ""),
            (
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
            ): (0, 'v s "257.6"\n', ""),
        }

    def run(self, args, **kwargs):
        args = tuple(args)
        self.calls.append(args)
        assert kwargs == dict(
            capture=True, check=False, cwd="/", env={"LC_ALL": "C"}, timeout=3
        )
        # Only these fixed metadata queries may be used. No engine inspection,
        # runtime execution, namespace creation or systemd mutations are allowed.
        code, stdout, stderr = self.replies[args]
        return ProcessResult(args, code, stdout, stderr)


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.setattr(doctor, "os", SimpleNamespace(getuid=lambda: 1000))
    monkeypatch.setattr(
        doctor.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="mimtest")
    )
    tools = {
        "newuidmap",
        "newgidmap",
        "getsubids",
        "pasta",
        "conmon",
        "crun",
        "systemctl",
        "systemd-run",
        "busctl",
    }
    monkeypatch.setattr(
        doctor.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in tools else None,
    )
    read_text = Path.read_text
    files = {
        "/etc/nsswitch.conf": "subid: files\n",
        "/etc/subuid": "mimtest:100000:65536\n",
        "/etc/subgid": "1000:200000:65536\n",
    }

    def read(path, *args, **kwargs):
        if str(path) in files:
            value = files[str(path)]
            if isinstance(value, Exception):
                raise value
            return value
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    is_file = Path.is_file
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda path: True
        if str(path) == "/sys/fs/cgroup/cgroup.controllers"
        else is_file(path),
    )
    runtime_calls = []
    monkeypatch.setattr(
        doctor, "find_runtime", lambda path: path or Path("/runtime/runsc")
    )

    def runtime(path, profile):
        runtime_calls.append((path, profile))
        return SimpleNamespace(version="20261012.0-mim.4")

    monkeypatch.setattr(doctor, "verify_runtime", runtime)
    return SimpleNamespace(
        tools=tools, files=files, process=Probes(), runtime_calls=runtime_calls
    )


def check(host, config=None, **kwargs):
    findings = doctor.inspect_host(
        config or AppConfig(Defaults(), {}), process=host.process, **kwargs
    )
    return {finding.name: finding for finding in findings}


def test_native_podman_checks_only_base_prerequisites(host):
    findings = check(host)
    assert all(f.status == "ok" for f in findings.values())
    assert "OCI runtime" in findings and "User systemd" not in findings
    assert host.process.calls == [
        ("podman", "--version"),
        ("getsubids", "mimtest"),
        ("getsubids", "-g", "mimtest"),
    ]
    assert not host.runtime_calls


def test_missing_podman_and_helpers_are_actionable(host):
    host.process.replies[("podman", "--version")] = (127, "", "not found")
    host.tools.difference_update({"newuidmap", "newgidmap", "pasta", "crun", "conmon"})
    findings = check(host)
    assert findings["Podman"].status == "error"
    assert "uidmap" in findings["Rootless helpers"].detail
    assert "passt" in findings["Networking"].detail
    assert findings["OCI runtime"].status == "warn"
    assert findings["Container monitor"].status == "warn"


@pytest.mark.parametrize("reply", [(0, "", ""), (124, "", "timed out")])
def test_unavailable_version_query_is_an_error(host, reply):
    host.process.replies[("podman", "--version")] = reply
    assert check(host)["Podman"].status == "error"


def test_slirp_configuration_gets_a_note_not_an_unrelated_dependency_error(host):
    host.tools.remove("pasta")
    host.tools.add("slirp4netns")
    finding = check(host)["Networking"]
    assert finding.status == "warn" and "slirp4netns" in finding.detail


@pytest.mark.parametrize("network", [NetworkMode.NONE, NetworkMode.HOST])
def test_native_modes_without_private_networking_skip_helpers(host, network):
    host.tools.remove("pasta")
    assert "Networking" not in check(host, AppConfig(Defaults(network=network), {}))


def test_profile_resolution_and_explicit_overrides(host):
    config = AppConfig(
        Defaults(runner="docker"),
        {"work": {"runner": "podman", "isolation": "gvisor", "network": "none"}},
    )
    findings = check(host, config, profile="work")
    assert "gVisor" in findings and "Networking" not in findings
    findings = check(
        host, config, profile="work", runner="docker", isolation=IsolationMode.NATIVE
    )
    assert set(findings) == {"Docker client"}


def test_gvisor_checks_distribution_and_capabilities(host):
    findings = check(host, isolation=IsolationMode.GVISOR)
    assert all(f.status == "ok" for f in findings.values())
    assert "OCI runtime" not in findings
    assert host.runtime_calls == [(Path("/runtime/runsc"), "offline")]
    assert ("pasta", "--help") in host.process.calls
    assert findings["Cgroup v2"].status == "ok"


def test_gvisor_reports_a_missing_cgroup_v2_hierarchy(host, monkeypatch):
    # The hierarchy marker is enough for this prerequisite check. Actual
    # delegation and requested ceilings remain creation-time verification.
    def is_file(path):
        assert str(path) == "/sys/fs/cgroup/cgroup.controllers"
        return False

    monkeypatch.setattr(Path, "is_file", is_file)
    assert check(host, isolation=IsolationMode.GVISOR)["Cgroup v2"].status == "error"


def test_gvisor_requires_pasta_even_when_slirp_is_available(host):
    host.tools.remove("pasta")
    host.tools.add("slirp4netns")
    finding = check(host, isolation=IsolationMode.GVISOR)["Networking"]
    assert finding.status == "warn" and "No pasta" in finding.detail


def test_offline_gvisor_does_not_require_networking_helpers(host):
    host.tools.remove("pasta")
    findings = check(host, isolation=IsolationMode.GVISOR, no_net=True)
    assert "Networking" not in findings
    assert ("pasta", "--help") not in host.process.calls


@pytest.mark.parametrize(
    "reply,status",
    [((0, "old pasta", ""), "error"), ((124, "--no-splice", ""), "warn")],
)
def test_pasta_capability_probe_must_complete(host, reply, status):
    host.process.replies[("pasta", "--help")] = reply
    assert check(host, isolation=IsolationMode.GVISOR)["Networking"].status == status


@pytest.mark.parametrize(
    "reply", [(124, "", ""), (0, 'v s ""', ""), (0, "unexpected output", "")]
)
def test_unavailable_user_manager_is_reported(host, reply):
    host.process.replies[
        next(args for args in host.process.replies if args[0] == "busctl")
    ] = reply
    assert check(host, isolation=IsolationMode.GVISOR)["User systemd"].status == "error"


@pytest.mark.parametrize("version", ["v259~rc2.custom", "custom systemd", "253.9"])
def test_manager_version_is_informational_not_a_capability_gate(host, version):
    args = next(args for args in host.process.replies if args[0] == "busctl")
    host.process.replies[args] = (0, f'v s "{version}"\n', "")
    finding = check(host, isolation=IsolationMode.GVISOR)["User systemd"]
    assert finding.status == "ok" and version in finding.detail


def test_new_manager_does_not_hide_an_old_systemd_client(host):
    host.process.replies[("systemd-run", "--help")] = (0, "old client", "")
    assert check(host, isolation=IsolationMode.GVISOR)["User systemd"].status == "error"
    assert not any(args[0] == "busctl" for args in host.process.calls)


def test_failed_systemd_query_is_not_diagnosed_as_an_old_client(host):
    host.process.replies[("systemd-run", "--help")] = (124, "", "")
    finding = check(host, isolation=IsolationMode.GVISOR)["User systemd"]
    assert finding.status == "error" and "Cannot check" in finding.detail
    assert "254" not in finding.detail


def test_invalid_runtime_is_reported_alongside_other_checks(host, monkeypatch):
    def reject(*args):
        raise ValueError("distribution file checksum mismatch")

    monkeypatch.setattr(doctor, "verify_runtime", reject)
    findings = check(host, isolation=IsolationMode.GVISOR)
    assert findings["gVisor"].status == "error"
    assert "checksum mismatch" in findings["gVisor"].detail
    assert findings["User systemd"].status == "ok"


@pytest.mark.parametrize(
    "reply,status", [((0, "", ""), "error"), ((124, "", ""), "warn")]
)
def test_missing_or_unavailable_subordinate_ids(host, reply, status):
    host.process.replies[("getsubids", "-g", "mimtest")] = reply
    assert check(host)["Subordinate IDs"].status == status


@pytest.mark.parametrize(
    "output", ["unexpected output", "0: somebodyelse 100000 65536\n"]
)
def test_unrecognized_id_query_is_not_diagnosed_as_missing_ranges(host, output):
    host.process.replies[("getsubids", "mimtest")] = (0, output, "")
    finding = check(host)["Subordinate IDs"]
    assert finding.status == "warn" and "Cannot interpret" in finding.detail


def test_rootless_account_must_resolve_in_the_user_database(host, monkeypatch):
    def missing(uid):
        raise KeyError(uid)

    monkeypatch.setattr(doctor.pwd, "getpwuid", missing)
    finding = check(host)["Subordinate IDs"]
    assert finding.status == "error" and "no user account" in finding.detail


def test_local_subid_fallback_accepts_names_and_numeric_uids(host):
    host.tools.remove("getsubids")
    assert check(host)["Subordinate IDs"].status == "ok"
    host.files["/etc/subgid"] = "somebodyelse:200000:65536\n"
    assert check(host)["Subordinate IDs"].status == "error"


def test_absent_nss_config_uses_the_default_file_provider(host):
    host.tools.remove("getsubids")
    host.files["/etc/nsswitch.conf"] = FileNotFoundError()
    assert check(host)["Subordinate IDs"].status == "ok"


def test_missing_local_ranges_are_an_error(host):
    host.tools.remove("getsubids")
    host.files["/etc/subuid"] = FileNotFoundError()
    assert check(host)["Subordinate IDs"].status == "error"


def test_local_ranges_cannot_validate_an_external_subid_provider(host):
    host.tools.remove("getsubids")
    host.files["/etc/nsswitch.conf"] = "subid: sss\n"
    assert check(host)["Subordinate IDs"].status == "warn"


def test_unreadable_subid_config_is_a_note(host):
    host.tools.remove("getsubids")
    host.files["/etc/subgid"] = PermissionError()
    assert check(host)["Subordinate IDs"].status == "warn"


def test_docker_on_macos_only_queries_the_client(host, monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    assert set(check(host, runner="docker")) == {"Docker client"}
    assert host.process.calls == [("docker", "--version")]


def test_rootful_podman_does_not_suggest_rootless_checks_passed(host, monkeypatch):
    monkeypatch.setattr(doctor.os, "getuid", lambda: 0)
    findings = check(host)
    assert findings["Account"].status == "warn"
    assert "Rootless helpers" not in findings and "Subordinate IDs" not in findings


def test_probe_execution_errors_do_not_abort_other_findings(host, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError()

    monkeypatch.setattr(host.process, "run", denied)
    findings = check(host)
    assert findings["Podman"].status == "error"
    assert findings["OCI runtime"].status == "ok"


@pytest.mark.parametrize(
    "runner,platform,uid",
    [("docker", "linux", 1000), ("podman", "darwin", 1000), ("podman", "linux", 0)],
)
def test_unsupported_gvisor_combinations_do_not_probe(
    host, monkeypatch, runner, platform, uid
):
    monkeypatch.setattr(doctor.sys, "platform", platform)
    monkeypatch.setattr(doctor.os, "getuid", lambda: uid)
    findings = check(host, runner=runner, isolation=IsolationMode.GVISOR)
    assert any(f.status == "error" for f in findings.values())
    assert not host.process.calls


def test_gvisor_host_network_is_rejected_without_probing(host):
    config = AppConfig(Defaults(network=NetworkMode.HOST), {})
    assert (
        check(host, config, isolation=IsolationMode.GVISOR)["Networking"].status
        == "error"
    )
    assert not host.process.calls
