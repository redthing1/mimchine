import os
import tempfile
import unittest
from unittest.mock import ANY, patch

from mimchine import containers


class ImageArchiveTests(unittest.TestCase):
    def test_build_image_save_command_uses_docker_archive_for_podman(self):
        with patch("mimchine.containers.get_container_runtime", return_value="podman"):
            self.assertEqual(
                containers._build_image_save_command("example"),
                ["podman", "save", "--format", "docker-archive", "example"],
            )

    def test_build_image_save_command_stays_minimal_for_docker(self):
        with patch("mimchine.containers.get_container_runtime", return_value="docker"):
            self.assertEqual(
                containers._build_image_save_command("example"),
                ["docker", "save", "example"],
            )

    def test_export_image_archive_uses_zstd_pipeline_for_zst(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, "example.tar.zst")

            with (
                patch(
                    "mimchine.containers.get_container_runtime", return_value="docker"
                ),
                patch("mimchine.containers._require_zstd") as require_zstd,
                patch(
                    "mimchine.containers._run_pipeline_to_file"
                ) as run_pipeline_to_file,
                patch("mimchine.containers.os.replace") as replace,
            ):
                containers.export_image_archive("example", output_path)

            require_zstd.assert_called_once_with()
            run_pipeline_to_file.assert_called_once_with(
                ["docker", "save", "example"],
                ["zstd", *containers.DEFAULT_ZSTD_EXPORT_ARGS],
                ANY,
                error_action="image export",
            )
            temp_output_path = run_pipeline_to_file.call_args.args[2]
            replace.assert_called_once_with(
                temp_output_path, os.path.abspath(output_path)
            )

    def test_export_image_archive_rejects_existing_output_without_force(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, "example.tar")
            with open(output_path, "wb"):
                pass

            with self.assertRaisesRegex(ValueError, "already exists"):
                containers.export_image_archive("example", output_path)

    def test_export_image_archive_rejects_directory_output_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, "example.tar")
            os.mkdir(output_path)

            with self.assertRaisesRegex(ValueError, "is a directory"):
                containers.export_image_archive("example", output_path)

    def test_import_image_archive_uses_runtime_input_for_tar(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = os.path.join(temp_dir, "example.tar")
            with open(input_path, "wb"):
                pass

            with (
                patch(
                    "mimchine.containers.get_container_runtime", return_value="podman"
                ),
                patch("mimchine.containers._run_process") as run_process,
            ):
                containers.import_image_archive(input_path)

            run_process.assert_called_once_with(
                ["podman", "load", "--input", os.path.abspath(input_path)],
                error_action="image import",
            )

    def test_import_image_archive_uses_zstd_pipeline_for_zst(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = os.path.join(temp_dir, "example.tar.zst")
            with open(input_path, "wb"):
                pass

            with (
                patch(
                    "mimchine.containers.get_container_runtime", return_value="docker"
                ),
                patch("mimchine.containers._require_zstd") as require_zstd,
                patch("mimchine.containers._run_pipeline") as run_pipeline,
            ):
                containers.import_image_archive(input_path)

            require_zstd.assert_called_once_with()
            run_pipeline.assert_called_once_with(
                ["zstd", "-d", "-c", os.path.abspath(input_path)],
                ["docker", "load"],
                error_action="image import",
            )

    def test_import_image_archive_rejects_unsupported_suffix(self):
        with self.assertRaisesRegex(ValueError, "unsupported archive path"):
            containers.import_image_archive("/tmp/example.tar.gz")


if __name__ == "__main__":
    unittest.main()
