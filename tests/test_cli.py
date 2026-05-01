import os
import tempfile
import unittest
from unittest.mock import patch

import typer

from mimchine import cli, containers
from mimchine.profiles import Profile


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
                patch("mimchine.create_config.get_home_dir", return_value=host_home),
                patch("mimchine.cli.get_container_integration_mounts", return_value=[]),
                patch("mimchine.cli._run_container_cmd") as run_container_cmd,
            ):
                cli.create(
                    image_name="example",
                    container_name="example",
                    profile_name=None,
                    workspaces=[],
                    home_shares=[downloads_dir],
                    port_binds=[],
                    mounts=[],
                    devices=[],
                    host_pid=False,
                    network=None,
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
                    f"{downloads_dir}:{downloads_dir}:rw",
                    "-v",
                    f"{downloads_dir}:/home/user/Downloads:rw",
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
                    profile_name=None,
                    workspaces=[],
                    home_shares=[],
                    port_binds=[],
                    mounts=[],
                    devices=[],
                    host_pid=False,
                    network="host",
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
                    profile_name=None,
                    workspaces=[],
                    home_shares=[],
                    port_binds=["8080:80"],
                    mounts=[],
                    devices=[],
                    host_pid=False,
                    network="host",
                    privileged=False,
                    keepalive_command=None,
                    integrate_home=False,
                )

        self.assertEqual(exc.exception.exit_code, 1)
        run_container_cmd.assert_not_called()

    def test_create_adds_workspace_and_read_only_mounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = os.path.join(temp_dir, "project")
            refs_dir = os.path.join(temp_dir, "refs")
            data_dir = os.path.join(temp_dir, "data")
            os.makedirs(workspace_dir)
            os.makedirs(refs_dir)

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
                    profile_name=None,
                    workspaces=[workspace_dir],
                    home_shares=[],
                    port_binds=[],
                    mounts=[f"{refs_dir}:/refs:ro"],
                    devices=[],
                    host_pid=False,
                    network=None,
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
                    f"{workspace_dir}:/work/project:rw",
                    "-v",
                    f"{refs_dir}:/refs:ro",
                    "example",
                ),
            )

    def test_create_applies_profile_before_cli_options(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile_workspace = os.path.join(temp_dir, "profile-work")
            cli_workspace = os.path.join(temp_dir, "cli-work")
            data_dir = os.path.join(temp_dir, "data")
            os.makedirs(profile_workspace)
            os.makedirs(cli_workspace)

            profile = Profile(
                name="agentish",
                workspaces=(f"{profile_workspace}:ro",),
                mounts=(),
                home_shares=(),
                port_binds=(),
                devices=(),
                network="none",
                host_pid=False,
                privileged=False,
                integrate_home=False,
                keepalive_command=None,
            )

            with (
                patch("mimchine.cli.DATA_DIR", data_dir),
                patch("mimchine.cli.container_exists", return_value=False),
                patch("mimchine.cli.image_exists", return_value=True),
                patch("mimchine.cli.get_container_runtime", return_value="podman"),
                patch("mimchine.cli.load_profile", return_value=profile),
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
                    profile_name="agentish",
                    workspaces=[cli_workspace],
                    home_shares=[],
                    port_binds=[],
                    mounts=[],
                    devices=[],
                    host_pid=False,
                    network=None,
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
                    "--network=none",
                    "-v",
                    f"{profile_workspace}:/work/profile-work:ro",
                    "-v",
                    f"{cli_workspace}:/work/cli-work:rw",
                    "example",
                ),
            )


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


class EnterCommandTests(unittest.TestCase):
    def test_enter_creates_missing_container_then_shells_in(self):
        with (
            patch("mimchine.cli.container_exists", return_value=False),
            patch("mimchine.cli._create_container", return_value="example") as create,
            patch("mimchine.cli._shell_container") as shell,
        ):
            cli.enter_container(
                image_name="example-image",
                container_name="example",
                profile_name=None,
                workspaces=["/tmp"],
                home_shares=[],
                port_binds=[],
                mounts=[],
                devices=[],
                host_pid=False,
                network="none",
                privileged=False,
                keepalive_command=None,
                integrate_home=False,
                shell_command="sh",
                as_root=False,
                as_user=False,
            )

        create.assert_called_once()
        self.assertEqual(create.call_args.args[0], "example-image")
        self.assertEqual(create.call_args.args[1], "example")
        self.assertEqual(create.call_args.args[2].workspaces, ("/tmp",))
        self.assertEqual(create.call_args.args[2].network, "none")
        shell.assert_called_once_with("example", "sh", False, False)

    def test_enter_existing_container_does_not_create(self):
        with (
            patch("mimchine.cli.container_exists", return_value=True),
            patch("mimchine.cli._create_container") as create,
            patch("mimchine.cli._shell_container") as shell,
        ):
            cli.enter_container(
                image_name=None,
                container_name="example",
                profile_name=None,
                workspaces=[],
                home_shares=[],
                port_binds=[],
                mounts=[],
                devices=[],
                host_pid=False,
                network=None,
                privileged=False,
                keepalive_command=None,
                integrate_home=False,
                shell_command="sh",
                as_root=False,
                as_user=False,
            )

        create.assert_not_called()
        shell.assert_called_once_with("example", "sh", False, False)


if __name__ == "__main__":
    unittest.main()
