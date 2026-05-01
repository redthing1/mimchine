import unittest
from unittest.mock import patch

from mimchine import profiles


class ProfileTests(unittest.TestCase):
    def test_load_profile_accepts_minimal_profile(self):
        config = {
            "profiles": {
                "agentish": {
                    "workspace": "~/src",
                    "mounts": ["~/.codex:/home/user/.codex:rw"],
                    "network": "none",
                }
            }
        }

        with patch("mimchine.profiles.load_config", return_value=config):
            profile = profiles.load_profile("agentish")

        self.assertEqual(profile.name, "agentish")
        self.assertEqual(profile.workspaces, ("~/src",))
        self.assertEqual(profile.mounts, ("~/.codex:/home/user/.codex:rw",))
        self.assertEqual(profile.network, "none")

    def test_load_profile_rejects_unknown_fields(self):
        config = {
            "profiles": {
                "agentish": {
                    "workspace": "~/src",
                    "surprise": True,
                }
            }
        }

        with patch("mimchine.profiles.load_config", return_value=config):
            with self.assertRaisesRegex(ValueError, "unknown field"):
                profiles.load_profile("agentish")


if __name__ == "__main__":
    unittest.main()
