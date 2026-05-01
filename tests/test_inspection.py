import unittest

from mimchine.inspection import build_container_inspection


class InspectionTests(unittest.TestCase):
    def test_build_container_inspection_summarizes_mount_ports_and_env_keys(self):
        inspect_data = {
            "Name": "/example",
            "Config": {
                "Image": "example:latest",
                "Env": ["HOME=/home/user", "TOKEN=secret"],
            },
            "State": {"Status": "running"},
            "HostConfig": {
                "NetworkMode": "default",
                "PidMode": "private",
                "Privileged": False,
            },
            "Mounts": [
                {
                    "Source": "/host/project",
                    "Destination": "/work/project",
                    "RW": False,
                }
            ],
            "NetworkSettings": {
                "Ports": {
                    "8080/tcp": [
                        {
                            "HostIp": "127.0.0.1",
                            "HostPort": "18080",
                        }
                    ]
                }
            },
        }

        inspection = build_container_inspection(
            "example",
            "podman",
            "/tmp/mim/example",
            inspect_data,
        )

        self.assertIn(("state", "running"), inspection.basics)
        self.assertEqual(
            inspection.mounts,
            [("/host/project", "/work/project", "ro")],
        )
        self.assertEqual(inspection.ports, [("8080/tcp", "127.0.0.1:18080")])
        self.assertEqual(inspection.env_keys, [("HOME",), ("TOKEN",)])


if __name__ == "__main__":
    unittest.main()
