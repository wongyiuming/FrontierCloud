from contextlib import asynccontextmanager
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import ip_security


class SummaryTests(unittest.IsolatedAsyncioTestCase):
    def test_api_counter_includes_nested_routers_without_private_wrapper_access(self):
        from fastapi import APIRouter, FastAPI

        app = FastAPI()
        root, child = APIRouter(), APIRouter()

        @child.get("/item")
        async def get_item():
            return {}

        @child.post("/item")
        async def create_item():
            return {}

        @child.get("/view", include_in_schema=False)
        async def html_view():
            return "view"

        root.include_router(child, prefix="/nested")
        app.include_router(root, prefix="/api/v1")
        self.assertEqual(ip_security.legal_api_count(app), 2)

    def test_running_application_api_count_is_not_zero(self):
        from main import app
        self.assertGreater(ip_security.legal_api_count(app), 0)

    async def test_one_summary_projection_splits_white_ips_without_duplicate_history(self):
        count, stats, rows = MagicMock(), MagicMock(), MagicMock()
        count.scalar_one.return_value = 3
        stats.mappings.return_value.one.return_value = {"active_count": 1, "whitelist_count": 1}
        rows.mappings.return_value.all.return_value = [
            {"ip_address": "10.199.254.235", "status": "whitelisted", "note": "trusted"},
            {"ip_address": "13.11.1.1", "status": "active", "ban_count": 8,
             "ban_kind": "permanent", "reason": "repeat"},
            {"ip_address": "2001:db8::1", "status": "observed", "ban_count": 0,
             "ban_kind": None, "reason": None},
        ]
        conn = AsyncMock()
        conn.execute.side_effect = [count, stats, rows]

        @asynccontextmanager
        async def connect():
            yield conn

        with patch.object(ip_security, "engine") as engine:
            engine.connect = connect
            result = await ip_security.list_security_summary(ip_order="desc", page=2, page_size=2)
        ips = [item["ip"] for item in result["events"] + result["whitelist"]]
        self.assertEqual(len(set(ips)), 3)
        self.assertEqual(result["pagination"]["total"], 3)
        self.assertEqual(result["events"][0]["ban_count"], 8)
        self.assertNotIn("banned_at", result["events"][0])
        sql, params = conn.execute.call_args.args
        self.assertIn("INET6_ATON(ip_address) desc", str(sql))
        self.assertEqual(params["offset"], 2)

    async def test_sort_direction_is_validated_before_interpolation(self):
        with self.assertRaises(ValueError):
            await ip_security.list_security_summary(ip_order="desc; DROP TABLE x")

    async def test_whitelist_removal_evidence_is_written_inside_state_transaction(self):
        conn = AsyncMock()

        async def transaction(operation, **_kwargs):
            await operation(conn)

        with patch.object(ip_security, "_run_state_transaction", side_effect=transaction):
            await ip_security.remove_whitelist("203.0.113.72", "a" * 64)
        statements = [str(call.args[0]) for call in conn.execute.call_args_list]
        self.assertIn("DELETE FROM ip_permanent_whitelist", statements[-2])
        self.assertIn("INSERT INTO ip_security_audit_log", statements[-1])
        self.assertEqual(conn.execute.call_args.args[1]["action"], "whitelist_remove")
