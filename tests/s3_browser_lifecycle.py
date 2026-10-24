"""Real browser interruption and restart checks for disposable S3 fixtures."""
import json
import os
import shutil
import time
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect

from tests import federation_stack as fleet


def observe(node):
    # Read-only inspection of the live database. No deadlines or product state
    # are edited to shorten the real grant-expiry/recovery interval.
    return json.loads(node.web("""
import asyncio,json
from sqlalchemy import select
from app.services.federation.state import state
from app.services import s3_schema as s
async def read():
    await state.initialize()
    async with state.database.connect() as conn:
        columns = {
          'members':(s.members.c.member_id,s.members.c.used_bytes,s.members.c.reserved_bytes),
          'uploads':(s.uploads.c.session_id,s.uploads.c.state,s.uploads.c.media_path,s.uploads.c.expected_bytes,s.uploads.c.grant_until,s.uploads.c.expires_at),
          'objects':(s.objects.c.media_id,s.objects.c.state,s.objects.c.size_bytes,s.objects.c.grant_until),
          'parts':(s.parts.c.session_id,s.parts.c.part_number,s.parts.c.size_bytes,s.parts.c.etag),
        }
        return {name:[dict(row) for row in (await conn.execute(select(*values))).mappings()]
                for name,values in columns.items()}
print(json.dumps(asyncio.run(read())))
"""))


