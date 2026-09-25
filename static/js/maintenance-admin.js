(() => {
    const PANEL_ID = 'siteAccessPanel';
    const ENDPOINT = '/api/v1/media/admin/site/maintenance';
    let timer = null;
    let lastState = null;
    const element = id => document.getElementById(id);

    function setExpanded(panel) {
        const open = !panel.classList.contains('expanded');
        for (const module of document.querySelectorAll('.admin-module')) {
            const selected = module === panel && open;
            module.classList.toggle('expanded', selected);
            const heading = module.querySelector('.module-heading');
            if (!heading) continue;
            heading.setAttribute('aria-expanded', String(selected));
            const marker = heading.querySelector('b');
            if (marker) marker.textContent = selected ? '−' : '＋';
        }
        return open;
    }

    function schedule(delay = 3000) {
        if (timer) clearTimeout(timer);
        const panel = element(PANEL_ID);
        if (!panel?.classList.contains('expanded')) {
            timer = null;
            return;
        }
        timer = setTimeout(() => refresh().catch(showError), delay);
    }

    function showError(error) {
        const detail = element('siteAccessDetail');
        if (detail) detail.textContent = error.message;
        schedule(5000);
    }

    function render(value) {
        lastState = value;
        const pill = element('siteAccessState');
        const maintenance = Boolean(value.maintenance);
        pill.textContent = maintenance ? '维护中' : '正常开放';
        pill.className = `site-state-pill ${maintenance ? 'maintenance' : 'open'}`;
        element('siteAccessDetail').textContent = value.detail || (maintenance ? '站点维护中' : '站点正常开放');
        element('siteAccessSource').textContent = value.source || '-';
        const release = value.release || {};
        element('siteAccessRelease').textContent = `${release.state || 'unknown'} / ${release.phase || '-'}${release.target_sha ? ` · ${String(release.target_sha).slice(0, 12)}` : ''}`;
        element('siteAccessManual').textContent = value.manual ? '已启用' : '未启用';
        element('siteEnterMaintenance').disabled = maintenance && value.manual;
        element('siteEndMaintenance').disabled = !maintenance;
        element('siteAccessSummary').textContent = maintenance
            ? `当前站点维护中 · ${value.detail || ''}`
            : '当前站点正常开放';
        schedule();
    }

    async function refresh() {
        const value = await api(ENDPOINT);
        render(value);
        return value;
    }

    async function setMaintenance(enabled) {
        const wording = enabled ? '进入维护' : '结束维护';
        if (!confirm(`确认${wording}？`)) return;
        element('siteAccessDetail').textContent = `${wording}处理中…`;
        try {
            const value = await api(ENDPOINT, {
                method: 'POST', headers: requestHeaders(), body: JSON.stringify({enabled}),
            });
            render(value);
        } catch (error) {
            showError(error);
        }
    }

    function init() {
        if (element(PANEL_ID)) return;
        const panel = document.createElement('section');
        panel.id = PANEL_ID;
        panel.className = 'admin-module site-access-panel';
        panel.dataset.adminModule = 'site';
        panel.innerHTML = `
            <button class="module-heading" type="button" aria-expanded="false">
                <span><strong>站点开放状态</strong><small id="siteAccessSummary">观察当前站点开放 / 维护状态，并可手动切换</small></span><b>＋</b>
            </button>
            <div class="system-module-content">
                <div class="system-toolbar">
                    <span id="siteAccessState" class="site-state-pill">正在加载</span>
                    <span class="spacer"></span>
                    <button id="siteAccessRefresh" type="button">刷新状态</button>
                </div>
                <div class="site-access-overview">
                    <div class="site-access-stat"><small>状态来源</small><strong id="siteAccessSource">-</strong></div>
                    <div class="site-access-stat"><small>发布任务</small><strong id="siteAccessRelease">-</strong></div>
                    <div class="site-access-stat"><small>手动维护</small><strong id="siteAccessManual">-</strong></div>
                </div>
                <div id="siteAccessDetail" class="site-access-note">尚未加载</div>
                <div class="site-access-actions">
                    <button id="siteEnterMaintenance" class="danger" type="button">进入维护</button>
                    <button id="siteEndMaintenance" type="button">结束维护</button>
                </div>
            </div>
        `;
        const release = element('systemVersionPanel');
        const nodes = element('nodesPanel');
        const anchor = release || nodes;
        (anchor?.parentElement || document.querySelector('.admin-console'))?.insertBefore(panel, anchor || null);
        panel.querySelector('.module-heading').onclick = () => {
            const open = setExpanded(panel);
            if (open) refresh().catch(showError);
            else schedule(0);
        };
        element('siteAccessRefresh').onclick = () => refresh().catch(showError);
        element('siteEnterMaintenance').onclick = () => setMaintenance(true);
        element('siteEndMaintenance').onclick = () => setMaintenance(false);
    }

    init();
})();
