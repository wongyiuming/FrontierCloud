"""Real Chromium geometry checks for Admin module height/scroll coordination."""
import argparse
import json
import pathlib

from playwright.sync_api import sync_playwright


def fixture(root):
    css = '\n'.join((root / 'static/css' / name).read_text(encoding='utf-8')
                    for name in ('admin.css', 'admin-system-modules.css'))
    focus = (root / 'static/js/admin-focus.js').read_text(encoding='utf-8')
    names = ['media', 'priority', 'lyrics', 'users', 'security', 'network', 'nodes', 'site', 'key', 'brand']
    modules = ''.join(f'<section class="admin-module" data-admin-module="{name}">'
                      f'<button class="module-heading"><strong>{name}</strong><b>＋</b></button>'
                      '<div class="module-content">Content</div></section>' for name in names)
    toolbar = ('<div class="admin-mobile-toolbar"><button id="adminNavigationToggle">Modules</button></div>'
               if '.admin-mobile-toolbar' in css else '')
    return f'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
    <style>{css}</style></head><body>
    <aside id="adminNavigation" class="admin-navigation"><nav id="adminModuleLinks"></nav></aside>
    {toolbar}
    <main class="admin-console">{modules}</main><script>
    function expandAdminModule(target) {{
      const shouldExpand = !target.classList.contains('expanded');
      for (const module of document.querySelectorAll('.admin-module'))
        module.classList.toggle('expanded', module === target && shouldExpand);
    }}
    for (const module of document.querySelectorAll('.admin-module'))
      module.querySelector('.module-heading').onclick=()=>expandAdminModule(module);
    </script><script>{focus}</script></body></html>'''


def click(page, name):
    # Gin's real navigation does not pre-scroll the destination before clicking.
    # Legacy Python has no sidebar until parity completion; its heading has the
    # same expansion event and is exercised without Playwright auto-scrolling.
    button = page.locator(f'[data-module-target="{name}"]')
    if button.count():
        page.locator('#adminNavigation').evaluate("p=>p.classList.add('open')")
        button.click()
    else:
        page.locator(f'[data-admin-module="{name}"] .module-heading').evaluate('b=>b.click()')


def geometry(page, name):
    return page.evaluate('''name=>{
      const root=document.querySelector('.admin-console');
      const target=root.querySelector(`[data-admin-module="${name}"]`);
      const padding=parseFloat(getComputedStyle(root).scrollPaddingTop)||0;
      return {top:target.getBoundingClientRect().top, expected:root.getBoundingClientRect().top+root.clientTop+padding,
        scroll:root.scrollTop, body:document.scrollingElement.scrollTop};
    }''', name)


def verify_live_navigation(page):
    for name in ('media', 'key', 'priority', 'brand', 'nodes', 'media'):
        click(page, name)
        page.wait_for_timeout(700)
        actual = geometry(page, name)
        assert abs(actual['top'] - actual['expected']) <= 1.5, {'module': name, **actual}
        assert actual['body'] == 0


def check(root, output, expect_broken=False):
    results=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True, args=['--no-sandbox'])
        for size in ((1366,900),(390,844)):
            for motion in ('no-preference','reduce'):
                page=browser.new_page(viewport={'width':size[0],'height':size[1]},reduced_motion=motion)
                page.set_content(fixture(root))
                page.wait_for_timeout(50)
                for name in ('media','key','priority','brand','nodes','media'):
                    click(page,name);page.wait_for_timeout(700)
                    actual=geometry(page,name)
                    results.append({'viewport':size,'motion':motion,'module':name,**actual})
                    if not expect_broken:
                        assert abs(actual['top']-actual['expected']) <= 1.5, results[-1]
                        assert actual['body']==0, 'Module navigation must not scroll the body'
                if not expect_broken:
                    # Pending downward focus must never steal back a newer upward target.
                    click(page,'brand');page.wait_for_timeout(35);click(page,'priority');page.wait_for_timeout(700)
                    actual=geometry(page,'priority');assert abs(actual['top']-actual['expected'])<=1.5,actual
                    # An explicit manual scroll during expansion cancels queued focus.
                    click(page,'brand');page.wait_for_timeout(35)
                    page.locator('.admin-console').evaluate('root=>{root.dispatchEvent(new WheelEvent("wheel",{bubbles:true}));root.scrollTop=0;}')
                    page.wait_for_timeout(700);assert page.locator('.admin-console').evaluate('root=>root.scrollTop')==0
                page.close()
        browser.close()
    if expect_broken:
        assert any(abs(v['top']-v['expected'])>2 for v in results if v['motion']=='no-preference'), 'Baseline did not reproduce drift'
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps({'baseline':expect_broken,'results':results},indent=2))
    print(json.dumps({'baseline':expect_broken,'cases':len(results),'maximum_error':max(abs(v['top']-v['expected']) for v in results)}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=pathlib.Path,default=pathlib.Path(__file__).resolve().parents[1]);parser.add_argument('--output',type=pathlib.Path,required=True);parser.add_argument('--expect-broken',action='store_true')
    args=parser.parse_args();check(args.root,args.output,args.expect_broken)
