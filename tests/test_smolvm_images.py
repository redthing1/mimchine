from __future__ import annotations

import json
from pathlib import Path

from mimchine.domain import ImageSource
from mimchine.process import ProcessResult
from mimchine.smolvm_images import SmolvmImageImporter


class RecordingRunner:
    def __init__(self, *, imported: bool = False):
        self.calls: list[tuple[str, ...]] = []
        self.imported = imported

    def run(self, args, *, capture=False, foreground=False, check=True):
        command = tuple(str(arg) for arg in args)
        self.calls.append(command)
        if command[:4] == ("podman", "image", "inspect", "--format"):
            return ProcessResult(
                command,
                0,
                stdout=json.dumps(
                    [
                        {
                            "Id": "sha256:abc",
                            "Os": "linux",
                            "Architecture": "amd64",
                        }
                    ]
                ),
            )
        if command == ("smolvm", "image", "ls", "--json"):
            images = (
                [
                    {
                        "reference": "mim_codex",
                        "source_id": "sha256:abc",
                        "os": "linux",
                        "architecture": "amd64",
                    }
                ]
                if self.imported
                else []
            )
            return ProcessResult(command, 0, stdout=json.dumps({"images": images}))
        if command[:2] == ("podman", "save"):
            Path(command[command.index("-o") + 1]).write_text("archive", encoding="utf-8")
            return ProcessResult(command, 0)
        if command[:3] == ("smolvm", "image", "import"):
            return ProcessResult(command, 0)
        if command[:3] == ("smolvm", "image", "prune"):
            return ProcessResult(
                command,
                0,
                stdout=json.dumps(
                    {
                        "refs": 3,
                        "entries": 1,
                        "staging_entries": 2,
                        "bytes_reclaimable": 1024,
                        "dry_run": "--dry-run" in command,
                    }
                ),
            )
        return ProcessResult(command, 1, stderr="unexpected command")


def test_imports_local_podman_image_into_smolvm(tmp_path: Path) -> None:
    runner = RecordingRunner()
    importer = SmolvmImageImporter(tmp_path, runner=runner)

    importer.materialize(ImageSource.oci_reference("mim_codex"), builder="podman")

    assert ("podman", "save") == runner.calls[2][:2]
    import_call = runner.calls[3]
    assert import_call[:3] == ("smolvm", "image", "import")
    assert "--oci-archive" in import_call
    assert ("--tag", "mim_codex") == (
        import_call[import_call.index("--tag")],
        import_call[import_call.index("--tag") + 1],
    )
    assert ("--source-id", "sha256:abc") == (
        import_call[import_call.index("--source-id")],
        import_call[import_call.index("--source-id") + 1],
    )


def test_skips_import_when_smolvm_has_current_local_image(tmp_path: Path) -> None:
    runner = RecordingRunner(imported=True)
    importer = SmolvmImageImporter(tmp_path, runner=runner)

    importer.materialize(ImageSource.oci_reference("mim_codex"), builder="podman")

    assert ("podman", "save") not in [call[:2] for call in runner.calls]
    assert ("smolvm", "image", "import") not in [call[:3] for call in runner.calls]


def test_leaves_missing_local_image_as_registry_reference(tmp_path: Path) -> None:
    class MissingRunner(RecordingRunner):
        def run(self, args, *, capture=False, foreground=False, check=True):
            command = tuple(str(arg) for arg in args)
            if command[:4] == ("podman", "image", "inspect", "--format"):
                return ProcessResult(command, 1, stderr="not found")
            return super().run(args, capture=capture, foreground=foreground, check=check)

    runner = MissingRunner()
    importer = SmolvmImageImporter(tmp_path, runner=runner)
    image = ImageSource.oci_reference("ghcr.io/org/app:latest")

    importer.materialize(image, builder="podman")
    assert ("podman", "save") not in [call[:2] for call in runner.calls]


def test_prune_delegates_to_smolvm(tmp_path: Path) -> None:
    runner = RecordingRunner()
    importer = SmolvmImageImporter(tmp_path, runner=runner)

    result = importer.prune(dry_run=True)

    assert ("--unused" in runner.calls[-1])
    assert result.image_refs == 3
    assert result.image_entries == 1
    assert result.staging_entries == 2
    assert result.bytes_reclaimable == 1024
    assert result.dry_run is True
