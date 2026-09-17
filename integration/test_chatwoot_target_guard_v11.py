"""Primary CI wrapper for target-guard and real-world lifecycle adversarial suites.

The two suites run in separate interpreters because both deliberately reconfigure
module-level runtime state and temporary SQLite locations. Keeping process isolation
makes the primary Validate stack exercise exactly the same scenarios as the focused
adversarial workflow without allowing test-order leakage to create false positives.
"""
import subprocess
import sys
import unittest


class ChatwootTargetGuardAndRealityV12Tests(unittest.TestCase):
    def run_suite(self, module_name: str) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "unittest", module_name],
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            self.fail(
                f"{module_name} failed with exit {result.returncode}\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )

    def test_chatwoot_target_guard_v11(self):
        self.run_suite("test_chatwoot_target_guard_core_v11.py")

    def test_conversation_lifecycle_reality_v12(self):
        self.run_suite("test_conversation_lifecycle_reality_v12.py")


if __name__ == "__main__":
    unittest.main()
