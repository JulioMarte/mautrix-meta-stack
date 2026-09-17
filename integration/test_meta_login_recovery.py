import unittest

from meta_login_recovery import is_missing_login_process
from meta_provisioning import ProvisioningError


class MetaLoginRecoveryTests(unittest.TestCase):
    def test_login_not_found_404_is_recoverable_stale_process(self):
        exc = ProvisioningError("Login not found", errcode="M_NOT_FOUND", status_code=404)
        self.assertTrue(is_missing_login_process(exc))

    def test_other_provisioning_failures_are_not_misclassified(self):
        cases = [
            ProvisioningError("Invalid auth token", errcode="M_UNKNOWN_TOKEN", status_code=401),
            ProvisioningError("Flow not found", errcode="M_NOT_FOUND", status_code=404),
            ProvisioningError("Internal server error", errcode="M_UNKNOWN", status_code=500),
            RuntimeError("Login not found"),
        ]
        for exc in cases:
            with self.subTest(exc=repr(exc)):
                self.assertFalse(is_missing_login_process(exc))


if __name__ == "__main__":
    unittest.main()
