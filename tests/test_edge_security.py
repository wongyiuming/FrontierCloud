from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.services.edge_security import write_snapshot


class EdgeSnapshotTests(unittest.TestCase):
    def test_numeric_order_whitelist_exemptions_and_permanent_expiry(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = [{"ip_address": ip, "ban_kind": "permanent", "expires_at": now}
                for ip in ["13.11.1.1", "10.199.254.235", "203.0.113.1", "127.0.0.1", "2001:db8::1"]]
        rows.append({"ip_address": "203.0.113.2", "ban_kind": "auto",
                     "expires_at": now + timedelta(hours=24)})
        with TemporaryDirectory() as directory:
            path = Path(directory) / "active.tsv"
            write_snapshot(rows, ["203.0.113.1"], path)
            content = path.read_text()
            self.assertTrue(content.startswith("# frontiercloud-ip-security-v1\n10.199.254.235\t0\n13.11.1.1\t0\n"))
            self.assertNotIn("127.0.0.1", content)
            self.assertNotIn("203.0.113.1", content)
            self.assertIn("2001:db8::1\t0", content)
            self.assertNotIn("203.0.113.2\t0", content)
            with patch("app.services.edge_security.os.replace") as replace:
                write_snapshot(rows, ["203.0.113.1"], path)
                replace.assert_not_called()

    def test_failed_write_preserves_previous_snapshot_and_cleans_temporary_file(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "active.tsv"
            write_snapshot([], [], path)
            previous = path.read_bytes()
            row = {"ip_address": "203.0.113.8", "ban_kind": "permanent", "expires_at": None}
            with patch("app.services.edge_security.os.replace", side_effect=OSError("disk failed")):
                with self.assertRaises(OSError):
                    write_snapshot([row], [], path)
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_invalid_address_cannot_inject_nginx_configuration(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "active.tsv"
            with self.assertRaises(ValueError):
                write_snapshot([{"ip_address": "1.2.3.4; include /tmp/x;"}], [], path)
            self.assertFalse(path.exists())
