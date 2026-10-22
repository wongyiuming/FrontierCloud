"""Real Admin GUI, Compose, trusted HTTPS and native migration to cleanup grace."""
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import socket
import signal
import time
import traceback
import wave

from cryptography.hazmat.primitives import serialization
from playwright.sync_api import expect,sync_playwright

from tests import federation_stack as fleet
from tests.certificate_fixtures import TestCA

ROOT=Path(__file__).resolve().parents[1]


def snapshot(node):
    return json.loads(node.web('''import asyncio,hashlib,json
from sqlalchemy import select
from app.core.db import engine
from app.services.federation import schema as fs
from app.services import media_crypto_schema as cs
async def read():
    async with engine.connect() as conn:
        rows=[dict(r) for r in (await conn.execute(select(fs.global_media).order_by(fs.global_media.c.media_id))).mappings()]
        owners={r['media_id']:r.pop('storage_member_id') for r in rows}
        encrypted=[dict(r) for r in (await conn.execute(select(cs.encryption).order_by(cs.encryption.c.file_id))).mappings()]
        digest=lambda value:hashlib.sha256(json.dumps(value,sort_keys=True,default=str).encode()).hexdigest()
        return dict(owners=owners,catalog_sha256=digest(rows),encryption_sha256=digest(encrypted))
print(json.dumps(asyncio.run(read())))'''))


