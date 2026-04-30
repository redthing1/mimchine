import os
import tempfile
import unittest
from unittest.mock import patch

import typer

from mimchine import cli, containers


class CreateCommandTests(unittest.TestCase):
    def test_create_uses_image_identity_for_podman_userns_and_home_share(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            host_home = os.path.join(temp_dir, "home")
            downloads_dir = os.path.join(host_home, "Downloads")
            data_dir = os.path.join(temp_dir, "data")
            os.makedirs(downloads_dir)

            with (
                patch("mimchine.cli.DATA_DIR", data_dir),
                patch("mimchine.cli.container_exists", return_value=False),
                patch("mimchine.cli.image_exists", return_value=True),
                patch("mimchine.cli.get_container_runtime", return_value="podman"),
                patch(
                    "mimchine.cli.resolve_image_identity",
                    return_value=containers.ImageIdentity(
                        home_dir="/home/user",
                        uid=1000,
                        gid=1000,
                    ),
                ),
                patch("mimchine.cli.get_home_dir", return_value=host_home),
                patch("mimchine.cli.get_container_integration_mounts", return_value=[]),
                patch("mimchine.cli._run_container_cmd") as run_container_cmd,
            ):
                cli.create(
                    image_name="example",
                    container_name="example",
                    home_shares=[downloads_dir],
                    port_binds=[],
                    custom_mounts=[],
                    devices=[],
                    host_pid=False,
                    host_net=False,
                    privileged=False,
                    keepalive_command=None,
                    integrate_home=False,
                )

            self.assertEqual(
                run_container_cmd.call_args.args,
                (
                    "create",
                    "--name",
                    "example",
                    "--init",
                    "--label",
                    "mim=1",
                    "--userns",
                    "keep-id:uid=1000,gid=1000",
                    "-v",
                    f"{downloads_dir}:{downloads_dir}",
                    "-v",
                    f"{downloads_dir}:/home/user/Downloads",
                    "example",
                ),
            )
            self.assertEqual(
                run_container_cmd.call_args.kwargs,
                {
                    "error_action": "create",
                    "format_output": True,
                },
            )

    def test_create_adds_host_network(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = os.path.join(temp_dir, "data")

            with (
                patch("mimchine.cli.DATA_DIR", data_dir),
                patch("mimchine.cli.container_exists", return_value=False),
                patch("mimchine.cli.image_exists", return_value=True),
                patch("mimchine.cli.get_container_runtime", return_value="podman"),
                patch(
                    "mimchine.cli.resolve_image_identity",
                    return_value=containers.ImageIdentity(
                        home_dir="/home/user",
                        uid=1000,
                        gid=1000,
                    ),
                ),
                patch("mimchine.cli.get_container_integration_mounts", return_value=[]),
                patch("mimchine.cli._run_container_cmd") as run_container_cmd,
            ):
                cli.create(
                    image_name="example",
                    container_name="example",
                    home_shares=[],
                    port_binds=[],
                    custom_mounts=[],
                    devices=[],
                    host_pid=False,
                    host_net=True,
                    privileged=False,
                    keepalive_command=None,
                    integrate_home=False,
                )

            self.assertEqual(
                run_container_cmd.call_args.args,
                (
                    "create",
                    "--name",
                    "example",
                    "--init",
                    "--label",
                    "mim=1",
                    "--userns",
                    "keep-id:uid=1000,gid=1000",
                    "--network=host",
                    "example",
                ),
            )

    def test_create_rejects_host_network_with_port_binds(self):
        with (
            patch("mimchine.cli.container_exists", return_value=False),
            patch("mimchine.cli.image_exists", return_value=True),
            patch("mimchine.cli._run_container_cmd") as run_container_cmd,
        ):
            with self.assertRaises(typer.Exit) as exc:
                cli.create(
                    image_name="example",
                    container_name="example",
                    home_shares=[],
                    port_binds=["8080:80"],
                    custom_mounts=[],
                    devices=[],
                    host_pid=False,
                    host_net=True,
                    privileged=False,
                    keepalive_command=None,
                    integrate_home=False,
                )

        self.assertEqual(exc.exception.exit_code, 1)
        run_container_cmd.assert_not_called()


class StartCommandTests(unittest.TestCase):
    def test_start_aborts_before_runtime_call_when_preflight_fails(self):
        with (
            patch("mimchine.cli._require_mim_container"),
            patch("mimchine.cli.container_is_running", return_value=False),
            patch(
                "mimchine.cli.ensure_runtime_supports_containers",
                side_effect=RuntimeError("missing rootless helper"),
            ),
            patch("mimchine.cli._run_container_cmd") as run_container_cmd,
        ):
            with self.assertRaises(typer.Exit) as exc:
                cli.start(container_name="example")

        self.assertEqual(exc.exception.exit_code, 1)
        run_container_cmd.assert_not_called()


if __name__ == "__main__":
    unittest.main()
