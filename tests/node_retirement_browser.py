"""Actual trusted Chromium/Admin inputs against disposable Compose peers."""
import json
from pathlib import Path

from playwright.sync_api import expect


def run(page,master,peer,root,report,check):
    identity=peer.nodes()['node_id'];inputs=[];responses=[];errors=[]
    def record(request):
        if '/admin/nodes/' in request.url and ('/retirement' in request.url or request.url.endswith('/revoke')):
            inputs.append(dict(method=request.method,path=request.url.split(master.endpoint,1)[-1],
                body=request.post_data or '',csrf_header_present=bool(request.headers.get('x-csrf-token'))))
    page.on('request',record)
    def record_response(response):
        if '/admin/nodes/' not in response.url or '/retirement' not in response.url:return
        row=dict(path=response.url.split(master.endpoint,1)[-1],status=response.status)
        try:row['body']=response.json()
        except Exception as exc:row['body_unavailable']=type(exc).__name__
        responses.append(row)
    page.on('response',record_response)
    page.on('pageerror',lambda error:errors.append(type(error).__name__))
    report['retirement_gui_inputs']=inputs;report['retirement_gui_responses']=responses
    report['retirement_gui_actions']=[]
    def action(name):report['retirement_gui_actions'].append(name)
    legacy=page.evaluate("""async path => {
        const response = await fetch(path, {method: 'POST', headers: requestHeaders(), body: '{}'});
        return {status: response.status, body: await response.json()};
    }""",'/api/v1/media/admin/nodes/'+peer.relation+'/revoke')
    action('验证旧撤销入口拒绝活动节点')
    check('real_admin_legacy_route_cannot_bypass_retirement',legacy['status']==409 and legacy['body'].get('detail')=='retirement_required')
    card=page.locator('#nodeRelationships .node-card').filter(has_text=identity)
    expect(card).to_have_count(1);card.get_by_role('button',name='展开',exact=True).click();action('展开所选 Direct')
    check('active_node_has_safe_retirement_only',card.get_by_role('button',name='撤销关系',exact=True).count()==0)
    card.get_by_role('button',name='安全退役',exact=True).click();action('安全退役')
    dialog=page.get_by_role('dialog').filter(has_text='安全退役')
    expect(dialog.get_by_role('status')).to_have_text('先预检，再确认')
    confirmation=dialog.get_by_role('textbox',name='确认退役节点 ID')
    apply=dialog.get_by_role('button',name='确认退役',exact=True)
    expect(apply).to_be_disabled();confirmation.fill('f'*32);expect(apply).to_be_disabled()
    check('retirement_requires_check_and_exact_identity',True)
    # The only file created/removed by this fixture is its own known test file.
    program="from pathlib import Path; from app.api.v1.media import MEDIA_ROOT; p=MEDIA_ROOT/'music/retirement-browser.bin'; p.write_bytes(b'owned fixture unknown ciphertext'); print(p.stat().st_ino)"
    inode=peer.web(program)
    dialog.get_by_role('button',name='预检',exact=True).click();action('预检：未知文件阻断')
    expect(dialog.get_by_role('status')).to_contain_text('存在未核实文件',timeout=30000)
    confirmation.fill(identity);expect(apply).to_be_disabled()
    peer.web("from app.api.v1.media import MEDIA_ROOT; p=MEDIA_ROOT/'music/retirement-browser.bin'; assert p.read_bytes()==b'owned fixture unknown ciphertext'; assert p.stat().st_ino=="+inode+"; p.unlink()")
    check('gui_preflight_preserves_unknown_file_and_refuses_apply',True)
    dialog.get_by_role('button',name='预检',exact=True).click();action('预检：两端通过')
    expect(dialog.get_by_role('status')).to_have_text('Master · 通过存储节点 · 通过',timeout=30000)
    expect(apply).to_be_enabled()
    # CSRF enforcement is real product middleware, not a dependency override.
    response=page.evaluate("""async value => {
        const response = await fetch(value.path, {method: 'POST', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json'}, body: JSON.stringify(value.body)});
        return {status: response.status};
    }""",dict(path='/api/v1/media/admin/nodes/'+peer.relation+'/retirement',body=dict(action='apply',confirm_node_id=identity)))
    check('retirement_gui_csrf_is_required',response['status']==403)
    dialog.screenshot(path=str(Path(root)/'retirement-preflight.png'))
    apply.click();action('确认退役')
    expect(dialog.get_by_role('status')).to_have_text('已退役',timeout=45000)
    expect(confirmation).to_be_hidden()
    with page.expect_download() as transfer:
        dialog.get_by_role('button',name='下载凭据',exact=True).click();action('下载凭据')
    artifact=transfer.value;target=Path(root)/'retirement-receipt.json';artifact.save_as(target)
    receipt=json.loads(target.read_text());check('gui_downloads_real_signed_terminal_receipt',receipt['payload']['side']=='master'
        and receipt['payload']['member_id']==identity and receipt['payload']['peer_receipt']['payload']['member_id']==identity)
    dialog.get_by_role('button',name='关闭',exact=True).click()
    page.locator('#nodeRetirementHistory').click();action('退役记录')
    history=page.get_by_role('dialog').filter(has_text='退役记录');expect(history).to_contain_text('已退役')
    history.get_by_role('button',name='查看',exact=True).click();action('查看已完成记录')
    completed=page.get_by_role('dialog').filter(has_text='安全退役');expect(completed.get_by_role('status')).to_have_text('已退役')
    expect(completed.get_by_role('button',name='下载凭据',exact=True)).to_be_enabled()
    completed.screenshot(path=str(Path(root)/'retirement-completed.png'))
    completed.get_by_role('button',name='关闭',exact=True).click();history.get_by_role('button',name='关闭',exact=True).click()
    check('completed_retirement_remains_in_gui_history',True)
    current=peer.nodes();check('retirement_keeps_peer_identity_and_runtime',current['node_id']==identity and current['role']=='Follower')
    peer.web("""import asyncio
from sqlalchemy import select,func
from app.core.db import engine
from app.services.node_retirement_schema import retirements
async def read():
    async with engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(retirements).where(retirements.c.phase=='completed'))==1
asyncio.run(read())
""")
    check('retirement_browser_has_no_page_errors',not errors)
    check('retirement_gui_records_actual_inputs',any(row['method']=='POST' and json.loads(row['body']).get('action')=='apply'
        and row['csrf_header_present'] for row in inputs))