def main():
    root=Path(os.environ['FC_MIGRATION_BROWSER_ROOT']).resolve()
    if root.exists() or not root.name.startswith('migration-browser-fixture-'):raise RuntimeError('Fixture root must be new')
    root.mkdir(mode=0o700,parents=True);tag=hashlib.sha256(str(root).encode()).hexdigest()[:12]
    command=fleet.command;network='fc-python-migration-ui-'+tag;nodes=[];created=False;reader=None
    terminal=os.environ.get('FC_MIGRATION_GUI_TERMINAL')=='1'
    report=dict(format='frontiercloud-python-migration-browser-native',version=1,checks=[],actual_backend=True,
        tls_errors_ignored=False,host_ports=[],production_changed=False,whole_task_terminal=False,actual_hour=False,passed=False)
    def check(name,value):
        if not value:raise AssertionError(name)
        report['checks'].append(name);print(json.dumps(dict(passed=name)),flush=True)
    ca=TestCA();key=root/'test-ca';key.mkdir(mode=0o700)
    public=key/'root.crt';public.write_bytes(ca.root.public_bytes(serialization.Encoding.PEM))
    private=key/'private.pem';private.write_bytes(ca.key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));private.chmod(0o600)
    bundle=root/'roots.pem';bundle.write_bytes(Path('/etc/ssl/certs/ca-certificates.crt').read_bytes()+public.read_bytes())
    images={}
    for kind in ('web','nginx'):
        clean=f'fc-python-migration-ui:{tag}-clean-{kind}'
        command('docker','build','-t',clean,'-f',str(ROOT/('Dockerfile' if kind=='web' else 'nginx/Dockerfile')),str(ROOT))
        build=key/('Dockerfile.'+kind);build.write_text(f'FROM {clean}\nUSER 0\nCOPY root.crt /usr/local/share/ca-certificates/\nRUN update-ca-certificates\n'+
            ('USER 10001:10001\n' if kind=='web' else ''))
        images[kind]=f'fc-python-migration-ui:{tag}-trusted-{kind}';command('docker','build','-t',images[kind],'-f',str(build),str(key))
    try:
        ids=command('docker','network','ls','-q').splitlines()
        all_networks=json.loads(command('docker','network','inspect',*ids)) if ids else []
        occupied=[ipaddress.ip_network(r['Subnet']) for item in all_networks
            for r in ((item.get('IPAM') or {}).get('Config') or []) if r.get('Subnet')]
        subnet=next((value for index in range(250,100,-1) for value in [ipaddress.ip_network(f'10.243.{index}.0/24')]
            if not any(value.overlaps(other) for other in occupied if other.version==4)),None)
        if subnet is None:raise RuntimeError('No isolated public migration subnet')
        command('docker','network','create','--internal','--subnet',str(subnet),network);created=True
        info=json.loads(command('docker','network','inspect',network))[0]
        subnet=next(ipaddress.ip_network(r['Subnet']) for r in info['IPAM']['Config'] if ':' not in r['Subnet'])
        occupied.append(subnet)
        choices=(ipaddress.ip_network(f'10.252.{v}.0/24') for v in range(240,200,-1))
        private_networks=[c for c in choices if not any(c.overlaps(o) for o in occupied if o.version==4)][:3]
        if len(private_networks)!=3:raise RuntimeError('No isolated fixture subnets')
        base=json.loads(command('docker','compose','-f',str(ROOT/'docker-compose.yaml'),'config','--format','json'))
        for index,name in enumerate(('migration-admin','migration-source','migration-target')):
            node=fleet.Node(name,root,base,public,bundle,index,network=network);nodes.append(node)
            node.host=str(subnet.network_address+11+index);fleet.DOMAIN_ADDRESSES[node.hostname]=node.host
            configuration=json.loads(node.configuration.read_text())
            configuration['networks']['default']['ipam']={'config':[{'subnet':str(private_networks[index])}]}
            for service_name,service in configuration['services'].items():
                if service_name in ('web','secrets-init','media-init','certificate-init','certificate-agent'):service['image']=images['web']
                if service_name=='nginx':service['image']=images['nginx'];service['networks']['fleet']['ipv4_address']=node.host
                if service_name=='web':
                    service['mem_limit']='512m'
                    if node.storage:service['networks']['fleet']['ipv4_address']=node.host
                    else:service.setdefault('environment',{})['TRUSTED_PROXY_NETWORKS']=str(private_networks[index])
            node.configuration.write_text(json.dumps(configuration))
        socket.getaddrinfo=fleet.fixture_getaddrinfo
        for node in nodes:node.start()
        master,source,target=nodes
        home=root/'browser-home';nss=home/'.pki/nssdb';nss.mkdir(mode=0o700,parents=True)
        command('certutil','-N','--empty-password','-d','sql:'+str(nss))
        command('certutil','-A','-n','Disposable migration root','-t','C,,','-i',str(public),'-d','sql:'+str(nss))
        with sync_playwright() as browser_runtime:
            browser=browser_runtime.chromium.launch(executable_path=os.environ['PLAYWRIGHT_CHROMIUM_EXECUTABLE'],
                args=['--host-resolver-rules='+', '.join('MAP '+n.hostname+' '+n.host for n in nodes)],env={**os.environ,'HOME':str(home)})
            try:
                fleet.browser_promote(browser,master,'Master')
                for peer in (source,target):
                    fleet.browser_pair(browser,master,peer)
                    master.configure_resources(storage=True,capacity_gib=1,backup=False,relation=peer.relation)
                master.mode('Direct',relation=source.relation)
                modes={r['peer_id']:r['mode'] for r in master.nodes()['relationships']}
                check('fixture_has_an_explicit_direct_source_and_relay_target',modes[source.nodes()['node_id']]=='Direct' and modes[target.nodes()['node_id']]=='Relay')
                buffer=io.BytesIO()
                with wave.open(buffer,'wb') as output:output.setparams((1,2,8000,0,'NONE','not compressed'));output.writeframes(b'\x01\x00'*8000)
                payload=buffer.getvalue()
                for parent in ('music/migration-a','music/migration-b'):
                    master.upload_media(payload,'original.wav',site_type='direct',target_dir=parent)
                context,page=fleet.admin_page(browser,master);errors=[];inputs=[];encryption=[]
                report['actual_gui_inputs']=inputs
                check('native_admin_loads_migration_client',page.locator('#migrationPlanForm').evaluate('e=>typeof e.onsubmit==="function"'))
                page.on('pageerror',lambda error:errors.append(str(error)))
                def record(request):
                    if '/media/admin/migrations' in request.url:
                        inputs.append(dict(method=request.method,url=request.url,body=request.post_data,
                            csrf_header_present='x-csrf-token' in request.headers))
                    if request.method=='POST' and request.url.endswith('/admin/upload/session'):
                        body=request.post_data_json
                        if body.get('storage_mode')=='encrypted':encryption.append(body['encryption'])
                page.on('request',record)
                def observed(response):
                    if '/media/admin/migrations/plans/' in response.url and response.request.method=='GET':
                        try:
                            value=response.json();report['last_preview']={k:value.get(k) for k in ('plan_id','state','object_count','total_bytes')}
                            report['last_preview']['blockers']=value.get('report',{}).get('blockers',[])
                        except Exception:pass
                page.on('response',observed)
                page.locator('[data-admin-module="media"] .module-heading').click()
                page.evaluate("async () => {currentPath='';await renderTree();}")
                page.locator('#tree [data-path="music"]').dblclick();page.locator('#tree [data-path="music/migration-a"]').dblclick()
                page.select_option('#uploadSiteType','direct');page.locator('#uploadBtn').hover();page.locator('#uploadFiles').click()
                with page.expect_file_chooser() as chosen:page.locator('dialog.upload-storage-mode-dialog').get_by_text('加密落盘',exact=True).click()
                chosen.value.set_files(dict(name='encrypted.wav',mimeType='audio/wav',buffer=payload))
                fleet.wait_page(page,"() => !uploadRunning && document.getElementById('uploadSummary').textContent === '完成：成功 1，失败 0'",timeout=90000)
                check('ordinary_browser_encrypted_upload_finished',len(encryption)==1)
                paths=['music/migration-a/original.wav','music/migration-b/original.wav','music/migration-a/encrypted.wav']
                encrypted_path=paths[2]
                if terminal:
                    from tests import migration_terminal_browser as terminal_case
                    reader=terminal_case.prepare_reader(master,source,root,report)
                    paths.append(terminal_case.READER_PATH)
                before=snapshot(master);originals={p:(source.data/'media'/p).read_bytes() for p in paths}
                original_business=json.loads(master.web("""import asyncio,json
from app.services.federation.catalog import catalog
from app.services.federation.state import state
async def read():
    await state.initialize()
    return [dict(resource_id=row['resource_id'],object_id=row['object_id'],path=row['path'],
        size=int(row['payload']['size']),url='/api/v1/media/stream?resource_id='+row['resource_id']) for row in await catalog.resources()]
print(json.dumps(asyncio.run(read())))""")) if terminal else []
                original_business=[row for row in original_business if row['path'] in paths]
                if terminal:check('original_business_urls_cover_every_planned_object',len(original_business)==len(paths))
                check('source_ciphertext_is_opaque',originals[encrypted_path]!=payload and len(originals[encrypted_path])==encryption[0]['ciphertext_size'])
                identity=[n.nodes()['node_id'] for n in nodes]
                page.locator('#migrationsPanel .module-heading').click()
                page.select_option('#migrationSource',identity[1]);page.select_option('#migrationTarget',identity[2])
                refused=page.evaluate("""async () => {const r=await fetch('/api/v1/media/admin/migrations/plans',
                    {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});return r.status;}""")
                check('actual_migration_route_requires_csrf',refused==403)
                page.get_by_role('button',name='只读计划',exact=True).click()
                page.wait_for_function("() => document.getElementById('migrationPreview').textContent.includes('可执行') || document.getElementById('migrationPreview').textContent.includes('未通过')",timeout=180000)
                check('actual_readonly_plan_is_ready',page.get_by_role('button',name='执行迁移',exact=True).count()==1)
                check('gui_readonly_plan_preserves_ownership_and_source',snapshot(master)==before and all(
                    (source.data/'media'/p).read_bytes()==value for p,value in originals.items()))
                page.locator('#migrationsPanel').screenshot(path=str(root/'migration-plan.png'))
                page.once('dialog',lambda dialog:dialog.accept());page.get_by_role('button',name='执行迁移',exact=True).click()
                expect(page.locator('#migrationTask')).to_contain_text('等待源端清理',timeout=180000)
                action=next(json.loads(r['body']) for r in inputs if r['method']=='POST' and r['url'].endswith('/start'))
                check('actual_gui_approval_binds_both_nodes',action['confirm_source_id']==identity[1] and action['confirm_target_id']==identity[2])
                after=snapshot(master)
                check('actual_gui_switches_all_logical_ids_to_target',set(after['owners'])==set(before['owners']) and set(after['owners'].values())=={identity[2]})
                check('catalog_fields_and_encryption_records_are_unchanged',all(before[k]==after[k] for k in ('catalog_sha256','encryption_sha256')))
                check('target_bytes_match_all_original_plain_and_ciphertext',all((target.data/'media'/p).read_bytes()==value for p,value in originals.items()))
                check('source_originals_remain_during_real_cleanup_grace',all((source.data/'media'/p).read_bytes()==value for p,value in originals.items()))
                check('master_has_no_migrated_payload_files',all(not (master.data/'media'/p).exists() for p in paths))
                decrypted=page.evaluate("""async ({path,fileId}) => {await FrontierMediaCrypto.ensureWorker();
                    const response=await fetch(FrontierMediaCrypto.virtualUrl(path,false,fileId),{cache:'no-store'});
                    const bytes=await response.arrayBuffer();return {status:response.status,size:bytes.byteLength,
                    sha256:Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),x=>x.toString(16).padStart(2,'0')).join('')};}
                    """,dict(path=encrypted_path,fileId=encryption[0]['file_id']))
                check('real_browser_decrypts_the_migrated_relay_object',decrypted==dict(status=200,size=len(payload),sha256=hashlib.sha256(payload).hexdigest()))
                page.locator('#migrationsPanel').screenshot(path=str(root/'migration-committed.png'))
                check('node_identities_are_unchanged',[n.nodes()['node_id'] for n in nodes]==identity)
                check('actual_migration_gui_has_no_page_errors',not errors)
                report.update(actual_gui_inputs=inputs,before=before,after=after,source_payload_sha256={p:hashlib.sha256(v).hexdigest() for p,v in originals.items()},
                    decrypted=decrypted,task_phase='cleanup_pending',independent_compose_nodes=3,actual_tls=True)
                if terminal:
                    terminal_case.finish(page,master,source,target,root,reader,report,check)
                    reader=None
                    check('post_retirement_target_ownership_catalog_and_encryption_unchanged',snapshot(master)==after)
                    business=[dict(row,range_sha256=hashlib.sha256(originals[row['path']][3:10]).hexdigest()) for row in original_business]
                    encrypted=dict(path=encrypted_path,resource_id=next(row['resource_id'] for row in business if row['path']==encrypted_path),
                        file_id=encryption[0]['file_id'],ciphertext_sha256=report['source_payload_sha256'][encrypted_path],
                        plaintext_sha256=hashlib.sha256(payload).hexdigest(),plaintext_size=len(payload))
                    terminal_case.read_after_retirement(browser,master,source,target,business,encrypted,report,check)
                context.close()
            finally:browser.close()
        report['resources']={}
        for node in nodes:
            limits=json.loads(command('docker','inspect',node.container('web')))[0]['HostConfig']
            check('one_cpu_512mib_'+node.name,limits['NanoCpus']==1000000000 and limits['Memory']==512*1024**2)
            samples=[];deadline=time.monotonic()+30;low=0
            while time.monotonic()<deadline:
                value=float(command('docker','stats','--no-stream','--format','{{.CPUPerc}}',node.container('web')).removesuffix('%'))
                samples.append(value);low=low+1 if value<20 else 0
                if low>=5:break
                time.sleep(1)
            check('quiescent_cpu_'+node.name,low>=5);report['resources'][node.name]=dict(cpu_samples=samples)
        report['passed']=True
    except BaseException:
        report['error']=traceback.format_exc();raise
    finally:
        if reader is not None:
            from tests.migration_terminal_browser import close_reader
            close_reader(reader)
        socket.getaddrinfo=fleet._original_getaddrinfo
        cleanup=[]
        for node in reversed(nodes):
            try:node.stop();cleanup.append(dict(project=node.project,stopped=True))
            except Exception as error:cleanup.append(dict(project=node.project,stopped=False,error=type(error).__name__))
        if created:
            try:command('docker','network','rm',network)
            except Exception as error:cleanup.append(dict(network=network,stopped=False,error=type(error).__name__))
        report['cleanup']=cleanup;(root/'result.json').write_text(json.dumps(report,sort_keys=True))
        if not all(v['stopped'] for v in cleanup):raise RuntimeError('Exact migration fixtures did not stop')


if __name__=='__main__':
    def interrupted(_signal,_frame):raise InterruptedError('Native migration fixture interrupted')
    signal.signal(signal.SIGTERM,interrupted)
    main()
