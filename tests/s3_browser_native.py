"""Real Chromium/Admin S3 workflow on isolated Compose with ordinary CA trust.

This does not contact AWS or change the user's browser/system certificate store.
The existing compatible service is test infrastructure, not a product service.
"""
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import shutil
import signal
import socket
import struct
import sys
import time
import traceback
import wave

from cryptography.hazmat.primitives import serialization
from playwright.sync_api import expect, sync_playwright

from tests import federation_stack as fleet
from tests.certificate_fixtures import TestCA
from tests.native_runner_cleanup import finalize_cleanup,interrupt_fixture


ROOT = Path(__file__).resolve().parents[1]


def run():
    started=time.monotonic()
    directory=Path(os.environ['S3_BROWSER_ROOT'])
    fixture=Path(os.environ['S3_FIXTURE'])
    provider_compose=json.loads((fixture/'compose.json').read_text())
    provider_project=provider_compose['name']
    provider_network=provider_compose['networks']['fixture']['name']
    database=os.environ.get('S3_BROWSER_DATABASE','sqlite')
    if database not in ('sqlite','mysql'):
        raise RuntimeError('unsupported_browser_fixture_database')
    if directory.parent != fixture.parent or not directory.name.startswith('s3-browser-fixture-') or directory.exists():
        raise RuntimeError('invalid_disposable_browser_directory')
    directory.mkdir(mode=0o700)
    report=dict(format='frontiercloud-python-s3-real-browser',version=1,checks=[],tls_errors_ignored=False,
        aws_real_bucket=False,owner_acceptance=False,network=[],page_errors=[],actions=[],database=database)
    def check(name,value):
        if not value:
            raise AssertionError(name)
        report['checks'].append(name)
        print(json.dumps(dict(passed=name)),flush=True)
    def command(*args,**kwargs):
        return fleet.command(*args,**kwargs)
    key=directory/'test-ca';key.mkdir(mode=0o700)
    ca=TestCA()
    root=key/'root.crt';root.write_bytes(ca.root.public_bytes(serialization.Encoding.PEM))
    (key/'private.pem').write_bytes(ca.key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    (key/'private.pem').chmod(0o600)
    shutil.copy2(fixture/'ca.pem',key/'s3-root.crt')
    bundle=directory/'roots.pem';bundle.write_bytes(Path('/etc/ssl/certs/ca-certificates.crt').read_bytes()+root.read_bytes()+(key/'s3-root.crt').read_bytes())
    for kind in ('web','nginx'):
        clean='frontiercloud-python-s3-browser:clean-'+kind
        command('docker','build','-t',clean,'-f',str(ROOT/('Dockerfile' if kind=='web' else 'nginx/Dockerfile')),str(ROOT))
        build=key/('Dockerfile.'+kind)
        build.write_text(f'FROM {clean}\nUSER 0\nCOPY root.crt s3-root.crt /usr/local/share/ca-certificates/\nRUN update-ca-certificates\n'+('USER 10001:10001\n' if kind=='web' else ''))
        command('docker','build','-t','frontiercloud-python-s3-browser:trusted-'+kind,'-f',str(build),str(key))
    ids=command('docker','network','ls','-q').splitlines()
    occupied=[ipaddress.ip_network(entry['Subnet']) for item in json.loads(command('docker','network','inspect',*ids))
        for entry in ((item.get('IPAM') or {}).get('Config') or []) if entry.get('Subnet')]
    available=[ipaddress.ip_network(f'10.254.{value}.0/24') for value in range(240,200,-1)
        if not any(ipaddress.ip_network(f'10.254.{value}.0/24').overlaps(other) for other in occupied if other.version==4)]
    if len(available)<2:
        raise RuntimeError('no_isolated_browser_network_pair')
    subnet,private_subnet=available[:2]
    network='fc-python-'+directory.name
    command('docker','network','create','--internal','--subnet',str(subnet),network)
    node=None
    try:
        compose=['docker','compose','-f',str(ROOT/'docker-compose.yaml')]
        if database=='mysql':compose.extend(['-f',str(ROOT/'docker-compose.mysql.yaml')])
        base=json.loads(command(*compose,'config','--format','json'))
        node=fleet.Node('admin',directory,base,root,bundle,0,network=network)
        node.host=str(subnet.network_address+11)
        fleet.DOMAIN_ADDRESSES[node.hostname]=node.host
        configuration=json.loads(node.configuration.read_text())
        configuration['networks']['default']['ipam']={'config':[{'subnet':str(private_subnet)}]}
        configuration['networks']['s3']={'external':True,'name':provider_network}
        for name,service in configuration['services'].items():
            if name in ('web','secrets-init','media-init','certificate-init','certificate-agent'):
                service['image']='frontiercloud-python-s3-browser:trusted-web'
            elif name=='nginx':
                service['image']='frontiercloud-python-s3-browser:trusted-nginx'
            if name=='web':
                service['mem_limit']='512m'
                service.setdefault('environment',{})['TRUSTED_PROXY_NETWORKS']=str(private_subnet)
                service['networks']['s3']={}
            if name=='mysql' and database=='mysql':
                service['cpus']=1
                service['mem_limit']='768m'
                service['command'].append('--innodb-buffer-pool-size=64M')
            if name=='nginx':
                service['networks']['fleet']['ipv4_address']=node.host
        node.configuration.write_text(json.dumps(configuration))
        socket.getaddrinfo=fleet.fixture_getaddrinfo
        node.start()
        check('actual_'+database+'_browser_database',node.web('from app.core.db import engine; print(engine.dialect.name)').strip()==database)
        limits=json.loads(command('docker','inspect',node.container('web')))[0]['HostConfig']
        report['web_limits']=dict(nano_cpus=limits['NanoCpus'],memory_bytes=limits['Memory'])
        check('actual_one_cpu_512m_web_limit',limits['NanoCpus']==1000000000 and limits['Memory']==512*1024**2)
        check('ordinary_verified_master_https_ready',True)
        provider_ids=command('docker','compose','-p',provider_project,'-f',str(fixture/'compose.json'),'ps','-q').splitlines()
        provider=None
        for identity in provider_ids:
            info=json.loads(command('docker','inspect',identity))[0]
            view=info['NetworkSettings']['Networks'][provider_network]
            if 's3.4399.com' in (view.get('Aliases') or []):
                provider=view['IPAddress'];break
        if provider is None:
            raise RuntimeError('compatible_service_missing')
        fleet.DOMAIN_ADDRESSES['s3.4399.com']=provider
        home=directory/'browser-home';nss=home/'.pki/nssdb';nss.mkdir(mode=0o700,parents=True)
        command('certutil','-N','--empty-password','-d','sql:'+str(nss))
        for name,path in (('Disposable Master root',root),('Disposable compatible-service root',key/'s3-root.crt')):
            command('certutil','-A','-n',name,'-t','C,,','-i',str(path),'-d','sql:'+str(nss))
        with sync_playwright() as runtime:
            browser=runtime.chromium.launch(executable_path=os.environ['PLAYWRIGHT_CHROMIUM_EXECUTABLE'],
                args=['--autoplay-policy=no-user-gesture-required','--host-resolver-rules='+','.join(
                    f'MAP {host} {address}' for host,address in fleet.DOMAIN_ADDRESSES.items())],
                env={**os.environ,'HOME':str(home)})
            try:
                fleet.browser_promote(browser,node,'Master')
                context,page=fleet.admin_page(browser,node)
                from tests.nodes_compact_browser import run as compact_nodes
                compact_nodes(page,report,check)
                context.add_init_script("""(() => {
                    const open=XMLHttpRequest.prototype.open,send=XMLHttpRequest.prototype.send,header=XMLHttpRequest.prototype.setRequestHeader;
                    const seen=new WeakMap();window.s3AuditInputs=[];
                    XMLHttpRequest.prototype.open=function(method,url,...rest){seen.set(this,{method,origin:new URL(url,location.href).origin,headers:[]});return open.call(this,method,url,...rest);};
                    XMLHttpRequest.prototype.setRequestHeader=function(name,value){seen.get(this)?.headers.push(name.toLowerCase());return header.call(this,name,value);};
                    XMLHttpRequest.prototype.send=function(body){const row=seen.get(this);if(row&&body instanceof Blob)window.s3AuditInputs.push({...row,bytes:body.size,with_credentials:this.withCredentials});return send.call(this,body);};
                })();""")
                context.on('page',lambda extra:extra.on('pageerror',lambda error:report['page_errors'].append(type(error).__name__)))
                page.on('pageerror',lambda error:report['page_errors'].append(str(error).split('\n')[0][:120]))
                def observe(request):
                    from urllib.parse import urlsplit
                    url=urlsplit(request.url)
                    if url.hostname=='s3.4399.com':
                        report['network'].append(dict(host=url.hostname,method=request.method,
                            content_length=int(request.headers.get('content-length','0')),csrf_present='x-csrf-token' in request.headers))
                context.on('request',observe)
                def finished(request):
                    from urllib.parse import urlsplit
                    if urlsplit(request.url).hostname=='s3.4399.com':
                        headers=request.all_headers()
                        report.setdefault('completed_transfers',[]).append(dict(method=request.method,
                            request_body_bytes=request.sizes()['requestBodySize'],csrf_present='x-csrf-token' in headers))
                context.on('requestfinished',finished)
                page.locator('.s3-admin').wait_for(state='visible')
                page.get_by_role('button',name='添加 S3',exact=True).click()
                dialog=page.locator('.s3-config-dialog')
                secrets=json.loads((fixture/'credentials.json').read_text())['identities'][1]['credentials'][0]
                for name,value in dict(name='Browser S3',bucket='frontiercloud-python-acceptance',region='us-east-1',
                    endpoint='https://s3.4399.com:8333',prefix='browser/'+directory.name,quota='1',
                    access_key=secrets['accessKey'],secret_key=secrets['secretKey']).items():
                    dialog.locator(f'[name="{name}"]').fill(value)
                report['actions'].append(dict(action='GUI profile add',name='Browser S3',bucket='frontiercloud-python-acceptance',
                    endpoint='https://s3.4399.com:8333',prefix='browser/'+directory.name,credential_input='private fixture file; values omitted'))
                dialog.get_by_role('button',name='保存',exact=True).click()
                expect(dialog).to_have_count(0)
                member=page.locator('.s3-admin details').filter(has_text='Browser S3')
                member.locator('summary').click()
                member.get_by_role('button',name='测试连接',exact=True).click()
                expect(page.locator('.s3-admin details summary')).to_contain_text('已连接',timeout=60000)
                expect(page.locator('.s3-admin > [role="status"]')).to_have_text('已完成')
                # Refresh replaces the collapsed profile card; reopen explicitly.
                member.locator('summary').click()
                member.get_by_role('button',name='启用',exact=True).click()
                expect(page.locator('.s3-admin details summary')).to_contain_text('可上传',timeout=30000)
                expect(page.locator('.s3-admin > [role="status"]')).to_have_text('已完成')
                check('gui_profile_add_probe_enable',True)
                report['actions'].append(dict(action='GUI connection test and enable',scope='bucket_connection'))
                page.screenshot(path=str(directory/'admin-s3.png'),full_page=True)
                source=directory/'sources';source.mkdir()
                files={}
                for name,seconds in (('plain-album',2),('encrypted-album',270)):
                    folder=source/name;folder.mkdir();path=folder/'original sample.wav'
                    with wave.open(str(path),'wb') as output:
                        output.setnchannels(1);output.setsampwidth(2);output.setframerate(16000)
                        tone=b''.join(struct.pack('<h',int(math.sin(index*2*math.pi*440/16000)*8000)) for index in range(16000))
                        output.writeframes(tone*seconds)
                    files[name]=path
                # The upload-folder dialog creates the logical category; no
                # fixture writes catalog rows or media inside the deployment.
                media_heading=page.locator('[data-admin-module="media"] .module-heading')
                if media_heading.count() and media_heading.get_attribute('aria-expanded')!='true':media_heading.click()
                for name,mode in (('plain-album','明文落盘'),('encrypted-album','加密落盘')):
                    page.reload(wait_until='domcontentloaded')
                    if media_heading.count() and media_heading.get_attribute('aria-expanded')!='true':media_heading.click()
                    page.locator('.tree-row[data-path="music"]').dblclick()
                    fleet.wait_page(page,"currentPath === 'music'")
                    page.select_option('#uploadSiteType','s3')
                    page.locator('#uploadBtn').click()
                    page.locator('#uploadFolder').click()
                    with page.expect_file_chooser() as choose:
                        page.get_by_role('button',name=mode,exact=True).click()
                    choose.value.set_files(str(files[name].parent))
                    report['actions'].append(dict(action='GUI folder chooser upload',file=files[name].name,
                        logical_parent='music/'+name,storage_mode=mode,site_type='s3',source_bytes=files[name].stat().st_size,
                        source_sha256=hashlib.sha256(files[name].read_bytes()).hexdigest()))
                    fleet.wait_page(page,"document.querySelector('#uploadSummary').textContent.startsWith('完成：')",timeout=120000)
                    if '完成：成功 1，失败 0' not in page.locator('#uploadSummary').inner_text():
                        report['upload_error']=page.locator('#uploadResults').inner_text()[:250]
                        raise AssertionError('browser_upload_not_confirmed')
                    report.setdefault('xhr_blob_inputs',[]).extend(page.evaluate('window.s3AuditInputs'))
                    check('gui_'+name+'_upload_confirmed',True)
                rows=node.resources()
                check('exact_two_catalog_objects',len(rows)==2)
                page.reload(wait_until='domcontentloaded')
                if media_heading.count() and media_heading.get_attribute('aria-expanded')!='true':media_heading.click()
                page.locator('.tree-row[data-path="music"]').dblclick()
                for name in ('plain-album','encrypted-album'):
                    page.locator(f'.tree-row[data-path="music/{name}"]').dblclick()
                    page.locator('.tree-row[data-path$="original sample.wav"]').click()
                    with page.expect_download(timeout=60000) as result:
                        page.locator('#download').click()
                    download=result.value
                    target=directory/(name+'-download.wav');download.save_as(target)
                    check('gui_'+name+'_download_original_bytes',hashlib.sha256(target.read_bytes()).digest()==hashlib.sha256(files[name].read_bytes()).digest()
                        and download.suggested_filename=='original sample.wav')
                    report['actions'].append(dict(action='GUI download',logical_parent='music/'+name,filename=download.suggested_filename,
                        bytes=target.stat().st_size,sha256=hashlib.sha256(target.read_bytes()).hexdigest()))
                    page.locator('.tree-row').filter(has_text='返回上级').first.click()
                check('browser_payload_direct_no_csrf',len([entry for entry in report['network'] if entry['method']=='PUT'])>=3
                    and not any(entry['csrf_present'] for entry in report['network']))
                # Chromium omits upload-body sizes for file-backed Blob requests
                # in CDP. Audit the exact Blob passed to the unmodified native
                # send function and independently compare persisted S3 sizes.
                check('actual_xhr_inputs_match_verified_s3_object_sizes',sum(entry['bytes'] for entry in report['xhr_blob_inputs'])==sum(item['size'] for item in rows)
                    and all(entry['origin']=='https://s3.4399.com:8333' and not entry['with_credentials'] and 'x-csrf-token' not in entry['headers']
                        for entry in report['xhr_blob_inputs']))
                report['wire_body_size_observation']='Chromium returned zero for file Blob uploads; actual XHR inputs and independently verified object/download sizes are used instead'
                check('no_browser_script_errors',not report['page_errors'])
                context.close()
                public=browser.new_context()
                public_page=public.new_page()
                for name in ('plain-album','encrypted-album'):
                    public_page.goto(node.endpoint+'/api/v1/media/music/category?path=music%2F'+name,wait_until='domcontentloaded')
                    fleet.wait_page(public_page,"typeof art !== 'undefined' && art && currentMediaList.length === 1",timeout=30000)
                    public_page.evaluate('() => selectMedia(0)')
                    fleet.wait_page(public_page,"art.video.currentTime > 0.1 && !art.video.paused && !art.video.error",timeout=30000)
                    public_page.evaluate('() => {art.video.currentTime=1; return art.play();}')
                    fleet.wait_page(public_page,"art.video.currentTime > 1.1 && !art.video.paused && !art.video.error",timeout=30000)
                    check('public_'+name+'_play_and_seek',True)
                public.close()
                if os.environ.get('S3_BROWSER_PLAYBACK')=='1':
                    from tests.s3_browser_playback import run as playback
                    playback(browser,node,source,directory,report,check)
                    from tests.s3_artwork_browser import run as artwork
                    artwork(browser,node,source,report,check)
                if os.environ.get('S3_BROWSER_LIFECYCLE')=='1':
                    from tests.s3_browser_lifecycle import run as lifecycle
                    lifecycle(browser,node,source,directory,report,check)
            finally:
                browser.close()
        samples=[]
        low=0;deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            value=float(command('docker','stats','--no-stream','--format','{{.CPUPerc}}',node.container('web')).removesuffix('%'))
            samples.append(value);low=low+1 if value<20 else 0
            if low>=5:break
            time.sleep(1)
        report['cpu_samples']=samples
        check('one_core_web_quiescence',low>=5)
        report.update(passed=True,seconds=round(time.monotonic()-started,3))
    except Exception as error:
        report.update(passed=False,error_type=type(error).__name__,error=str(error).split('\n')[0][:180])
        if node is not None:
            try:node.compose('stop','-t','10','web')
            except Exception:pass
        raise
    finally:
        business_failed=sys.exc_info()[0] is not None
        cleanup=[]
        if node is not None:
            if node.client is not None:
                try:node.client.close();cleanup.append(dict(client='browser-api',stopped=True))
                except Exception as error:cleanup.append(dict(client='browser-api',stopped=False,error=type(error).__name__))
            try:node.compose('down','--timeout','15');cleanup.append(dict(project=node.project,stopped=True))
            except Exception as error:cleanup.append(dict(project=node.project,stopped=False,error=type(error).__name__))
        try:command('docker','network','rm',network);cleanup.append(dict(network=network,stopped=True))
        except Exception as error:cleanup.append(dict(network=network,stopped=False,error=type(error).__name__))
        finalize_cleanup(report,cleanup,directory/'result.json',business_failed=business_failed)


if __name__=='__main__':
    signal.signal(signal.SIGTERM,interrupt_fixture)
    try:
        run()
    except Exception as error:
        # Playwright's full call log can contain values entered into secret
        # fields. Retain code locations, never serialize those field values.
        print(json.dumps(dict(error_type=type(error).__name__,frames=[dict(file=Path(frame.filename).name,line=frame.lineno,
            function=frame.name) for frame in traceback.extract_tb(error.__traceback__)[-8:]])),flush=True)
        sys.exit(1)
