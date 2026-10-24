"""Chromium UI contract with explicit fixture responses, not backend/TLS proof."""
import json
import os
from pathlib import Path
import re

from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
SOURCE='a'*32
TARGET='b'*32
TASK='c'*32
UNIT='d'*64
NEXT='e'*64


def run(output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True);inputs=[];results=[]
    html=(ROOT/'static/media/admin.html').read_text()
    section=re.search(r'<section id="migrationsPanel".*?</section>',html,re.S).group()
    styles='\n'.join((ROOT/'static/css'/name).read_text() for name in ('admin.css','admin-system-modules.css','nodes-observability.css'))
    code=(ROOT/'static/js/migrations.js').read_text()
    document='<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>'+styles+'</style></head><body>'
    document+='<main class="admin-console">'+section+'</main><script>'+'''
    function requestHeaders(){return {'Content-Type':'application/json','X-CSRF-Token':'fixture-csrf'};}
    function activityRequestOptions(value){return value;}
    document.querySelector('.module-heading').onclick=()=>document.getElementById('migrationsPanel').classList.toggle('expanded');
    '''+code+'</script></body></html>'
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'),headless=True,args=['--no-sandbox'])
        try:
            for width,height in ((1366,900),(390,844)):
                context=browser.new_context(viewport=dict(width=width,height=height));page=context.new_page();errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                row=dict(task_id=TASK,source_id=SOURCE,target_id=TARGET,state='copying',revision=1,object_count=2,total_bytes=1000,
                    committed_bytes=0,error_code='',pause_reason='',resume_state='',created_at=123,updated_at=123,
                    current_object=dict(logical_id='f'*32,physical_path='music/<img src=x onerror=alert(1)>/long.mp3',
                        expected_bytes=1000,received_bytes=400,hashed_bytes=200),
                    units=[dict(unit_id=UNIT,parent_path='music/'+'长目录'*100,state='copying',total_bytes=1000)],next_cursor=NEXT)
                submitted=False;observed=False
                def route(request_route):
                    nonlocal submitted,observed
                    request=request_route.request
                    if request.url=='http://127.0.0.1:18825/':return request_route.fulfill(body=document,content_type='text/html')
                    if request.url.endswith('/favicon.ico'):return request_route.fulfill(status=404)
                    path=request.url.split('/migrations',1)[-1];body=request.post_data_json if request.post_data else None
                    inputs.append(dict(viewport=width,method=request.method,path=path,value=body,
                        csrf=request.headers.get('x-csrf-token','')))
                    if path=='/members':
                        value=dict(items=[dict(member_id=member,peer_endpoint='https://'+label+'.4399.com',transport=mode,
                            status='online',storage_enabled=1) for member,label,mode in ((SOURCE,'source','Direct'),(TARGET,'target','Relay'))],complete=True)
                    elif path=='/tasks':value=dict(items=[row] if submitted else [],next_cursor='')
                    elif path=='/plans' or path.startswith('/plans/') and not path.endswith('/start'):
                        if request.method=='POST':
                            assert (body['source_id'],body['target_id'])==(SOURCE,TARGET)
                            assert re.fullmatch('[a-f0-9]{32}',body['request_id'])
                        value=dict(plan_id=TASK,state='ready' if observed else 'observing',review_sha256='9'*64,object_count=2,
                            unit_count=1,total_bytes=1000,report=dict(source_id=SOURCE,target_id=TARGET,blocker_count=0))
                        observed=True
                    elif path.endswith('/start'):
                        assert body==dict(approved_sha256='9'*64,confirm_source_id=SOURCE,confirm_target_id=TARGET)
                        assert request.headers.get('x-csrf-token')=='fixture-csrf';submitted=True;value=row
                    elif path.startswith('/tasks/'+TASK):
                        if request.method=='POST':
                            assert body['revision']==row['revision'];row['revision']+=1
                            if body['action']=='pause':row.update(state='paused',pause_reason='manual')
                            elif body['action']=='resume':row.update(state='copying',pause_reason='')
                            elif body['action']=='cancel':row.update(state='cancelling',pause_reason='')
                            else:raise AssertionError('Unexpected control')
                        value={**row}
                        if '?after='+NEXT in path:value.update(units=[dict(unit_id=NEXT,parent_path='music/second-page',state='pending',total_bytes=500)],next_cursor='')
                    else:raise AssertionError('Unexpected UI input '+path)
                    request_route.fulfill(json=value)
                page.route('http://127.0.0.1:18825/**',route)
                page.on('dialog',lambda dialog:dialog.accept())
                page.goto('http://127.0.0.1:18825/')
                page.locator('.module-heading').click();page.locator('#migrationSource option[value="'+SOURCE+'"]').wait_for(state='attached')
                page.select_option('#migrationSource',SOURCE);page.select_option('#migrationTarget',TARGET)
                page.get_by_role('button',name='只读计划',exact=True).click()
                page.get_by_role('button',name='执行迁移',exact=True).wait_for(timeout=6000)
                page.get_by_role('button',name='执行迁移',exact=True).click()
                page.get_by_role('button',name='暂停',exact=True).wait_for()
                assert '40.0%' in page.locator('#migrationTask').inner_text()
                assert page.locator('#migrationTask img').count()==0
                page.get_by_role('button',name='暂停',exact=True).click();page.get_by_role('button',name='恢复',exact=True).wait_for()
                assert '手动暂停' in page.locator('#migrationTask').inner_text()
                page.get_by_role('button',name='恢复',exact=True).click();page.get_by_role('button',name='暂停',exact=True).wait_for()
                page.get_by_role('button',name='更多目录',exact=True).click()
                page.get_by_text('music/second-page',exact=False).wait_for();page.wait_for_timeout(3200)
                assert 'music/second-page' in page.locator('#migrationTask').inner_text()
                assert page.locator('.migration-card progress').get_attribute('aria-label')=='目录切换进度'
                dimensions=page.evaluate('({width:innerWidth,body:document.documentElement.scrollWidth})')
                assert dimensions['body']<=dimensions['width'],dimensions
                page.screenshot(path=str(output/f'migrations-{width}.png'),full_page=True)
                page.get_by_role('button',name='取消',exact=True).click()
                page.wait_for_function("document.getElementById('migrationTask').textContent.includes('取消中')")
                page.locator('.module-heading').click();page.wait_for_timeout(100);before=len(inputs)
                page.evaluate("document.dispatchEvent(new Event('visibilitychange'))");page.wait_for_timeout(3300)
                assert len(inputs)==before,'Collapsed module continued polling'
                assert not errors,errors
                results.append(dict(viewport=[width,height],native_dom=True,fixture_api=True,plan_approval=True,
                    file_progress=True,manual_pause_resume=True,cancel=True,paged_units_survive_poll=True,
                    paths_are_text=True,no_horizontal_overflow=True,collapsed_polling_stops=True))
                context.close()
        finally:browser.close()
    value=dict(format='frontiercloud-python-migration-browser-contract',version=1,actual_backend=False,tls_verified=False,
        cases=results,actual_browser_inputs=inputs)
    (output/'result.json').write_text(json.dumps(value,sort_keys=True));print(json.dumps(dict(cases=len(results),inputs=len(inputs))))


if __name__=='__main__':run(os.environ['FC_MIGRATION_UI_EVIDENCE'])
