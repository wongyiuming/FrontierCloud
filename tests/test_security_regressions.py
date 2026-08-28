import unittest

from app.core.admin_log import sanitize_log_value


class XSSAndLogIntegrityTests(unittest.TestCase):
    def test_log_values_cannot_inject_new_records_or_terminal_controls(self):
        value = sanitize_log_value("safe\n[ADMIN] forged\x1b[2J")
        self.assertEqual(value, "safe\\n[ADMIN] forged\\u{1b}[2J")
        self.assertNotIn("\n", value)
        self.assertNotIn("\x1b", value)


if __name__ == "__main__":
    unittest.main()
