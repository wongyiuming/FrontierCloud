"""Verified-HTTPS node layout gate, confined to its own Compose and browser roots."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import sys
import time
import traceback

from cryptography.hazmat.primitives import serialization
from playwright.sync_api import expect,sync_playwright

from tests import federation_stack as fleet
from tests.certificate_fixtures import TestCA
from tests.nodes_compact_browser import run as compact_nodes
from tests.node_retirement_browser import run as retirement_nodes
from tests.native_runner_cleanup import finalize_cleanup,interrupt_fixture


ROOT=Path(__file__).resolve().parents[1]


def main():
    root=Path(os.environ['FC_NODES_BROWSER_ROOT']).resolve()
    if root.exists() or not root.name.startswith('nodes-browser-fixture-'):
        raise RuntimeError('nodes_browser_fixture_must_be_new')
    root.mkdir(mode=0o700,parents=True)
    tag=hashlib.sha256(str(root).encode()).hexdigest()[:12]
    network='fc-python-nodes-ui-'+tag
    report=dict(format='frontiercloud-python-nodes-browser',version=1,checks=[],passed=False,
        tls_errors_ignored=False,host_ports=[],production_changed=False)
    def check(name,value):
        if not value:raise AssertionError(name)
        report['checks'].append(name)
        print(json.dumps(dict(passed=name)),flush=True)
    command=fleet.command
    key=root/'test-ca';key.mkdir(mode=0o700)
    ca=TestCA()
    public=key/'root.crt';public.write_bytes(ca.root.public_bytes(serialization.Encoding.PEM))
    private=key/'private.pem'
    private.write_bytes(ca.key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    private.chmod(0o600)
    bundle=root/'roots.pem';bundle.write_bytes(Path('/etc/ssl/certs/ca-certificates.crt').read_bytes()+public.read_bytes())
    images={}
    for kind in ('web','nginx'):
        clean=f'fc-python-nodes-ui:{tag}-clean-{kind}'
        command('docker','build','-t',clean,'-f',str(ROOT/('Dockerfile' if kind=='web' else 'nginx/Dockerfile')),str(ROOT))
        build=key/('Dockerfile.'+kind)
        build.write_text(f'FROM {clean}\nUSER 0\nCOPY root.crt /usr/local/share/ca-certificates/\nRUN update-ca-certificates\n'+('USER 10001:10001\n' if kind=='web' else ''))
        images[kind]=f'fc-python-nodes-ui:{tag}-trusted-{kind}'
        command('docker','build','-t',images[kind],'-f',str(build),str(key))
    node=None;peer=None;created=False
    try:
        command('docker','network','create','--internal',network);created=True
        info=json.loads(command('docker','network','inspect',network))[0]
        subnet=ipaddress.ip_network(info['IPAM']['Config'][0]['Subnet'])
        networks=json.loads(command('docker','network','inspect',*command('docker','network','ls','-q').splitlines()))
        occupied=[ipaddress.ip_network(row['Subnet']) for item in networks
            for row in ((item.get('IPAM') or {}).get('Config') or []) if row.get('Subnet')]
        private_subnet=next(candidate for value in range(240,200,-1) for candidate in [ipaddress.ip_network(f'10.253.{value}.0/24')]
            if not any(candidate.overlaps(other) for other in occupied if other.version==4))
        base=json.loads(command('docker','compose','-f',str(ROOT/'docker-compose.yaml'),'config','--format','json'))
        node=fleet.Node('admin',root,base,public,bundle,0,network=network)
        node.host=str(subnet.network_address+11)
        fleet.DOMAIN_ADDRESSES[node.hostname]=node.host
        configuration=json.loads(node.configuration.read_text())
        configuration['networks']['default']['ipam']={'config':[{'subnet':str(private_subnet)}]}
        for name,service in configuration['services'].items():
            if name in ('web','secrets-init','media-init','certificate-init','certificate-agent'):service['image']=images['web']
            if name=='nginx':
                service['image']=images['nginx']
                service['networks']['fleet']['ipv4_address']=node.host
            if name=='web':
                service['mem_limit']='512m'
                service.setdefault('environment',{})['TRUSTED_PROXY_NETWORKS']=str(private_subnet)
        node.configuration.write_text(json.dumps(configuration))
        socket.getaddrinfo=fleet.fixture_getaddrinfo
        node.start()
        peer=fleet.Node('retire-direct',root,base,public,bundle,1,network=network)
        peer.host=str(subnet.network_address+12);fleet.DOMAIN_ADDRESSES[peer.hostname]=peer.host
        configuration=json.loads(peer.configuration.read_text())
        for name,service in configuration['services'].items():
            if name in ('web','secrets-init','media-init','certificate-init','certificate-agent'):service['image']=images['web']
            if name=='web':
                service['mem_limit']='512m';service['networks']['fleet']['ipv4_address']=peer.host
        peer.configuration.write_text(json.dumps(configuration));peer.start()
        home=root/'browser-home';nss=home/'.pki/nssdb';nss.mkdir(mode=0o700,parents=True)
        command('certutil','-N','--empty-password','-d','sql:'+str(nss))
        command('certutil','-A','-n','Disposable nodes root','-t','C,,','-i',str(public),'-d','sql:'+str(nss))
        with sync_playwright() as runtime:
            browser=runtime.chromium.launch(executable_path=os.environ['PLAYWRIGHT_CHROMIUM_EXECUTABLE'],
                args=['--autoplay-policy=no-user-gesture-required','--host-resolver-rules=MAP '+node.hostname+' '+node.host+', MAP '+peer.hostname+' '+peer.host],
                env={**os.environ,'HOME':str(home)})
            try:
                context,page=fleet.admin_page(browser,node)
                cards=page.locator('#nodeRelationships .node-card')
                expect(cards).to_have_count(1)
                expect(cards.get_by_role('button',name='展开',exact=True)).to_have_attribute('aria-expanded','false')
                cards.get_by_role('button',name='展开',exact=True).click()
                expect(cards.locator('.node-resource-panel').filter(has_text='本机证书')).to_be_visible()
                check('standalone_certificate_has_collapsed_node_card',True)
                context.close()
                fleet.browser_promote(browser,node,'Master')
                context,page=fleet.admin_page(browser,node)
                compact_nodes(page,report,check)
                page.locator('#nodesPanel').screenshot(path=str(root/'nodes-compact.png'))
                context.close()
                fleet.browser_pair(browser,node,peer)
                context,page=fleet.admin_page(browser,node)
                try:retirement_nodes(page,node,peer,root,report,check)
                finally:
                    report['retirement_dialog_status']=page.locator('.node-retirement-status').all_text_contents()
                    context.close()
            finally:browser.close()
        limits=json.loads(command('docker','inspect',node.container('web')))[0]['HostConfig']
        check('one_cpu_512mib_web',limits['NanoCpus']==1000000000 and limits['Memory']==512*1024**2)
        samples=[];deadline=time.monotonic()+30;low=0
        while time.monotonic()<deadline:
            value=float(command('docker','stats','--no-stream','--format','{{.CPUPerc}}',node.container('web')).removesuffix('%'))
            samples.append(value);low=low+1 if value<20 else 0
            if low>=5:break
            time.sleep(1)
        check('one_core_quiescence',low>=5)
        report.update(cpu_samples=samples,web_limits=dict(nano_cpus=limits['NanoCpus'],memory_bytes=limits['Memory']))
        limits=json.loads(command('docker','inspect',peer.container('web')))[0]['HostConfig']
        check('direct_one_cpu_512mib_web',limits['NanoCpus']==1000000000 and limits['Memory']==512*1024**2)
        samples=[];deadline=time.monotonic()+30;low=0
        while time.monotonic()<deadline:
            value=float(command('docker','stats','--no-stream','--format','{{.CPUPerc}}',peer.container('web')).removesuffix('%'))
            samples.append(value);low=low+1 if value<20 else 0
            if low>=5:break
            time.sleep(1)
        check('direct_one_core_quiescence',low>=5)
        report['direct_cpu_samples']=samples
        report['passed']=True
    finally:
        business_failed=sys.exc_info()[0] is not None
        socket.getaddrinfo=fleet._original_getaddrinfo
        cleanup=[]
        for owned in (peer,node):
            if owned is None:continue
            try:owned.compose('stop','--timeout','10','web');cleanup.append(dict(project=owned.project,step='stop-web',stopped=True))
            except Exception as error:cleanup.append(dict(project=owned.project,step='stop-web',stopped=False,error=type(error).__name__))
            try:owned.stop();cleanup.append(dict(project=owned.project,step='down',stopped=True))
            except Exception as error:cleanup.append(dict(project=owned.project,step='down',stopped=False,error=type(error).__name__))
        if created:
            try:command('docker','network','rm',network);cleanup.append(dict(network=network,stopped=True))
            except Exception as error:cleanup.append(dict(network=network,stopped=False,error=type(error).__name__))
        finalize_cleanup(report,cleanup,root/'result.json',business_failed=business_failed)


if __name__=='__main__':
    signal.signal(signal.SIGTERM,interrupt_fixture)
    try:main()
    except Exception as error:
        # Browser call logs can contain test-session cookies. Keep only frame
        # locations and exception type, never the raw Playwright exception.
        print(json.dumps(dict(error_type=type(error).__name__,frames=[dict(file=Path(item.filename).name,
            line=item.lineno,function=item.name) for item in traceback.extract_tb(error.__traceback__)][-5:])),flush=True)
        raise SystemExit(1) from None