def run(browser,node,source,directory,report,check):
    started=time.monotonic()
    context,page=fleet.admin_page(browser,node)
    page.on('dialog',lambda dialog:dialog.accept())
    held=[]
    fault={'mode':'abort','attempts':0}
    def intercept(route):
        request=route.request
        query=parse_qs(urlsplit(request.url).query)
        if request.method=='PUT' and query.get('partNumber')==['2']:
            fault['attempts']+=1
            if fault['mode']=='hold':
                held.append(route)
                return
            route.abort('internetdisconnected')
        else:
            route.continue_()
    context.route('https://s3.4399.com:8333/**',intercept)
    heading=page.locator('[data-admin-module="media"] .module-heading')
    def nodes_panel():
        heading=page.locator('#nodesPanel .module-heading')
        if heading.get_attribute('aria-expanded')!='true':heading.click()
        page.locator('.s3-admin').wait_for(state='visible')
    def upload(name):
        folder=source/name;folder.mkdir()
        shutil.copyfile(source/'encrypted-album/original sample.wav',folder/'interrupted.wav')
        page.reload(wait_until='domcontentloaded')
        if heading.count() and heading.get_attribute('aria-expanded')!='true':heading.click()
        page.locator('.tree-row[data-path="music"]').dblclick()
        fleet.wait_page(page,"currentPath === 'music'")
        page.select_option('#uploadSiteType','s3')
        page.locator('#uploadBtn').click();page.locator('#uploadFolder').click()
        with page.expect_file_chooser() as choose:
            page.get_by_role('button',name='明文落盘',exact=True).click()
        choose.value.set_files(str(folder))
        report['actions'].append(dict(action='GUI interrupted upload',logical_parent='music/'+name,
            bytes=(folder/'interrupted.wav').stat().st_size,fault=fault['mode']+' second part only'))
    try:
        upload('failed-album')
        fleet.wait_page(page,"document.querySelector('#uploadSummary').textContent.startsWith('完成：')",timeout=60000)
        check('failed_browser_transfer_never_claims_publication','完成：成功 0，失败 1' in page.locator('#uploadSummary').inner_text())
        first=observe(node)
        failed=next(row for row in first['uploads'] if row['media_path']=='music/failed-album/interrupted.wav')
        check('failed_transfer_retains_real_confirmed_first_part',fault['attempts']==2 and failed['state']=='aborting'
            and sum(row['size_bytes'] for row in first['parts'] if row['session_id']==failed['session_id'] and row['etag'])==8*1024**2)
        check('live_part_grant_retains_quota',failed['grant_until']>time.time()
            and first['members'][0]['reserved_bytes']==failed['expected_bytes'])
        nodes_panel()
        expect(page.locator('.s3-pending').filter(has_text='待回收')).to_have_count(1)
        check('failure_panel_reflects_completed_cancel_without_reload',True)
        fault.update(mode='hold',attempts=0)
        upload('refreshed-album')
        deadline=time.monotonic()+60
        while not held and time.monotonic()<deadline:page.wait_for_timeout(100)
        check('browser_refresh_has_real_partial_upload',len(held)==1)
        page.reload(wait_until='domcontentloaded')
        # Navigation destroys the upload controller. An old request is never
        # allowed to complete behind the new page during the cancellation test.
        for route in held:
            try:route.abort('aborted')
            except Exception:pass
        context.unroute('https://s3.4399.com:8333/**',intercept)
        second=observe(node)
        refreshed=next(row for row in second['uploads'] if row['media_path']=='music/refreshed-album/interrupted.wav')
        check('refresh_preserves_upload_journal_and_reservation',refreshed['state']=='uploading'
            and second['members'][0]['reserved_bytes']==failed['expected_bytes']+refreshed['expected_bytes'])
        nodes_panel()
        row=page.locator('.s3-pending').filter(has_text='interrupted.wav').filter(has_text='上传中')
        row.get_by_role('button',name='取消',exact=True).click()
        expect(page.locator('.s3-pending').filter(has_text='待回收')).to_have_count(2,timeout=30000)
        report['actions'].append(dict(action='GUI cancel refreshed partial upload'))
        check('gui_cancel_reports_deferred_recovery',True)
        idle=None
        if os.environ.get('S3_BROWSER_IDLE')=='1':
            held.clear();fault.update(mode='hold',attempts=0)
            context.route('https://s3.4399.com:8333/**',intercept)
            upload('idle-album')
            deadline=time.monotonic()+60
            while not held and time.monotonic()<deadline:page.wait_for_timeout(100)
            check('idle_case_has_actual_partial_upload',len(held)==1)
            page.reload(wait_until='domcontentloaded')
            for route in held:
                try:route.abort('aborted')
                except Exception:pass
            context.unroute('https://s3.4399.com:8333/**',intercept)
            idle=next(row for row in observe(node)['uploads'] if row['media_path']=='music/idle-album/interrupted.wav')
            check('idle_upload_remains_owned_without_manual_cancel',idle['state']=='uploading' and idle['expires_at']>time.time()+1700)
            report['actions'].append(dict(action='GUI interrupted upload then refresh; no manual cancellation',
                logical_parent='music/idle-album',expires_at=idle['expires_at']))

        # Earlier playback grants may have expired during the real read-expiry
        # profile. Issue an ordinary browser HEAD immediately before GUI delete.
        deletion=[dict(kind='music',parent=name,names=['original sample.wav']) for name in ('plain-album','encrypted-album')]
        deletion.extend(report.get('playback_media',[]))
        published={row['path']:row for row in node.resources()}
        for item in deletion:
            page.reload(wait_until='domcontentloaded')
            if heading.count() and heading.get_attribute('aria-expanded')!='true':heading.click()
            page.locator(f'.tree-row[data-path="{item["kind"]}"]').dblclick()
            page.locator(f'.tree-row[data-path="{item["kind"]}/{item["parent"]}"]').dblclick()
            for name in item['names']:
                logical=item['kind']+'/'+item['parent']+'/'+name
                url=published[logical]['url']
                status=page.evaluate('async url => (await fetch(url,{method:"HEAD",cache:"no-store"})).status',url)
                check('fresh_browser_read_grant_before_delete_'+str(len(report.get('fresh_delete_read_inputs',[]))),status==200)
                report.setdefault('fresh_delete_read_inputs',[]).append(dict(method='HEAD',path=url,status=status))
                page.locator(f'.tree-row[data-path$="/{name}"]').click()
                page.locator('#delete').click();page.locator('#modalOk').click()
                expect(page.locator('#modal')).to_have_class('modal hidden',timeout=30000)
                report['actions'].append(dict(action='GUI delete',logical_parent=item['kind']+'/'+item['parent'],filename=name))
        deleting=observe(node)
        check('delete_waits_for_issued_read_grants',len(deleting['objects'])==sum(len(item['names']) for item in deletion)
            and all(row['state']=='deleting' and row['grant_until']>time.time() for row in deleting['objects'])
            and deleting['members'][0]['used_bytes']>0)
        check('unpublished_and_deleting_resources_hidden',not node.resources())

        # Restart the actual Web process with the same Compose mounts. Its
        # normal startup worker, rather than this test, performs reconciliation.
        node.compose('restart','web')
        fleet.wait_for(lambda:node.client.get(node.endpoint+'/health/ready').status_code==200,description='restart readiness')
        restarted=observe(node)
        check('restart_preserves_all_deferred_ownership',restarted['members']==deleting['members']
            and all(row['state']==('uploading' if row['media_path']=='music/idle-album/interrupted.wav' else 'aborting')
                for row in restarted['uploads'] if row['media_path'].endswith('interrupted.wav')))
        report['actions'].append(dict(action='test infrastructure Compose restart Web',state_mutation='none'))
        page.reload(wait_until='domcontentloaded')
        nodes_panel()
        member=page.locator('.s3-admin details').filter(has_text='Browser S3')
        member.locator('summary').click()
        member.get_by_role('button',name='移除',exact=True).click()
        expect(page.locator('.s3-admin > [role="status"]')).not_to_have_text('处理中',timeout=30000)
        check('gui_removal_cannot_orphan_owned_data',page.locator('.s3-admin details').count()==1
            and page.locator('.s3-admin > [role="status"]').inner_text()!='已完成')
        report['actions'].append(dict(action='GUI removal while ownership live',expected='refused'))

        until=max(row['grant_until'] for row in restarted['uploads']+restarted['objects'])
        if idle:until=max(until,idle['expires_at'])
        report['lifecycle']=dict(grants_not_before=until,wait_started_at=time.time(),clock_changed=False)
        deadline=time.monotonic()+max(390,until-time.time()+150)
        while time.monotonic()<deadline:
            pending=node.api('/api/v1/media/admin/s3/uploads')['uploads']
            members=node.api('/api/v1/media/admin/s3/members')['members']
            if not pending and members and members[0]['used_bytes']==members[0]['reserved_bytes']==0:break
            page.wait_for_timeout(5000)
        final=observe(node)
        check('normal_worker_settles_after_actual_grant_expiry',time.time()>=until and not pending
            and final['members'][0]['used_bytes']==final['members'][0]['reserved_bytes']==0
            and all(row['state']=='deleted' for row in final['objects'])
            and all(row['state']=='aborted' for row in final['uploads'] if row['media_path'].endswith('interrupted.wav')))
        if idle:
            check('actual_thirty_minute_idle_worker_cleanup_without_cancel',time.time()>=idle['expires_at']
                and next(row for row in final['uploads'] if row['session_id']==idle['session_id'])['state']=='aborted')
        report['lifecycle'].update(settled_at=time.time(),seconds=round(time.monotonic()-started,3),final=final)
        page.reload(wait_until='domcontentloaded')
        nodes_panel()
        expect(page.locator('.s3-pending')).to_have_count(0)
        member=page.locator('.s3-admin details').filter(has_text='Browser S3')
        member.locator('summary').click();member.get_by_role('button',name='停用',exact=True).click()
        expect(page.locator('.s3-admin > [role="status"]')).to_have_text('已完成')
        member.locator('summary').click();member.get_by_role('button',name='移除',exact=True).click()
        expect(page.locator('.s3-admin details')).to_have_count(0,timeout=30000)
        check('gui_disable_and_remove_after_settlement',True)
        report['actions'].append(dict(action='GUI disable and remove after zero owned/reserved bytes'))
    finally:
        context.close()
