import unittest
from unittest.mock import patch

from mimchine import containers


class ContainerRuntimeTests(unittest.TestCase):
    def test_ensure_runtime_supports_containers_rejects_rootless_podman_without_helpers(
        self,
    ):
        podman_info = {
            "host": {
                "security": {
                    "rootless": True,
                },
                "slirp4netns": {
                    "executable": "",
                },
                "pasta": {
                    "executable": "",
                },
            }
        }

        with (
            patch("mimchine.containers.get_container_runtime", return_value="podman"),
            patch("mimchine.containers._get_podman_info", return_value=podman_info),
        ):
            with self.assertRaisesRegex(RuntimeError, "slirp4netns"):
                containers.ensure_runtime_supports_containers()

    def test_ensure_runtime_supports_containers_accepts_rootless_podman_with_helper(
        self,
    ):
        podman_info = {
            "host": {
                "security": {
                    "rootless": True,
                },
                "slirp4netns": {
                    "executable": "/usr/bin/slirp4netns",
                },
                "pasta": {
                    "executable": "",
                },
            }
        }

        with (
            patch("mimchine.containers.get_container_runtime", return_value="podman"),
            patch("mimchine.containers._get_podman_info", return_value=podman_info),
        ):
            containers.ensure_runtime_supports_containers()

    def test_resolve_image_identity_parses_probe_output(self):
        with patch(
            "mimchine.containers._probe_image_identity_output",
            return_value="/home/user\n1000\n1000\n",
        ):
            self.assertEqual(
                containers.resolve_image_identity("example"),
                containers.ImageIdentity(home_dir="/home/user", uid=1000, gid=1000),
            )

    def test_resolve_image_identity_rejects_invalid_home_directory(self):
        with patch(
            "mimchine.containers._probe_image_identity_output",
            return_value="home/user\n1000\n1000\n",
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid home directory"):
                containers.resolve_image_identity("example")


if __name__ == "__main__":
    unittest.main()
