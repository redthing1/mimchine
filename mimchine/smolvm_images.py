from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .domain import ImageSource
from .process import ProcessRunner


@dataclass(frozen=True)
class LocalImage:
    reference: str
    image_id: str
    os: str
    architecture: str


@dataclass(frozen=True)
class PruneResult:
    image_refs: int
    image_entries: int
    staging_entries: int
    bytes_reclaimable: int
    dry_run: bool


class SmolvmImageImporter:
    def __init__(self, staging_root: Path, runner: ProcessRunner | None = None):
        self.staging_root = staging_root
        self.runner = runner or ProcessRunner()

    def materialize(self, image: ImageSource, *, builder: str) -> None:
        local = self._inspect_local_image(builder, image.value)
        if local is None:
            return
        if builder != "podman":
            raise ValueError(
                "local smolvm image imports currently support podman; "
                "use a registry image or pass a .smolmachine artifact"
            )
        if self._already_imported(local):
            return

        self.staging_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="smolvm-image.", dir=self.staging_root
        ) as staging:
            archive = Path(staging) / "image.oci.tar"
            self.runner.run(
                [
                    "podman",
                    "save",
                    "--format",
                    "oci-archive",
                    "--uncompressed",
                    "-o",
                    str(archive),
                    image.value,
                ],
                foreground=True,
            )
            self.runner.run(
                [
                    "smolvm",
                    "image",
                    "import",
                    "--oci-archive",
                    str(archive),
                    "--tag",
                    image.value,
                    "--source-id",
                    local.image_id,
                ],
                foreground=True,
            )

    def prune(self, *, dry_run: bool) -> PruneResult:
        result = self.runner.run(
            [
                "smolvm",
                "image",
                "prune",
                "--unused",
                "--json",
                *(["--dry-run"] if dry_run else []),
            ],
            capture=True,
        )
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("could not parse smolvm image prune output") from exc
        return PruneResult(
            image_refs=_int(data, "refs"),
            image_entries=_int(data, "entries"),
            staging_entries=_int(data, "staging_entries"),
            bytes_reclaimable=_int(data, "bytes_reclaimable"),
            dry_run=bool(data.get("dry_run", dry_run)),
        )

    def _already_imported(self, image: LocalImage) -> bool:
        result = self.runner.run(
            ["smolvm", "image", "ls", "--json"],
            capture=True,
            check=False,
        )
        if result.returncode != 0:
            return False
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return False
        images = data.get("images", [])
        if not isinstance(images, list):
            return False
        platform = f"{image.os}/{image.architecture}"
        for imported in images:
            if not isinstance(imported, dict):
                continue
            imported_platform = (
                f"{imported.get('os', '')}/{imported.get('architecture', '')}"
            )
            if (
                imported.get("reference") == image.reference
                and imported.get("source_id") == image.image_id
                and imported_platform == platform
            ):
                return True
        return False

    def _inspect_local_image(self, builder: str, image: str) -> LocalImage | None:
        result = self.runner.run(
            [builder, "image", "inspect", "--format", "json", image],
            capture=True,
            check=False,
        )
        if result.returncode != 0:
            return None

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError(f"could not parse {builder} image inspect output") from exc

        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            raise ValueError(f"unexpected {builder} image inspect output")

        return LocalImage(
            reference=image,
            image_id=_required_text(data, "Id", "ID"),
            os=_required_text(data, "Os", "OS"),
            architecture=_required_text(data, "Architecture"),
        )


def _required_text(data: dict[str, object], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    raise ValueError(f"image inspect output is missing [{keys[0]}]")


def _int(data: dict[str, object], key: str) -> int:
    value = data.get(key, 0)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    raise ValueError(f"smolvm image prune output has invalid [{key}]")
