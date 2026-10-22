"""Real HTTP Logo cache across two completely separate Chromium lifetimes.

The disposable persistent profile uses the fixture's normal NSS trust. This
checks public Logo payload reuse only; it says nothing about encrypted media
or the lifetime of the player's in-memory caches.
"""
import os
from urllib.parse import parse_qs, urlsplit

from tests import browser_ui_regression as ui
from tests import federation_stack as fleet


def _logo_url(url):
    value=urlsplit(url)
    return (value.path=='/api/v1/media/brand/logo/music'
        and bool(parse_qs(value.query).get('v')))


def _observe(context,url):
    page=context.new_page();session=context.new_cdp_session(page)
    session.send('Network.enable')
    responses=[];cached=set();finished={};wire_status={}
    def received(event):
        response=event.get('response') or {};target=str(response.get('url') or '')
        if _logo_url(target):
            responses.append(dict(request_id=str(event.get('requestId') or ''),url=target,
                status=int(response.get('status') or 0),from_disk_cache=bool(response.get('fromDiskCache',False)),
                from_service_worker=bool(response.get('fromServiceWorker',False))))
    def served(event):cached.add(str(event.get('requestId') or ''))
    def loaded(event):finished[str(event.get('requestId') or '')]=event.get('encodedDataLength',0)
    def extra(event):wire_status[str(event.get('requestId') or '')]=event.get('statusCode',0)
    session.on('Network.responseReceived',received)
    session.on('Network.requestServedFromCache',served)
    session.on('Network.loadingFinished',loaded)
    session.on('Network.responseReceivedExtraInfo',extra)
    page.goto(url,wait_until='load')
    ui.wait_image(page,'img[src*="/brand/logo/music"]')
    # Let CDP deliver loadingFinished/extra-info after the actual image decoded.
    page.wait_for_timeout(300)
    dom=page.locator('img[src*="/brand/logo/music"]').first.evaluate("""image=>({
        src:image.src,current_src:image.currentSrc,complete:image.complete,
        width:image.naturalWidth,height:image.naturalHeight})""")
    records=[{**row,'request_served_from_cache':row['request_id'] in cached,
        'wire_status':int(wire_status.get(row['request_id'],0)),
        'encoded_network_bytes':finished.get(row['request_id'],0)} for row in responses]
    return dict(navigation_url=url,logo_urls=[row['url'] for row in records],dom=dom,responses=records)


def exercise(browser,master,root,report,check):
    home=root/'browser-home';nss=home/'.pki/nssdb'
    if not nss.is_dir():raise AssertionError('Logo restart check requires the fixture NSS trust store')
    profile=home/'logo-cache-profile'
    if profile.exists():raise AssertionError('Logo cold profile must be new')
    profile.mkdir(mode=0o700)
    evidence=dict(scope='public-music-logo-only',persistent_profile='browser-home/logo-cache-profile',
        tls_errors_ignored=False,encrypted_media_cache_tested=False,network_logs_private=True,
        cold=None,restarted=None,first_browser_fully_closed=False,second_browser_fully_closed=False)
    report['logo_browser_restart']=evidence
    executable=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
    def launch(phase):
        # The default fleet browser is still alive: never overwrite its net-log.
        args=[value for value in fleet.browser_args([master]) if not value.startswith('--log-net-log=')]
        args.append('--log-net-log='+str(root/('logo-'+phase+'-network.json')))
        options=dict(args=args,env={**os.environ,'HOME':str(home)},viewport={'width':1280,'height':800})
        if executable:options['executable_path']=executable
        report['inputs'].append(dict(action='launch persistent Chromium',phase=phase,
            profile='browser-home/logo-cache-profile',same_profile=True,normal_nss_trust=True))
        return browser.browser_type.launch_persistent_context(str(profile),**options)
    for phase,path,key in (('cold','/api/v1/media/','cold'),('restarted','/api/v1/media/music','restarted')):
        context=launch(phase);process=context.browser
        try:
            startup=[page.url for page in context.pages]
            check('logo_'+phase+'_starts_without_restored_business_pages',all(
                value in ('about:blank','chrome://newtab/') for value in startup))
            target=master.endpoint+path
            report['inputs'].append(dict(action='navigate for Logo cache',phase=phase,url=target))
            evidence[key]=_observe(context,target)
            evidence[key]['startup_urls']=startup
        finally:
            context.close()
            closed=process is not None and not process.is_connected()
            evidence['first_browser_fully_closed' if phase=='cold' else 'second_browser_fully_closed']=closed
            check('logo_'+phase+'_browser_fully_closed',closed)
    cold=evidence['cold'];warm=evidence['restarted']
    check('logo_restart_both_lifetimes_render_actual_image',all(value['dom']['complete']
        and value['dom']['width']>0 and value['dom']['height']>0 for value in (cold,warm)))
    check('logo_restart_cold_profile_downloads_original_payload',any(
        row['status']==200 and not row['from_disk_cache'] and not row['request_served_from_cache']
        and not row['from_service_worker'] and row['encoded_network_bytes']>0 for row in cold['responses']))
    first=cold['logo_urls'][-1]
    check('logo_restart_uses_same_immutable_url',bool(warm['responses'])
        and all(row['url']==first for row in warm['responses']))
    check('logo_restart_reuses_payload_after_browser_exit',all(not row['from_service_worker']
        and (row['from_disk_cache'] or row['request_served_from_cache'] or row['status']==304 or row['wire_status']==304)
        for row in warm['responses']))
