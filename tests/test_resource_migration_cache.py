"""Actual SQL cutover counters select cache namespaces without Redis notification."""
import os
import unittest
from unittest.mock import patch

from sqlalchemy.exc import SQLAlchemyError

from app.services import media_catalog_cache as cache
from app.services.federation import state as state_module
from app.services.federation.catalog import Catalog
from tests import test_resource_cutover as fixtures


class Redis:
    def __init__(self):self.values={}
    async def get(self,key):return self.values.get(key)
    async def set(self,key,value,**kwargs):self.values[key]=value
    async def incr(self,key):self.values[key]=int(self.values.get(key,0))+1;return self.values[key]


@unittest.skipUnless(os.name=='posix','cache fixture requires native publication')
class MigrationCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.CutoverTests('runTest');await self.fixture.asyncSetUp();await self.fixture.prepare()
        self.f=self.fixture.f;self.redis=Redis()
        self.patches=[patch.object(cache,'redis_client',self.redis),patch.object(state_module,'state',self.f.master.store)]
        for item in self.patches:item.start()
        self.catalog=Catalog(self.f.master.store);self.logical=self.fixture.resources[0][0]

    async def asyncTearDown(self):
        for item in reversed(self.patches):item.stop()
        await self.fixture.asyncTearDown()

    async def warm(self):
        generation,value=await cache.load_media_catalog('migration-test','album');self.assertIsNone(value)
        row=await self.catalog.resource(self.logical)
        await cache.store_media_catalog(generation,'migration-test','album',[row])
        self.assertEqual((await cache.load_media_catalog('migration-test','album'))[1],[row])
        return generation,row

    async def test_committed_ownership_changes_namespace_without_redis_generation_write(self):
        before,row=await self.warm();self.assertEqual(row['owner_id'],self.f.source.store.node['node_id'])
        self.fixture.lease,_=await self.fixture.service.commit(self.fixture.lease,self.fixture.unit_id)
        after,value=await cache.load_media_catalog('migration-test','album')
        self.assertNotEqual(after,before);self.assertIsNone(value);self.assertNotIn(cache.GENERATION_KEY,self.redis.values)
        current=await self.catalog.resource(self.logical)
        self.assertEqual(current['owner_id'],self.f.target.store.node['node_id'])
        await cache.store_media_catalog(after,'migration-test','album',[current])
        self.assertEqual((await cache.load_media_catalog('migration-test','album'))[1],[current])

    async def test_failed_owner_transaction_keeps_warm_source_namespace(self):
        before,row=await self.warm();log=self.f.master.store.log
        async def fail(conn,action,actor,**kwargs):
            if action=='migration_parent_committed':raise OSError('injected cutover audit failure')
            return await log(conn,action,actor,**kwargs)
        with patch.object(self.f.master.store,'log',side_effect=fail):
            with self.assertRaises(OSError):await self.fixture.service.commit(self.fixture.lease,self.fixture.unit_id)
        self.assertEqual(await cache.load_media_catalog('migration-test','album'),(before,[row]))

    async def test_native_acknowledgement_progress_does_not_discard_warm_target_catalog(self):
        self.fixture.lease,_=await self.fixture.service.commit(self.fixture.lease,self.fixture.unit_id)
        generation,row=await self.warm();await self.fixture.acknowledged()
        self.assertEqual(await cache.load_media_catalog('migration-test','album'),(generation,[row]))

    async def test_unavailable_sql_counter_does_not_return_cached_old_ownership(self):
        await self.warm()
        with patch.object(cache,'_migration_generation',side_effect=SQLAlchemyError('fixture SQL unavailable')):
            self.assertEqual(await cache.load_media_catalog('migration-test','album'),(None,None))
