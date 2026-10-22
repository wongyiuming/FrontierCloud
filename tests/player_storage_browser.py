"""Actual Local/Direct/Relay long-MP3 and per-track artwork browser acceptance."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import struct
import sys
import traceback
import zlib
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from playwright.sync_api import sync_playwright

from tests import federation_stack as fleet
from tests.certificate_fixtures import TestCA
from tests.native_runner_cleanup import finalize_cleanup,interrupt_fixture


ROOT=Path(__file__).resolve().parents[1]


def image(red,blue):
    def chunk(kind,value):return struct.pack('>I',len(value))+kind+value+struct.pack('>I',zlib.crc32(kind+value))
    scan=b''.join(b'\0'+bytes((red,index,blue,255))*64 for index in range(64))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',64,64,8,6,0,0,0))+chunk(b'IDAT',zlib.compress(scan))+chunk(b'IEND',b'')


def tag(title,picture):
    def frame(name,value):return name.encode()+struct.pack('>I',len(value))+b'\0\0'+value
    body=frame('TIT2',b'\x03'+title.encode())+frame('TPE1',b'\x03Native fixture')+frame('APIC',b'\x03image/png\0\x03\0'+picture)
    size=len(body)
    return b'ID3\x03\0\0'+bytes((size>>21&127,size>>14&127,size>>7&127,size&127))+body


def media(folder):
    folder.mkdir()
    tone=(ROOT/'tests/fixtures/tone.mp3').read_bytes()
    if tone[:3]==b'ID3':
        length=10+sum((value&127)<<(21-index*7) for index,value in enumerate(tone[6:10]));tone=tone[length:]
    pictures=[image(220,20),image(20,220)]
    long=folder/'01-long.mp3'
    with long.open('wb') as stream:
        stream.write(tag('Long original',pictures[0]))
        for _ in range((129*1024**2)//len(tone)+1):stream.write(tone)
    (folder/'02-next.mp3').write_bytes(tag('Next original',pictures[1])+tone*3)
    return pictures


def exercise(browser,master,root,report,check):
    folder=root/'album';pictures=media(folder)
    context,page=fleet.admin_page(browser,master)
    for site in ('primary','direct','relay'):
        for mode in ('明文落盘','加密落盘'):
            parent=site+('-plain' if mode=='明文落盘' else '-encrypted')
            selected=root/parent;selected.mkdir()
            for p in folder.iterdir():os.link(p,selected/p.name)
            page.reload(wait_until='domcontentloaded')
            heading=page.locator('[data-admin-module="media"] .module-heading')
            if heading.get_attribute('aria-expanded')!='true':heading.click()
            page.locator('.tree-row[data-path="music"]').dblclick()
            fleet.wait_page(page,"currentPath === 'music'")
            page.select_option('#uploadSiteType',site)
            page.locator('#uploadBtn').click();page.locator('#uploadFolder').click()
            with page.expect_file_chooser() as chooser:page.get_by_role('button',name=mode,exact=True).click()
            chooser.value.set_files(str(selected))
            report['inputs'].append(dict(action='GUI folder upload',site_type=site,storage_mode=mode,parent=parent,
                files=[dict(name=p.name,bytes=p.stat().st_size) for p in selected.iterdir()]))
            fleet.wait_page(page,"document.querySelector('#uploadSummary').textContent.startsWith('完成：')",timeout=180000)
            summary=page.locator('#uploadSummary').inner_text()
            check(parent+'_actual_upload','成功 2，失败 0' in summary)
    report['uploaded_catalog']=master.resources()
    expected_paths={'music/'+site+'-'+mode+'/'+filename for site in ('primary','direct','relay')
        for mode in ('plain','encrypted') for filename in ('01-long.mp3','02-next.mp3')}
    check('all_gui_uploads_publish_under_the_selected_album',
        {row['path'] for row in report['uploaded_catalog']}==expected_paths)
    context.close()
    for site in ('primary','direct','relay'):
        for mode in ('plain','encrypted'):
            name=site+'-'+mode;context=browser.new_context();page=context.new_page();errors=[];responses=[]
            page.on('pageerror',lambda error:errors.append(type(error).__name__))
            page.on('response',lambda response:responses.append(dict(path=urlsplit(response.url).path,status=response.status)))
            try:
                page.goto(master.endpoint+'/api/v1/media/music/category?path=music%2F'+name,wait_until='domcontentloaded')
                fleet.wait_page(page,"typeof art!=='undefined' && art && currentMediaList.length===2",timeout=30000)
                index=page.evaluate("currentMediaList.findIndex(row=>row.media_path.endsWith('/01-long.mp3'))");next_index=1-index
                page.evaluate('(index)=>selectMedia(index)',index)
                fleet.wait_page(page,f'currentIndex==={index} && frontierCloudContinuousAudio.status().active && art.currentTime>0.2 && art.duration>8000 && !art.video.error',timeout=45000)
                check(name+'_above_128mib_mse_playback',True)
                page.evaluate('(index)=>selectMedia(index)',next_index)
                fleet.wait_page(page,f'currentIndex==={next_index} && art.currentTime>0.1 && !art.video.paused && !art.video.error',timeout=30000)
                check(name+'_manual_next',True)
                page.evaluate('(index)=>selectMedia(index)',index)
                fleet.wait_page(page,f'currentIndex==={index} && art.currentTime>0.1 && art.duration>8000',timeout=30000)
                page.evaluate('() => {art.currentTime=art.duration-2;return art.play();}')
                fleet.wait_page(page,f'currentIndex==={next_index} && art.currentTime>0.1 && !art.video.paused && !art.video.error',timeout=45000)
                check(name+'_real_tail_automatic_next',True)
                fleet.wait_page(page,"currentMediaList.every(item => ['Long original','Next original'].includes(item.title) && item.cover && item.cover !== '/favicon.ico')",timeout=30000)
                check(name+'_selected_and_visible_metadata_arrive_without_bulk_forcing',True)
                tags=page.evaluate("""async () => {
                    const rows=await Promise.all(currentMediaList.map(async item=>{
                        const value=item;
                        let picture;
                        if(item.encryption){
                            const url=FrontierMediaCrypto.virtualUrl(item.media_path,false,item.encryption.file_id);
                            const bytes=await (await fetch(url,{headers:{Range:'bytes=0-16383'},cache:'no-store'})).arrayBuffer();
                            picture=FrontierMusicMetadata.parse(new Uint8Array(bytes)).picture.bytes;
                        }else picture=await (await fetch(value.cover)).arrayBuffer();
                        const dimensions=await new Promise((resolve,reject)=>{
                            const image=new Image();image.onload=()=>resolve([image.naturalWidth,image.naturalHeight]);
                            image.onerror=()=>reject(new Error('Original picture could not be displayed'));image.src=value.cover;
                        });
                        return {path:item.media_path,title:value.title,picture_sha256:Array.from(new Uint8Array(
                            await crypto.subtle.digest('SHA-256',picture)),x=>x.toString(16).padStart(2,'0')).join(''),dimensions};
                    }));return rows;
                }""")
                expected={'01-long.mp3':('Long original',hashlib.sha256(pictures[0]).hexdigest()),
                    '02-next.mp3':('Next original',hashlib.sha256(pictures[1]).hexdigest())}
                check(name+'_each_track_retains_its_own_original_picture',all(
                    (row['title'],row['picture_sha256'])==expected[row['path'].rsplit('/',1)[1]] and row['dimensions']==[64,64] for row in tags))
                check(name+'_no_browser_errors',not errors)
                report.setdefault('playback',[]).append(dict(site_type=site,storage_mode=mode,metadata=tags,
                    pipeline=page.evaluate('frontierCloudContinuousAudio.status()'),whole_track_elapsed=False,real_tail_seek=True))
            except BaseException:
                report['failed_case']=name;report['failed_responses']=responses[-40:];report['page_errors']=errors
                report['failed_page']=page.evaluate('() => ({title:document.title,body:document.body.innerText.slice(0,1000),tracks:typeof currentMediaList!=="undefined"?currentMediaList.length:null,has_player:typeof art!=="undefined"&&Boolean(art)})')
                raise
            finally:context.close()


def main():
    five=os.environ.get('FC_FIVE_NODE_TRAFFIC')=='1'
    faults=os.environ.get('FC_FILESYSTEM_UPLOAD_FAULTS')=='1'
    if five and faults:raise RuntimeError('Select one native profile')
    names=('traffic-master','traffic-source','traffic-target','traffic-load-one','traffic-load-two') if five else ('player-master','player-direct','player-relay')
    root=Path(os.environ['FC_PLAYER_STORAGE_ROOT']).resolve()
    if root.exists() or not root.name.startswith('player-storage-fixture-'):raise RuntimeError('Fixture root must be new')
    root.mkdir(mode=0o700,parents=True);token=hashlib.sha256(str(root).encode()).hexdigest()[:12]
    report=dict(format='frontiercloud-python-player-storage-browser',checks=[],inputs=[],passed=False,
        production_changed=False,tls_errors_ignored=False);nodes=[];created=False
    if five:report['format']='frontiercloud-python-five-node-migration-traffic'
    if faults:report['format']='frontiercloud-python-filesystem-upload-faults'
    def check(name,value):
        if not value:raise AssertionError(name)
        report['checks'].append(name);print(json.dumps(dict(passed=name)),flush=True)
    ca=TestCA();key=root/'test-ca';key.mkdir(mode=0o700);public=key/'root.crt'
    public.write_bytes(ca.root.public_bytes(serialization.Encoding.PEM))
    private=key/'private.pem';private.write_bytes(ca.key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));private.chmod(0o600)
    bundle=root/'roots.pem';bundle.write_bytes(Path('/etc/ssl/certs/ca-certificates.crt').read_bytes()+public.read_bytes())
    command=fleet.command;network='fc-player-storage-'+token
    try:
        images={}
        for kind,dockerfile in [('web','Dockerfile'),('nginx','nginx/Dockerfile')]:
            clean='fc-player-storage:'+token+'-clean-'+kind;command('docker','build','-t',clean,'-f',str(ROOT/dockerfile),str(ROOT))
            build=key/('Dockerfile.'+kind);build.write_text('FROM '+clean+'\nUSER 0\nCOPY root.crt /usr/local/share/ca-certificates/\nRUN update-ca-certificates\n'+('USER 10001:10001\n' if kind=='web' else ''))
            images[kind]='fc-player-storage:'+token+'-trusted-'+kind;command('docker','build','-t',images[kind],'-f',str(build),str(key))
        ids=command('docker','network','ls','-q').splitlines();network_rows=json.loads(command('docker','network','inspect',*ids)) if ids else []
        occupied=[ipaddress.ip_network(row['Subnet']) for item in network_rows for row in ((item.get('IPAM') or {}).get('Config') or []) if row.get('Subnet')]
        choices=[value for index in range(250,100,-1) for value in [ipaddress.ip_network(f'10.246.{index}.0/24')]
            if not any(value.overlaps(other) for other in occupied if other.version==4)]
        if len(choices)<len(names)+1:raise RuntimeError('No isolated player fixture networks')
        subnet=choices[0];command('docker','network','create','--internal','--subnet',str(subnet),network);created=True
        base=json.loads(command('docker','compose','-f',str(ROOT/'docker-compose.yaml'),'config','--format','json'))
        for index,name in enumerate(names):
            node=fleet.Node(name,root,base,public,bundle,index,network=network,address=str(subnet.network_address+11+index),private_subnet=choices[index+1]);nodes.append(node)
            config=json.loads(node.configuration.read_text())
            for service_name,service in config['services'].items():
                if service_name in ('web','secrets-init','media-init','certificate-init','certificate-agent'):service['image']=images['web']
                elif service_name=='nginx':service['image']=images['nginx']
            node.configuration.write_text(json.dumps(config))
        socket.getaddrinfo=fleet.fixture_getaddrinfo
        for node in nodes:node.start()
        master,direct,relay=nodes[:3]
        with sync_playwright() as runtime:
            browser=fleet.launch_browser(runtime,nodes)
            try:
                fleet.browser_promote(browser,master,'Master')
                for peer in nodes[1:]:
                    fleet.browser_pair(browser,master,peer);master.configure_resources(storage=True,capacity_gib=1,backup=False,relation=peer.relation)
                master.mode('Direct',relation=direct.relation)
                if five:
                    from tests.migration_traffic_browser import exercise as traffic
                    traffic(nodes,root,report,check)
                elif faults:
                    from tests.filesystem_upload_browser import exercise as upload_faults
                    upload_faults(browser,nodes,root,report,check)
                elif os.environ.get('FC_PLAYER_STORAGE_BASELINE_ONLY')=='1':
                    report['baseline_only']=True
                    check('baseline_has_no_media_objects',not master.resources())
                elif os.environ.get('FC_PLAYER_PRESENTATION_ONLY')=='1':
                    report['presentation_only']=True
                    media(root/'album')
                    from tests.player_presentation_browser import exercise as presentation
                    presentation(browser,master,root,report,check)
                else:
                    exercise(browser,master,root,report,check)
                    from tests.player_presentation_browser import exercise as presentation
                    presentation(browser,master,root,report,check)
            finally:browser.close()
        report['resources']={}
        for node in nodes:
            config=json.loads(command('docker','inspect',node.container('web')))[0]['HostConfig']
            check(node.name+'_one_cpu_512mib',config['NanoCpus']==1000000000 and config['Memory']==512*1024**2)
            cpu=command('python3',str(ROOT/'scripts/check_cpu_quiescence.py'),node.container('web'),'--timeout','30')
            check(node.name+'_quiescent_cpu','for 5 samples' in cpu)
            peak=int(node.compose('exec','-T','web','cat','/sys/fs/cgroup/memory.peak'))
            memory=json.loads(node.web("""from pathlib import Path
