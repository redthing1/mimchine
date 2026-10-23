from __future__ import annotations

import hashlib
import json
import platform

import pytest

from mimchine.gvisor import PROFILES, REQUIRED_FILES, find_runtime, verify_runtime


@pytest.fixture
def distribution(tmp_path):
    root = tmp_path / "release"
    files = {}
    for name in REQUIRED_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
        path.chmod(0o755 if name != "LICENSE" else 0o644)
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "version": "20261012.0-mim.2",
        "policy_version": 1,
        "source_commit": "a" * 40,
        "architecture": platform.machine(),
        "files": files,
        "profiles": {
            name: {"path": f"profiles/{name}/runsc", "flags": ["platform=systrap"]}
            for name in PROFILES
        },
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root


def rewrite_manifest(root, change):
    path = root / "manifest.json"
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


def test_runtime_identity_pins_canonical_installation_and_profile(
    distribution, tmp_path
):
    selector = tmp_path / "current"
    selector.symlink_to(distribution, target_is_directory=True)
    identity = verify_runtime(selector / "runsc", "offline")
    assert identity.distribution == distribution
    assert identity.entrypoint == distribution / "profiles/offline/runsc"
    assert identity.version == "20261012.0-mim.2"
    selector.unlink()
    selector.symlink_to(tmp_path / "future", target_is_directory=True)
    assert (
        verify_runtime(
            identity.distribution / "runsc",
            identity.profile,
            expected_manifest=identity.manifest_sha256,
        )
        == identity
    )


@pytest.mark.parametrize(
    "name", ["runsc", "gvisor-bin/gvisor_sentry", "profiles/offline/runsc"]
)
def test_changed_payload_is_rejected(distribution, name):
    (distribution / name).write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_runtime(distribution / "runsc", "offline")


def test_changed_manifest_cannot_replace_a_bound_identity(distribution):
    identity = verify_runtime(distribution / "runsc", "offline")
    rewrite_manifest(distribution, lambda data: data.update(source_commit="b" * 40))
    with pytest.raises(ValueError, match="bound identity"):
        verify_runtime(
            distribution / "runsc",
            "offline",
            expected_manifest=identity.manifest_sha256,
        )


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda data: data.update(policy_version=2), "policy version"),
        (lambda data: data.update(policy_version=True), "policy version"),
        (lambda data: data.update(architecture="other"), "architecture"),
        (lambda data: data["files"].pop("gvisor-bin/checkpointgofer"), "incomplete"),
        (lambda data: data["files"].update({"../outside": "0" * 64}), "file path"),
        (
            lambda data: data["profiles"]["offline"].update(path="runsc"),
            "profile settings",
        ),
        (lambda data: data.update(profiles=list(PROFILES)), "runtime profiles"),
        (lambda data: data["profiles"].update(offline=[]), "profile settings"),
    ],
)
def test_unusable_manifests_are_rejected(distribution, change, message):
    rewrite_manifest(distribution, change)
    with pytest.raises(ValueError, match=message):
        verify_runtime(distribution / "runsc", "offline")


def test_internal_symlink_is_not_part_of_an_immutable_distribution(
    distribution, tmp_path
):
    sidecar = distribution / "gvisor-bin/checkpointgofer"
    target = tmp_path / "sidecar"
    sidecar.rename(target)
    sidecar.symlink_to(target)
    with pytest.raises(ValueError, match="regular file"):
        verify_runtime(distribution / "runsc", "offline")


def test_duplicate_manifest_keys_are_rejected(distribution):
    path = distribution / "manifest.json"
    path.write_text('{"policy_version":1,' + path.read_text()[1:])
    with pytest.raises(ValueError, match="duplicate manifest key"):
        verify_runtime(distribution / "runsc", "offline")


def test_distribution_requires_executable_payload(distribution):
    (distribution / "gvisor-bin/gvisor_sentry").chmod(0o644)
    with pytest.raises(ValueError, match="execute permission"):
        verify_runtime(distribution / "runsc", "offline")


def test_discovery_selects_numeric_release_order_without_running_anything(tmp_path):
    for name in (
        "20261012.0-mim.9",
        "20261012.0-mim.10",
        "20260930.1-mim.20",
        "current",
        "unrelated",
    ):
        (tmp_path / name).mkdir()
    assert (
        find_runtime(None, directory=tmp_path) == tmp_path / "20261012.0-mim.10/runsc"
    )


def test_explicit_runtime_does_not_require_or_search_default_installation(tmp_path):
    configured = tmp_path / "custom/runsc"
    assert find_runtime(configured, directory=tmp_path / "missing") == configured


def test_stable_package_takes_precedence_over_legacy_releases(distribution):
    (distribution / "20261012.0-mim.99").mkdir()
    selected = find_runtime(None, directory=distribution)
    assert selected == distribution / "runsc"
    assert verify_runtime(selected, "offline").distribution == distribution


@pytest.mark.parametrize("missing", ["runsc", "manifest.json"])
def test_broken_stable_package_does_not_fall_back(distribution, missing):
    (distribution / "20261012.0-mim.99").mkdir()
    (distribution / missing).unlink()
    selected = find_runtime(None, directory=distribution)
    assert selected == distribution / "runsc"
    with pytest.raises(ValueError, match="cannot verify"):
        verify_runtime(selected, "offline")


def test_missing_installation_has_an_actionable_error(tmp_path):
    with pytest.raises(ValueError, match="install a runtime package"):
        find_runtime(None, directory=tmp_path / "missing")


def test_broken_latest_installation_does_not_fall_back(distribution, tmp_path):
    directory = tmp_path / "installed"
    directory.mkdir()
    distribution.rename(directory / "20261012.0-mim.2")
    (directory / "20261012.0-mim.3").mkdir()
    selected = find_runtime(None, directory=directory)
    with pytest.raises(ValueError, match="cannot verify"):
        verify_runtime(selected, "offline")


@pytest.mark.parametrize("target", ["runsc", "manifest.json", "gvisor-bin"])
def test_other_writers_cannot_modify_a_verified_distribution(distribution, target):
    path = distribution / target
    path.chmod(path.stat().st_mode | 0o020)
    with pytest.raises(ValueError, match="untrusted writes|other writers"):
        verify_runtime(distribution / "runsc", "offline")


def test_runtime_cannot_be_modified_through_a_hard_linked_workspace_alias(
    distribution, tmp_path
):
    (tmp_path / "alias").hardlink_to(distribution / "runsc")
    with pytest.raises(ValueError, match="singly linked"):
        verify_runtime(distribution / "runsc", "offline")
