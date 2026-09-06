"""Real MySQL regression checks using isolated, uniquely named fixture tables."""
import asyncio
from contextlib import asynccontextmanager
import inspect
import re
import time
import uuid
from unittest.mock import AsyncMock, patch

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core import db
from app.services import ip_security as s


async def main():
    prefix = "fc_security_test_" + uuid.uuid4().hex[:12] + "_"
    tables = ["ip_auto_ban_events", "ip_permanent_whitelist", "ip_security_locks", "ip_security_audit_log"]
    created = []
    async with db.engine.connect() as physical:
        class FixtureConnection:
            def rewrite(self, statement):
                sql = str(statement)
                for table in tables:
                    sql = re.sub(r"\b" + table + r"\b", prefix + table, sql)
                return text(sql)

            async def execute(self, statement, params=None):
                return await physical.execute(self.rewrite(statement), params or {})

            async def scalar(self, statement, params=None):
                return await physical.scalar(self.rewrite(statement), params or {})

            def begin(self):
                return physical.begin()

            async def commit(self):
                await physical.commit()

            async def rollback(self):
                await physical.rollback()

        conn = FixtureConnection()
        try:
            for table in tables:
                ddl = re.search(r'CREATE TABLE IF NOT EXISTS ' + table + r'\s*\(.*?"""',
                                inspect.getsource(db.init_db), re.S).group(0)[:-3]
                await conn.execute(text(ddl.replace("CREATE TABLE IF NOT EXISTS", "CREATE TABLE", 1)))
                created.append(table)
                await conn.commit()

            class TemporaryEngine:
                @asynccontextmanager
                async def connect(self):
                    yield conn

                @asynccontextmanager
                async def begin(self):
                    # End any read-only autobegin before the next mutation.
                    await conn.commit()
                    async with conn.begin():
                        yield conn

            @asynccontextmanager
            async def unlocked():
                yield

            cache = AsyncMock()
            cache.sismember.return_value = False
            with (patch.object(s, "engine", TemporaryEngine()),
                  patch.object(s, "redis_client", cache),
                  patch.object(s, "_security_state_guard", unlocked),
                  patch.object(s, "_hydrate_ip_security_cache", new=AsyncMock()),
                  patch.object(s, "publish_edge_snapshot", new=AsyncMock())):
                for ip in ["13.11.1.1", "10.199.254.235", "2.255.255.255", "2001:db8::10", "2001:db8::2"]:
                    await s.manual_ban_ip(ip, "a" * 64, "temporary-table fixture")
                for _ in range(3):
                    await s.unban_ip("13.11.1.1", "a" * 64)
                    await s.manual_ban_ip("13.11.1.1", "a" * 64, "repeat")
                expected = ["2.255.255.255", "10.199.254.235", "13.11.1.1", "2001:db8::2", "2001:db8::10"]
                for order in ["asc", "desc"]:
                    found = []
                    for page in range(1, 4):
                        result = await s.list_security_summary(page=page, page_size=2, ip_order=order)
                        assert result["pagination"]["total"] == 5
                        found.extend(item["ip"] for item in result["events"])
                    assert found == (expected if order == "asc" else expected[::-1]), found
                await s.add_whitelist("13.11.1.1", "a" * 64)
                result = await s.list_security_summary(ip_filter="13.11.1.1")
                assert not result["events"] and len(result["whitelist"]) == 1 and result["pagination"]["total"] == 1
                assert (await s.list_security_summary(ip_filter="13.11.1.1", status_filter="active"))["pagination"]["total"] == 0
                await s.remove_whitelist("13.11.1.1", "a" * 64)
                result = await s.list_security_summary(ip_filter="13.11.1.1")
                assert result["events"][0]["status"] == "unbanned"
                assert result["events"][0]["ban_count"] == 4
                actions = (await conn.execute(text("SELECT action FROM ip_security_audit_log WHERE ip_address='13.11.1.1' ORDER BY created_at,id"))).scalars().all()
                assert actions[-2:] == ["whitelist_add", "whitelist_remove"]
                before = await conn.scalar(text("SELECT COUNT(*) FROM ip_security_audit_log"))
                with patch.object(s, "_audit_ip", side_effect=SQLAlchemyError("injected audit failure")):
                    try:
                        await s.unban_ip("10.199.254.235", "a" * 64)
                    except SQLAlchemyError:
                        pass
                    else:
                        raise AssertionError("Expected audit failure")
                assert (await s.list_security_summary(ip_filter="10.199.254.235"))["events"][0]["active"]
                assert await conn.scalar(text("SELECT COUNT(*) FROM ip_security_audit_log")) == before
                cache.eval.return_value = [6, int(time.time() * 1000)]
                await s.record_invalid_api("203.0.113.99", "GET", "/invalid", "fixture")
                assert (await s.list_security_summary(ip_filter="203.0.113.99"))["events"][0]["ban_kind"] == "auto"
                await s.unban_ip("203.0.113.99", "a" * 64)
                await s.record_invalid_api("203.0.113.99", "GET", "/invalid", "fixture")
                assert (await s.list_security_summary(ip_filter="203.0.113.99"))["events"][0]["ban_kind"] == "permanent"
                cache.eval.return_value = [1, int(time.time() * 1000)]
                await s.record_invalid_api("203.0.113.98", "GET", "/invalid", "fixture")
                assert (await s.list_security_summary(ip_filter="203.0.113.98"))["events"][0]["status"] == "observed"
            await conn.rollback()
        finally:
            await physical.rollback()
            for table in created:
                await physical.execute(text("DROP TABLE " + prefix + table))
                await physical.commit()
    await db.engine.dispose()
    print("mysql-security-smoke-ok: isolated fixture tables removed; numeric IPv4/IPv6, unique pagination, whitelist, audit rollback, automatic escalation")


if __name__ == "__main__":
    asyncio.run(main())
