"""Admin route inputs drive the actual two-store retirement lifecycle."""
from unittest.mock import patch

from fastapi import FastAPI,HTTPException
import httpx
from sqlalchemy import select,update

from app.api.v1 import admin_nodes
from app.api.v1.admin import require_session
from app.services import node_retirement_schema as s
from app.services.federation import runtime as runtime_module
from tests.test_node_retirement_api import RetirementAPIFixture


class RetirementAdminTests(RetirementAPIFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.admin_patches=[patch.object(admin_nodes,'state',self.f.master),patch.object(admin_nodes,'transport',self.transport),
            patch.object(runtime_module,'state',self.f.master)]
        for item in self.admin_patches:item.start()
        self.admin_app=FastAPI();self.admin_app.include_router(admin_nodes.router,prefix='/api/v1/media/admin')
        self.admin_app.dependency_overrides[require_session]=lambda:'test-admin'
        self.admin_http=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.admin_app),base_url='https://master.4399.com')
        self.path='/api/v1/media/admin/nodes/'+self.f.relationship+'/retirement'

    async def asyncTearDown(self):
        await self.admin_http.aclose()
        for item in reversed(self.admin_patches):item.stop()
        await super().asyncTearDown()

    async def apply(self,member=None):
        return await self.admin_http.post(self.path,json=dict(action='apply',confirm_node_id=member or self.f.peer.node['node_id']))

    async def test_admin_check_apply_status_receipt_history_and_completed_retry(self):
        status=await self.admin_http.get(self.path);self.assertEqual(status.status_code,200)
        self.assertEqual(status.json()['phase'],'not_started');self.assertEqual(status.headers['cache-control'],'no-store')
        checked=await self.admin_http.post(self.path,json=dict(action='check'));self.assertEqual(checked.status_code,200);self.assertTrue(checked.json()['ready'])
        result=await self.apply();self.assertEqual(result.status_code,200,result.text)
        operation=result.json()['operation'];self.assertEqual(operation['phase'],'completed');self.assertTrue(operation['receipt_sha256'])
        self.assertNotIn('credential',str(operation));self.assertNotIn('private_key',str(operation));self.assertNotIn(str(self.f.file.base),str(operation))
        self.assertEqual((await self.admin_http.get(self.path)).json(),operation)
        history=(await self.admin_http.get('/api/v1/media/admin/nodes/retirements')).json()
        self.assertEqual(history,dict(items=[operation],next_cursor=''))
        with patch.object(self.transport,'request',side_effect=AssertionError('completed retry contacted peer')):
            replay=await self.apply();self.assertEqual(replay.json()['operation'],operation)
        await self.f.phase(self.f.peer,'completed');await self.f.phase(self.f.master,'completed')

    async def test_wrong_or_missing_confirmation_cannot_start_preparation(self):
        for body in (dict(action='apply'),dict(action='apply',confirm_node_id='f'*32)):
            response=await self.admin_http.post(self.path,json=body);self.assertEqual(response.status_code,409)
            self.assertEqual(response.json()['code'],'retirement_confirmation_mismatch')
        for store in (self.f.master,self.f.peer):
            async with store.database.connect() as conn:self.assertIsNone(await conn.scalar(select(s.retirements.c.retirement_id)))

    async def test_unknown_file_blocks_admin_apply_with_both_observations_and_no_fence(self):
        path=self.f.file.paths['media']/'unknown.bin';path.write_bytes(b'preserve all bytes')
        response=await self.apply();self.assertEqual(response.status_code,409)
        value=response.json();self.assertEqual(value['code'],'retirement_preflight_blocked');self.assertFalse(value['report']['ready'])
        self.assertEqual(value['report']['peer']['code'],'retirement_unknown_file')
        self.assertEqual((await self.admin_http.get(self.path)).json()['phase'],'not_started')
        self.assertEqual(path.read_bytes(),b'preserve all bytes')

    async def test_lost_complete_reply_retains_operation_and_admin_retry_finishes_it(self):
        original=self.transport.request;dropped=False
        async def lose(endpoint,path,**kwargs):
            nonlocal dropped
            result=await original(endpoint,path,**kwargs)
            if kwargs.get('value',{}).get('action')=='complete' and not dropped:
                dropped=True;raise TimeoutError('private endpoint must not leak')
            return result
        with patch.object(self.transport,'request',side_effect=lose):
            response=await self.apply();self.assertEqual(response.status_code,409);self.assertEqual(response.json()['code'],'retirement_peer_unavailable')
        self.assertNotIn('private endpoint',response.text)
        pending=(await self.admin_http.get(self.path)).json();self.assertEqual(pending['phase'],'retiring')
        await self.f.phase(self.f.peer,'completed')
        retry=await self.apply();self.assertEqual(retry.status_code,200,retry.text)
        self.assertEqual(retry.json()['operation']['retirement_id'],pending['retirement_id'])
        await self.f.phase(self.f.master,'completed')

    async def test_legacy_admin_revoke_cannot_bypass_retirement_for_empty_active_node(self):
        response=await self.admin_http.post(self.path.removesuffix('/retirement')+'/revoke',json={})
        self.assertEqual(response.status_code,409);self.assertEqual(response.json()['detail'],'retirement_required')
        self.assertEqual((await self.f.master.relationship(self.f.relationship))['state'],'active')

    async def test_routes_require_admin_and_mutations_require_https_and_exact_fields(self):
        async def denied():raise HTTPException(401,'Admin session required')
        self.admin_app.dependency_overrides[require_session]=denied
        for method,path,value in [('GET',self.path,None),('GET','/api/v1/media/admin/nodes/retirements',None),('POST',self.path,dict(action='check'))]:
            response=await self.admin_http.request(method,path,json=value);self.assertEqual(response.status_code,401)
        self.admin_app.dependency_overrides[require_session]=lambda:'test-admin'
        response=await self.admin_http.post('http://master.4399.com'+self.path,json=dict(action='check'));self.assertEqual(response.status_code,403)
        for value in [dict(action='erase'),dict(action='apply',confirm_node_id=123),dict(action='check',force=True)]:
            response=await self.admin_http.post(self.path,json=value);self.assertEqual(response.status_code,422)

    async def test_follower_cannot_use_master_admin_and_invalid_history_cursor_refuses(self):
        with patch.object(admin_nodes,'state',self.f.peer):
            response=await self.admin_http.get(self.path);self.assertEqual(response.status_code,409);self.assertEqual(response.json()['code'],'retirement_role_invalid')
        response=await self.admin_http.get('/api/v1/media/admin/nodes/retirements?after=not-an-id')
        self.assertEqual(response.status_code,409);self.assertEqual(response.json()['code'],'retirement_cursor_invalid')

    async def test_history_and_status_do_not_accept_tampered_completed_receipt(self):
        response=await self.apply();self.assertEqual(response.status_code,200)
        async with self.f.master.database.begin() as conn:
            await conn.execute(update(s.retirements).values(receipt_json='{}'))
        for path in (self.path,'/api/v1/media/admin/nodes/retirements'):
            response=await self.admin_http.get(path);self.assertEqual(response.status_code,409);self.assertEqual(response.json()['code'],'retirement_journal_invalid')