import json
root=Path('/sys/fs/cgroup')
events={key:int(value) for key,value in (line.split() for line in (root/'memory.events').read_text().splitlines())}
stat={key:int(value) for key,value in (line.split() for line in (root/'memory.stat').read_text().splitlines()) if key in ('anon','file','kernel','slab')}
rss=[]
for path in Path('/proc').iterdir():
 if not path.name.isdigit():continue
 try:
  status=(path/'status').read_text()
  rss.append(int(next(line.split()[1] for line in status.splitlines() if line.startswith('VmRSS:')))*1024)
 except (OSError,StopIteration):pass
print(json.dumps(dict(events=events,stat=stat,current_bytes=int((root/'memory.current').read_text()),process_rss_bytes=sum(rss),probe_rss_included=True)))
"""))
            check(node.name+'_no_oom',memory['events']['oom']==0 and memory['events']['oom_kill']==0)
            report['resources'][node.name]=dict(cpu=cpu,cgroup_peak_bytes=peak,memory=memory)
        report['passed']=True
    except BaseException:
        report['error']=traceback.format_exc();raise
    finally:
        business_failed=sys.exc_info()[0] is not None
        socket.getaddrinfo=fleet._original_getaddrinfo
        cleanup=[]
        for node in reversed(nodes):
            try:node.stop();cleanup.append(dict(project=node.project,stopped=True))
            except Exception as error:cleanup.append(dict(project=node.project,stopped=False,error=type(error).__name__))
        if created:
            try:command('docker','network','rm',network);cleanup.append(dict(network=network,stopped=True))
            except Exception as error:cleanup.append(dict(network=network,stopped=False,error=type(error).__name__))
        finalize_cleanup(report,cleanup,root/'result.json',business_failed=business_failed)


if __name__=='__main__':
    signal.signal(signal.SIGTERM,interrupt_fixture)
    main()
