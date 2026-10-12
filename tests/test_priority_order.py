"""Real SQLite priority transactions, full siblings and audit failure rollback."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import text

from app.services import priority_order, priority_planner
from app.services.federation import schema


class PriorityTransactions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.core.sqlite_compat import create_sqlite_engine
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / 'music/category').mkdir(parents=True)
        self.engine = create_sqlite_engine('sqlite+aiosqlite:///' + str(self.root / 'test.db'))
        async with self.engine.begin() as conn:
            await conn.run_sync(schema.metadata.create_all)
            await conn.execute(text('''INSERT INTO node_identity
                (singleton,node_id,role,endpoint,private_key,created_at)
                VALUES (1,'node','Standalone','','sealed',1)'''))
            await conn.execute(text('''CREATE TABLE media_objects (
                media_id TEXT PRIMARY KEY,object_kind TEXT,media_path TEXT,path_locator TEXT UNIQUE,
                created_at TIMESTAMP,updated_at TIMESTAMP)'''))
            await conn.execute(text('''CREATE TABLE media_playback_stats (
                media_id TEXT PRIMARY KEY,media_path TEXT,play_score INTEGER,preference INTEGER
                CHECK(preference BETWEEN -7 AND 500000),created_at TIMESTAMP,updated_at TIMESTAMP)'''))
            await conn.execute(text('CREATE TABLE media_playback_events (media_id TEXT)'))
            await conn.execute(text('CREATE TABLE media_lyric_links (media_id TEXT,lyric_id TEXT)'))
        self.cache = patch('app.services.priority_order.invalidate_media_catalog', new=AsyncMock())
        self.cache.start()

    async def asyncTearDown(self):
        self.cache.stop()
        await self.engine.dispose()
        self.directory.cleanup()

    async def seed(self, priorities):
        import hashlib
        for index, (name, value) in enumerate(priorities):
            path = 'music/category/' + name
            (self.root / path).write_bytes(b'fixture')
            async with self.engine.begin() as conn:
                await conn.execute(text('''INSERT INTO media_objects VALUES
                    (:id,'audio',:path,:locator,'2026-01-01','2026-01-01')'''),
                    {'id': str(index), 'path': path, 'locator': hashlib.sha256(path.encode()).hexdigest()})
                await conn.execute(text('''INSERT INTO media_playback_stats VALUES
                    (:id,:path,10,:value,'2026-01-01','2026-01-01')'''),
                    {'id': str(index), 'path': path, 'value': value})

    async def test_collision_move_and_stale_revision(self):
        await self.seed([('a.mp3', 100), ('b.mp3', 100), ('c.mp3', 101), ('hidden.mp3', 99)])
        old = await priority_order.order('music/category', 'media', database=self.engine, root=self.root)
        events = []
        async def audit(conn, result, count, detail):
            events.append(detail)
            self.assertTrue(conn.in_transaction())
        new = await priority_order.reorder('music/category', 'media', old['revision'], 'music/category/c.mp3',
            'music/category/b.mp3', 'music/category/a.mp3', audit, database=self.engine, root=self.root)
        self.assertEqual([v['path'].rsplit('/', 1)[-1] for v in new['items']], ['a.mp3','c.mp3','b.mp3','hidden.mp3'])
        self.assertEqual(new['items'][-1]['preference'], 99)
        self.assertTrue(events)
        with self.assertRaises(priority_order.StaleOrder):
            await priority_order.reorder('music/category', 'media', old['revision'], 'music/category/c.mp3',
                'music/category/b.mp3', 'music/category/a.mp3', audit, database=self.engine, root=self.root)

    async def test_audit_error_rolls_back_every_changed_priority(self):
        await self.seed([('a.mp3', 100), ('b.mp3', 100), ('c.mp3', 101)])
        old = await priority_order.order('music/category', 'media', database=self.engine, root=self.root)
        async def audit(*args):
            raise RuntimeError('audit unavailable')
        with self.assertRaises(RuntimeError):
            await priority_order.reorder('music/category', 'media', old['revision'], 'music/category/c.mp3',
                'music/category/b.mp3', 'music/category/a.mp3', audit, database=self.engine, root=self.root)
        again = await priority_order.order('music/category', 'media', database=self.engine, root=self.root)
        self.assertEqual(again, old)

    async def test_boundary_data_and_unfiltered_revision(self):
        await self.seed([('a.mp3', 500000), ('b.mp3', -7), ('off-page.mp3', 0)])
        old = await priority_order.order('music/category', 'media', database=self.engine, root=self.root)
        self.assertEqual(len(old['items']), 3)
        (self.root / 'music/category/new.mp3').write_bytes(b'new sibling')
        with self.assertRaises(priority_order.StaleOrder):
            await priority_order.reorder('music/category', 'media', old['revision'], 'music/category/b.mp3',
                'music/category/a.mp3', '', AsyncMock(), database=self.engine, root=self.root)

    async def test_role_and_escaping_paths_fail_closed(self):
        with self.assertRaises(priority_planner.PriorityMoveError):
            await priority_order.order('music/../outside', 'media', database=self.engine, root=self.root)
        async with self.engine.begin() as conn:
            await conn.execute(text("UPDATE node_identity SET role='Follower'"))
        with self.assertRaises(priority_planner.PriorityMoveError):
            await priority_order.order('music', 'directory', database=self.engine, root=self.root)
