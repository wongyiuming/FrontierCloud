"""Actual Admin node-card interactions; no API or database response replacement."""
import hashlib
from pathlib import Path
import re

from playwright.sync_api import expect


def run(page,report,check):
    expect(page.locator('#nodesPanel .module-heading strong')).to_have_text('节点管理')
    pairing=page.locator('.node-pairing-details')
    check('pairing_initialization_collapsed_by_default',not pairing.evaluate('(element)=>element.open'))
    cards=page.locator('#nodeRelationships .node-card')
    expect(cards).not_to_have_count(0)
    check('nodes_default_compact_and_lazy',cards.count()==page.locator('.node-details-toggle[aria-expanded="false"]').count()
        and page.locator('.node-card-details .node-resource-panel').count()==0)
    local=cards.filter(has_text='Master Local')
    expect(local).to_have_count(1)
    local.get_by_role('button',name='展开',exact=True).click()
    expect(local.locator('.node-card-details')).to_be_visible()
    expect(local.locator('.node-resource-panel').filter(has_text='本机证书')).to_be_visible()
    check('local_certificate_inside_its_node_card',page.locator('#nodeRelationships > tr > td > .node-resource-panel').count()==0)
    page.locator('#nodesRefresh').click()
    expect(page.locator('#nodeOperationStatus')).to_have_text('已刷新')
    expect(local.get_by_role('button',name='收起',exact=True)).to_have_attribute('aria-expanded','true')
    check('node_expansion_survives_refresh',True)
    toggle=local.get_by_role('button',name='收起',exact=True)
    toggle.focus();toggle.press('Enter')
    expect(local.locator('.node-card-details')).to_be_hidden()
    page.locator('#nodesRefresh').click()
    expect(page.locator('#nodeOperationStatus')).to_have_text('已刷新')
    expect(local.get_by_role('button',name='展开',exact=True)).to_have_attribute('aria-expanded','false')
    check('keyboard_collapse_survives_refresh',True)
    href=page.locator('link[href*="/static/css/nodes-observability.css"]').get_attribute('href')
    response=page.evaluate("async href=>{const response=await fetch(href,{cache:'reload'});return {ok:response.ok,cache:response.headers.get('cache-control')||''};}",href)
    digest=hashlib.sha256((Path(__file__).resolve().parents[1]/'static/css/nodes-observability.css').read_bytes()).hexdigest()[:16]
    check('nodes_styles_are_content_hashed_and_immutable',re.search(r'\?v='+digest+r'$',href) is not None
        and response['ok'] and 'immutable' in response['cache'])
    report['node_card_ui']=dict(default_collapsed=True,lazy_details=True,refresh_preserves_choice=True,
        local_certificate_inside_card=True,stylesheet_sha256_prefix=digest,keyboard_toggle=True)
