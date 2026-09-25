(() => {
    const PANEL_ID = 'brandLogoPanel';
    const BASE = '/api/v1/media/admin/brand';
    let csrfCookieName = '__Host-admin-csrf';
    let state = null;

    function cookie(name) {
        return document.cookie.split('; ')
            .find(value => value.startsWith(`${name}=`))
            ?.split('=').slice(1).join('=') || '';
    }

    async function json(url, options = {}) {
        const response = await fetch(url, options);
        if (response.status === 401) {
            location.href = '/api/v1/media';
            throw new Error('特权模式已失效');
        }
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.detail || 'Logo 操作失败');
        return payload;
    }

    function writeHeaders() {
        return {'X-CSRF-Token': cookie(csrfCookieName)};
    }

    function humanSize(value) {
        if (value < 1024) return `${value} B`;
        if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
        return `${(value / 1024 / 1024).toFixed(2)} MiB`;
    }

    function installStyle() {
        const style = document.createElement('style');
        style.textContent = `
            #${PANEL_ID} .brand-logo-grid {
                display:grid;
                grid-template-columns:repeat(3,minmax(0,1fr));
                gap:14px;
            }
            #${PANEL_ID} .brand-logo-card {
                min-width:0;
                padding:14px;
                border:1px solid rgba(148,163,184,.22);
                border-radius:14px;
                background:rgba(15,23,42,.42);
            }
            #${PANEL_ID} .brand-logo-preview {
                display:grid;
                place-items:center;
                min-height:120px;
                margin-bottom:10px;
                overflow:hidden;
                border-radius:10px;
                background:#07101f;
            }
            #${PANEL_ID} .brand-logo-preview img {
                display:block;
                width:100%;
                height:auto;
                max-height:180px;
                object-fit:contain;
            }
            #${PANEL_ID} .brand-logo-meta {
                display:flex;
                justify-content:space-between;
                gap:8px;
                margin:8px 0 12px;
                font-size:12px;
                opacity:.76;
            }
            #${PANEL_ID} .brand-logo-actions {
                display:flex;
                flex-wrap:wrap;
                gap:8px;
            }
            #${PANEL_ID} .brand-logo-actions button { flex:1 1 auto; }
            #${PANEL_ID} .brand-logo-status { margin-top:12px; min-height:1.4em; }
            @media(max-width:800px) {
                #${PANEL_ID} .brand-logo-grid { grid-template-columns:1fr; }
            }
        `;
        document.head.append(style);
    }

    function card(item) {
        const box = document.createElement('article');
        box.className = 'brand-logo-card';
        const preview = document.createElement('div');
        preview.className = 'brand-logo-preview';
        const image = document.createElement('img');
        image.alt = item.label;
        image.src = `${item.url}?v=${Date.now()}`;
        preview.append(image);

        const title = document.createElement('strong');
        title.textContent = item.label;
        const meta = document.createElement('div');
        meta.className = 'brand-logo-meta';
        const source = document.createElement('span');
        source.textContent = item.custom ? '自定义' : '内置默认';
        const detail = document.createElement('span');
        detail.textContent = `${String(item.format).toUpperCase()} · ${humanSize(item.size_bytes)}`;
        meta.append(source, detail);

        const actions = document.createElement('div');
        actions.className = 'brand-logo-actions';
        const input = document.createElement('input');
        input.type = 'file';
        input.accept = 'image/png,image/webp,.png,.webp';
        input.hidden = true;

        const upload = document.createElement('button');
        upload.type = 'button';
        upload.textContent = item.custom ? '替换' : '上传';
        upload.onclick = () => input.click();
        input.onchange = async () => {
            const file = input.files?.[0];
            if (!file) return;
            const body = new FormData();
            body.append('file', file);
            status(`正在上传 ${item.label}…`);
            try {
                await json(`/api/v1/media/admin/upload/brand/${item.kind}`, {
                    method: 'POST',
                    headers: writeHeaders(),
                    body,
                });
                await refresh();
                status(`${item.label} 已更新`);
            } catch (error) {
                status(error.message);
            } finally {
                input.value = '';
            }
        };

        const download = document.createElement('button');
        download.type = 'button';
        download.textContent = '下载';
        download.onclick = () => location.assign(`${BASE}/${item.kind}/download`);

        const remove = document.createElement('button');
        remove.type = 'button';
        remove.textContent = '删除自定义';
        remove.disabled = !item.custom;
        remove.onclick = async () => {
            if (!confirm(`删除 ${item.label} 自定义 Logo，并恢复内置默认图？`)) return;
            status(`正在恢复 ${item.label} 默认图…`);
            try {
                await json(`${BASE}/${item.kind}`, {
                    method: 'DELETE',
                    headers: writeHeaders(),
                });
                await refresh();
                status(`${item.label} 已恢复内置默认图`);
            } catch (error) {
                status(error.message);
            }
        };

        actions.append(upload, download, remove, input);
        box.append(preview, title, meta, actions);
        return box;
    }

    function status(text) {
        const value = document.getElementById('brandLogoStatus');
        if (value) value.textContent = text;
    }

    async function refresh() {
        state = await json(BASE);
        const grid = document.getElementById('brandLogoGrid');
        if (!grid) return;
        grid.replaceChildren(...state.items.map(card));
    }

    async function init() {
        if (document.getElementById(PANEL_ID)) return;
        try {
            const admin = await json('/api/v1/media/admin/status');
            csrfCookieName = admin.csrf_cookie_name || csrfCookieName;
        } catch (_error) {
            return;
        }
        installStyle();
        const panel = document.createElement('section');
        panel.id = PANEL_ID;
        panel.className = 'admin-module brand-logo-panel';
        panel.dataset.adminModule = 'brand';
        panel.innerHTML = `
            <button class="module-heading" type="button" aria-expanded="false">
                <span><strong>品牌 Logo</strong><small>前沿娱乐 / 前沿媒体 / 前沿音乐；上传覆盖自定义，删除后恢复内置默认</small></span><b>＋</b>
            </button>
            <div class="module-content">
                <div id="brandLogoGrid" class="brand-logo-grid"></div>
                <div id="brandLogoStatus" class="brand-logo-status">尚未加载</div>
            </div>
        `;
        const mediaPanel = document.querySelector('[data-admin-module="media"]');
        (mediaPanel?.parentElement || document.querySelector('.admin-console'))?.insertBefore(
            panel, mediaPanel || null,
        );
        panel.querySelector('.module-heading').onclick = async () => {
            const open = !panel.classList.contains('expanded');
            for (const module of document.querySelectorAll('.admin-module')) {
                module.classList.toggle('expanded', module === panel && open);
                const heading = module.querySelector('.module-heading');
                if (!heading) continue;
                heading.setAttribute('aria-expanded', String(module === panel && open));
                const marker = heading.querySelector('b');
                if (marker) marker.textContent = module === panel && open ? '−' : '＋';
            }
            if (open) {
                status('正在加载…');
                try {
                    await refresh();
                    status('当前生效 Logo');
                } catch (error) {
                    status(error.message);
                }
            }
        };
    }

    init();
})();
