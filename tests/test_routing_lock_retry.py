import unittest

from sqlalchemy.exc import OperationalError

from app.services.federation import routing


class RoutingLockRetryTests(unittest.TestCase):
    @staticmethod
    def operational(code: int) -> OperationalError:
        original = RuntimeError(code, "database error")
        return OperationalError("statement", {}, original)

    def test_deadlock_is_retryable(self):
        self.assertTrue(routing._retryable_mysql_lock_error(self.operational(1213)))

    def test_lock_wait_timeout_is_retryable(self):
        self.assertTrue(routing._retryable_mysql_lock_error(self.operational(1205)))

    def test_other_operational_errors_are_not_retryable(self):
        self.assertFalse(routing._retryable_mysql_lock_error(self.operational(1045)))

    def test_non_operational_error_is_not_retryable(self):
        self.assertFalse(routing._retryable_mysql_lock_error(RuntimeError(1213, "not sqlalchemy")))


if __name__ == "__main__":
    unittest.main()
