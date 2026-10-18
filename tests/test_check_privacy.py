import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_privacy", ROOT / "scripts" / "check_privacy.py"
)
check_privacy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = check_privacy
SPEC.loader.exec_module(check_privacy)

BACKSLASH = chr(92)


class PrivacyRules(unittest.TestCase):
    def test_placeholders_are_clean(self):
        text = "~/.hermes 127.0.0.1 <test-host> noreply@github.com ...@im.bot"
        self.assertEqual(check_privacy.scan_text(text), [])

    def test_documentation_network_is_clean(self):
        self.assertEqual(check_privacy.scan_text("192.0.2.10"), [])

    def test_home_path_is_detected(self):
        findings = check_privacy.scan_text("/home/" + "alice/project")
        self.assertIn("unix-home-path", findings[0][0])

    def test_windows_user_path_is_detected(self):
        text = "C:" + BACKSLASH + "Users" + BACKSLASH + "alice" + BACKSLASH + "work"
        findings = check_privacy.scan_text(text)
        self.assertIn("windows-user-path", findings[0][0])

    def test_ssh_alias_is_detected(self):
        findings = check_privacy.scan_text("ssh " + "my-host")
        self.assertIn("ssh-alias", findings[0][0])

    def test_public_ip_is_detected(self):
        findings = check_privacy.scan_text(".".join(["8", "8", "8", "8"]))
        self.assertIn("ipv4-address", findings[0][0])

    def test_github_token_is_detected_and_masked(self):
        secret = "ghp_" + "A" * 36
        findings = check_privacy.scan_text(secret)
        self.assertIn("github-token", findings[0][0])
        self.assertNotIn(secret, findings[0][2])

    def test_private_key_block_is_detected(self):
        text = "-----BEGIN " + "RSA PRIVATE KEY-----" + "\n" + "A" * 64
        findings = check_privacy.scan_text(text)
        self.assertIn("private-key-block", findings[0][0])

    def test_placeholder_cloud_key_is_clean(self):
        self.assertEqual(check_privacy.scan_text("sk-" + "test0123456789abcdef"), [])

    def test_local_only_file_is_detected(self):
        self.assertEqual(
            check_privacy.check_path("local_" + "info.md"), "local-only-file"
        )

    def test_repository_is_clean(self):
        self.assertEqual(check_privacy.scan_files(ROOT), [])


if __name__ == "__main__":
    unittest.main()
