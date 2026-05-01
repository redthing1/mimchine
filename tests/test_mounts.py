import os
import tempfile
import unittest

from mimchine import mounts


class MountTests(unittest.TestCase):
    def test_parse_mount_spec_accepts_read_only_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            spec = mounts.parse_mount_spec(f"{temp_dir}:/refs:ro")

        self.assertEqual(spec.source_path, os.path.realpath(temp_dir))
        self.assertEqual(spec.container_path, "/refs")
        self.assertEqual(spec.mode, "ro")

    def test_parse_workspace_spec_defaults_target_and_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = os.path.join(temp_dir, "project")
            os.makedirs(workspace_dir)

            spec = mounts.parse_workspace_spec(workspace_dir)

        self.assertEqual(spec.source_path, os.path.realpath(workspace_dir))
        self.assertEqual(spec.container_path, "/work/project")
        self.assertEqual(spec.mode, "rw")

    def test_parse_workspace_spec_accepts_mode_without_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = os.path.join(temp_dir, "project")
            os.makedirs(workspace_dir)

            spec = mounts.parse_workspace_spec(f"{workspace_dir}:ro")

        self.assertEqual(spec.container_path, "/work/project")
        self.assertEqual(spec.mode, "ro")

    def test_parse_workspace_spec_rejects_files(self):
        with tempfile.NamedTemporaryFile() as temp_file:
            with self.assertRaisesRegex(ValueError, "not a directory"):
                mounts.parse_workspace_spec(temp_file.name)


if __name__ == "__main__":
    unittest.main()
