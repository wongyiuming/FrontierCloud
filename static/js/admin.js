let selected = new Set();
let selectionKind = null;
let currentPath = '';
let csrfCookieName = '__Host-admin-csrf';
let uploadRunning = false;
let securityTimer = null;
let securityLoading = false;
let securityPage = 1;
let securityPages = 1;
let uploadLimits = {
    max_upload_file_size: 800 * 1024 * 1024,
    max_upload_task_files: 5000,
};

const $ = id => document.getElementById(id);

function expandAdminModule(target) {
    const shouldExpand = !target.classList.contains('expanded');
    for (const module of document.querySelectorAll('.admin-module')) {
        const expanded = module === target && shouldExpand;
        module.classList.toggle('expanded', expanded);
        const heading = module.querySelector?.('.module-heading');
        if (heading) {
            heading.setAttribute('aria-expanded', String(expanded));
            const indicator = heading.querySelector('b');
            if (indicator) indicator.textContent = expanded ? '−' : '＋';
        }
    }
}

for (const module of document.querySelectorAll('.admin-module')) {
    const heading = module.querySelector('.module-heading');
    if (heading) heading.onclick = () => expandAdminModule(module);
}

function getCookie(name) {
    return document.cookie
        .split('; ')
        .find(value => value.startsWith(`${name}=`))
        ?.split('=')
        .slice(1)
        .join('=') || '';
}

function csrf() {
    return getCookie(csrfCookieName);
}

function requestHeaders(json = true) {
    const result = {'X-CSRF-Token': csrf()};
    if (json) result['Content-Type'] = 'application/json';
    return result;
}

function formatErrorDetail(detail) {
    if (!detail) return '操作失败';
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
        return detail.map(item => {
            if (typeof item === 'string') return item;
            const location = Array.isArray(item.loc) ? item.loc.join('.') : '';
            return `${location ? `${location}: ` : ''}${item.msg || JSON.stringify(item)}`;
        }).join('；');
    }
    if (typeof detail === 'object') return detail.message || JSON.stringify(detail);
    return String(detail);
}

async function api(url, options = {}) {
    const response = await fetch(url, options);
    if (response.status === 401) {
        if (securityTimer) clearInterval(securityTimer);
        alert('特权模式已失效，请重新提权');
        location.href = '/api/v1/media';
        throw new Error('特权模式已失效');
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(formatErrorDetail(data.detail));
    return data;
}

function escapeHtml(value) {
    return String(value).replace(/[&<>'"]/g, character => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
    }[character]));
}

function formatSize(value) {
    if (value == null) return '';
    const units = ['B', 'KB', 'MB', 'GB'];
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
        value /= 1024;
        index += 1;
    }
    return `${value.toFixed(index ? 1 : 0)} ${units[index]}`;
}

function showModal(title, body, onConfirm) {
    $('modalTitle').textContent = title;
    $('modalBody').textContent = body;
    $('modal').classList.remove('hidden');
    $('modalCancel').onclick = () => $('modal').classList.add('hidden');
    $('modalOk').onclick = async () => {
        try {
            await onConfirm();
            $('modal').classList.add('hidden');
        } catch (error) {
            alert(error.message);
        }
    };
}

function updateToolbar() {
    const count = selected.size;
    $('selection').textContent = count
        ? `已选择 ${count} 个${selectionKind === 'directory' ? '目录' : '文件'}`
        : '未选择';
    $('download').disabled = !count;
    $('delete').disabled = !count;
    $('hide').disabled = !(count && selectionKind === 'directory');
    if (count && selectionKind === 'directory') {
        const rows = [...document.querySelectorAll('.tree-row.selected')];
        $('hide').textContent = rows.every(row => row.dataset.hidden === 'true') ? '恢复' : '隐藏';
    } else {
        $('hide').textContent = '隐藏';
    }
}

function toggleSelection(item) {
    if (selectionKind && selectionKind !== item.kind) {
        selected.clear();
        selectionKind = null;
    }
    selectionKind = item.kind;
    if (selected.has(item.path)) selected.delete(item.path);
    else selected.add(item.path);
    if (!selected.size) selectionKind = null;
    for (const row of document.querySelectorAll('.tree-row[data-path]')) {
        row.classList.toggle('selected', selected.has(row.dataset.path));
    }
    updateToolbar();
}

async function renderTree() {
    const data = await api(`/api/v1/media/admin/tree?path=${encodeURIComponent(currentPath)}`);
    $('pathbar').textContent = `/${currentPath}`;
    const tree = $('tree');
    tree.innerHTML = '';

    if (currentPath) {
        const up = document.createElement('div');
        up.className = 'tree-row';
        up.innerHTML = '<span class="kind">↩</span><span class="name">返回上级</span>';
        up.onclick = () => {
            currentPath = currentPath.split('/').slice(0, -1).join('/');
            selected.clear();
            selectionKind = null;
            renderTree();
        };
        tree.appendChild(up);
    }

    for (const item of data.items) {
        const row = document.createElement('div');
        row.className = `tree-row${selected.has(item.path) ? ' selected' : ''}${item.hidden ? ' hidden-item' : ''}`;
        row.dataset.path = item.path;
        row.dataset.hidden = String(item.hidden);
        row.innerHTML = `<span class="kind">${item.kind === 'directory' ? '📁' : '📄'}</span>`
            + `<span class="name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>`
            + `<small>${item.kind === 'file' ? formatSize(item.size) : ''}</small>`;
        row.onclick = event => {
            event.stopPropagation();
            toggleSelection(item);
        };
        row.ondblclick = event => {
            event.stopPropagation();
            if (item.kind === 'directory') {
                currentPath = item.path;
                selected.clear();
                selectionKind = null;
                renderTree();
            }
        };
        tree.appendChild(row);
    }
    updateToolbar();
}

function setUploadControlsDisabled(disabled) {
    uploadRunning = disabled;
    $('uploadBtn').disabled = disabled;
    $('uploadFiles').disabled = disabled;
    $('uploadFolder').disabled = disabled;
    $('fileInput').disabled = disabled;
    $('folderInput').disabled = disabled;
}

function setProgress(elementId, percentId, value) {
    const bounded = Math.max(0, Math.min(100, value));
    $(elementId).value = bounded;
    $(percentId).textContent = `${Math.round(bounded)}%`;
}

function addUploadResult(name, status, message) {
    const row = document.createElement('div');
    row.className = `upload-result ${status}`;
    row.textContent = `${status === 'ok' ? '✓' : '✗'} ${name}${message ? ` — ${message}` : ''}`;
    $('uploadResults').appendChild(row);
    $('uploadResults').scrollTop = $('uploadResults').scrollHeight;
}

function parseXhrData(xhr) {
    if (xhr.response && typeof xhr.response === 'object') return xhr.response;
    try {
        return JSON.parse(xhr.responseText || '{}');
    } catch (_error) {
        return {};
    }
}

function uploadOne(formData, onProgress) {
    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/v1/media/admin/upload/item');
        xhr.responseType = 'json';
        xhr.setRequestHeader('X-CSRF-Token', csrf());
        xhr.upload.onprogress = event => {
            if (event.lengthComputable) onProgress(event.loaded / event.total);
        };
        xhr.onerror = () => reject(new Error('网络连接中断'));
        xhr.onabort = () => reject(new Error('上传已取消'));
        xhr.onload = () => {
            const data = parseXhrData(xhr);
            if (xhr.status === 401) {
                reject(new Error('特权模式已失效，请重新提权'));
                location.href = '/api/v1/media';
                return;
            }
            if (xhr.status < 200 || xhr.status >= 300) {
                reject(new Error(formatErrorDetail(data.detail)));
                return;
            }
            resolve(data);
        };
        xhr.send(formData);
    });
}

async function runUploadTask(fileList, relativePaths = null) {
    if (uploadRunning) return;
    const files = [...fileList];
    if (!files.length) return;
    if (files.length > uploadLimits.max_upload_task_files) {
        alert(`一次上传任务最多选择 ${uploadLimits.max_upload_task_files} 个文件`);
        return;
    }
    if (!relativePaths && !currentPath) {
        alert('上传文件前请先进入 data/media/music 或 data/media/vido 下的分类目录');
        return;
    }

    setUploadControlsDisabled(true);
    $('uploadProgress').classList.remove('hidden');
    $('uploadResults').innerHTML = '';
    $('uploadTaskTitle').textContent = relativePaths ? '文件夹上传任务' : '多文件上传任务';
    setProgress('currentProgress', 'currentPercent', 0);
    setProgress('totalProgress', 'totalPercent', 0);

    const totalUnits = files.reduce((sum, file) => sum + Math.max(file.size, 1), 0);
    let completedUnits = 0;
    let successCount = 0;
    let failedCount = 0;

    try {
        for (let index = 0; index < files.length; index += 1) {
            const file = files[index];
            const displayName = relativePaths ? relativePaths[index] : file.name;
            const fileUnits = Math.max(file.size, 1);
            $('currentFileLabel').textContent = `当前：${displayName}`;
            $('totalTaskLabel').textContent = `任务总进度 ${index + 1}/${files.length}`;
            $('uploadSummary').textContent = `成功 ${successCount}，失败 ${failedCount}`;
            setProgress('currentProgress', 'currentPercent', 0);

            if (file.size > uploadLimits.max_upload_file_size) {
                failedCount += 1;
                completedUnits += fileUnits;
                addUploadResult(displayName, 'error', `超过 ${formatSize(uploadLimits.max_upload_file_size)} 限制`);
                setProgress('totalProgress', 'totalPercent', completedUnits / totalUnits * 100);
                continue;
            }

            const formData = new FormData();
            formData.append('target_dir', currentPath);
            if (relativePaths) formData.append('relative_path', relativePaths[index]);
            formData.append('file', file, file.name);

            try {
                const result = await uploadOne(formData, fraction => {
                    setProgress('currentProgress', 'currentPercent', fraction * 100);
                    setProgress(
                        'totalProgress',
                        'totalPercent',
                        (completedUnits + fileUnits * fraction) / totalUnits * 100,
                    );
                });
                successCount += 1;
                addUploadResult(displayName, 'ok', result.path);
            } catch (error) {
                failedCount += 1;
                addUploadResult(displayName, 'error', error.message);
            }

            completedUnits += fileUnits;
            setProgress('currentProgress', 'currentPercent', 100);
            setProgress('totalProgress', 'totalPercent', completedUnits / totalUnits * 100);
        }
    } finally {
        setUploadControlsDisabled(false);
        $('uploadSummary').textContent = `完成：成功 ${successCount}，失败 ${failedCount}`;
        $('currentFileLabel').textContent = '当前文件处理完成';
        await renderTree().catch(() => {});
    }
}

function securityDate(value) {
    if (!value) return '-';
    const normalized = /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`;
    const date = new Date(normalized);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function securityButton(label, className, handler) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = label;
    if (className) button.className = className;
    button.onclick = async () => {
        button.disabled = true;
        try {
            await handler();
            await loadSecurityStatus(true);
        } catch (error) {
            alert(error.message);
        } finally {
            button.disabled = false;
        }
    };
    return button;
}

function renderSecurityList(data) {
    $('legalApiCount').textContent = String(data.legal_api_count ?? 0);
    $('activeBanCount').textContent = String(data.active_ban_count ?? 0);
    $('whitelistCount').textContent = String(data.whitelist?.length ?? 0);
    $('securitySummary').textContent = `首次超过阈值封禁 24 小时；第二次触犯永久封禁`;

    const banList = $('banList');
    banList.innerHTML = '';
    if (!data.events?.length) {
        const empty = document.createElement('div');
        empty.className = 'security-empty';
        empty.textContent = '当前查询没有封禁审计记录';
        banList.appendChild(empty);
    }
    for (const event of data.events || []) {
        const row = document.createElement('div');
        row.className = `security-row${event.active ? ' active' : ''}${event.whitelisted ? ' whitelisted' : ''}`;
        const main = document.createElement('div');
        main.className = 'security-row-main';
        const ip = document.createElement('div');
        ip.className = 'security-ip';
        ip.textContent = event.ip;
        const meta = document.createElement('div');
        meta.className = 'security-meta';
        const expiry = event.ban_kind === 'permanent' ? '永久' : securityDate(event.expires_at);
        meta.textContent = `${event.status} · ${event.ban_kind || 'auto'} · ${event.trigger_count} 次 · ${securityDate(event.banned_at)} → ${expiry}`;
        const path = document.createElement('div');
        path.className = 'security-path';
        path.textContent = [event.reason, `${event.last_method || ''} ${event.last_path || ''}`.trim()].filter(Boolean).join(' · ');
        main.append(ip, meta, path);
        const actions = document.createElement('div');
        actions.className = 'security-actions';
        if (event.active) {
            actions.appendChild(securityButton('解封', 'danger', () => api('/api/v1/media/admin/security/unban', {
                method: 'POST', headers: requestHeaders(), body: JSON.stringify({ip: event.ip}),
            })));
        }
        if (!event.whitelisted) {
            actions.appendChild(securityButton('加白', 'allow', () => api('/api/v1/media/admin/security/whitelist', {
                method: 'POST', headers: requestHeaders(), body: JSON.stringify({ip: event.ip, note: 'Admin 封禁列表加白'}),
            })));
            if (!event.active) {
                actions.appendChild(securityButton('重新封禁', 'danger', async () => {
                    const reason = prompt('请输入重新封禁原因:');
                    if (!reason?.trim()) return;
                    await api('/api/v1/media/admin/security/reban', {
                        method: 'POST',
                        headers: requestHeaders(),
                        body: JSON.stringify({ip: event.ip, reason: reason.trim()}),
                    });
                }));
            }
            if (event.ban_kind !== 'permanent') {
                actions.appendChild(securityButton('永久拉黑', 'danger', async () => {
                    const reason = prompt(`请输入永久拉黑 ${event.ip} 的原因:`);
                    if (!reason?.trim()) return;
                    if (!confirm(`确认永久拉黑 ${event.ip}？该封禁不会自动到期。`)) return;
                    await api('/api/v1/media/admin/security/permanent-ban', {
                        method: 'POST',
                        headers: requestHeaders(),
                        body: JSON.stringify({ip: event.ip, reason: reason.trim()}),
                    });
                }));
            }
        }
        row.append(main, actions);
        banList.appendChild(row);
    }

    const whitelistList = $('whitelistList');
    whitelistList.innerHTML = '';
    if (!data.whitelist?.length) {
        const empty = document.createElement('div');
        empty.className = 'security-empty';
        empty.textContent = '永久白名单为空';
        whitelistList.appendChild(empty);
    }
    for (const entry of data.whitelist || []) {
        const row = document.createElement('div');
        row.className = 'security-row whitelisted';
        const main = document.createElement('div');
        main.className = 'security-row-main';
        const ip = document.createElement('div');
        ip.className = 'security-ip';
        ip.textContent = entry.ip;
        const meta = document.createElement('div');
        meta.className = 'security-meta';
        meta.textContent = `${securityDate(entry.created_at)}${entry.note ? ` · ${entry.note}` : ''}`;
        main.append(ip, meta);
        const actions = document.createElement('div');
        actions.className = 'security-actions';
        actions.appendChild(securityButton('移出', 'danger', () => api('/api/v1/media/admin/security/whitelist/remove', {
            method: 'POST', headers: requestHeaders(), body: JSON.stringify({ip: entry.ip}),
        })));
        row.append(main, actions);
        whitelistList.appendChild(row);
    }

    securityPage = data.pagination?.page || 1;
    securityPages = data.pagination?.pages || 1;
    $('securityPageInfo').textContent = `第 ${securityPage} / ${securityPages} 页，共 ${data.pagination?.total || 0} 条`;
    $('securityPrev').disabled = securityPage <= 1;
    $('securityNext').disabled = securityPage >= securityPages;
}

async function loadSecurityStatus(force = false) {
    if (securityLoading || (document.hidden && !force)) return;
    securityLoading = true;
    try {
        const params = new URLSearchParams({
            scope: $('securityScopeFilter').value,
            page: String(securityPage),
            page_size: '100',
        });
        const ip = $('securityIpFilter').value.trim();
        const status = $('securityStatusFilter').value;
        if (ip) params.set('ip', ip);
        if (status) params.set('status', status);
        renderSecurityList(await api(`/api/v1/media/admin/security/blocks?${params}`));
    } catch (error) {
        $('securitySummary').textContent = `加载失败：${error.message}`;
    } finally {
        securityLoading = false;
    }
}

$('securityRefresh').onclick = () => loadSecurityStatus(true);
$('securityFilterForm').onsubmit = event => {
    event.preventDefault();
    securityPage = 1;
    loadSecurityStatus(true);
};
$('securityPrev').onclick = () => {
    if (securityPage > 1) {
        securityPage -= 1;
        loadSecurityStatus(true);
    }
};
$('securityNext').onclick = () => {
    if (securityPage < securityPages) {
        securityPage += 1;
        loadSecurityStatus(true);
    }
};
$('whitelistForm').onsubmit = async event => {
    event.preventDefault();
    const ip = $('whitelistIp').value.trim();
    if (!ip) return;
    try {
        await api('/api/v1/media/admin/security/whitelist', {
            method: 'POST',
            headers: requestHeaders(),
            body: JSON.stringify({ip, note: $('whitelistNote').value.trim()}),
        });
        $('whitelistIp').value = '';
        $('whitelistNote').value = '';
        await loadSecurityStatus(true);
    } catch (error) {
        alert(error.message);
    }
};
$('permanentBanForm').onsubmit = async event => {
    event.preventDefault();
    const ip = $('permanentBanIp').value.trim();
    const reason = $('permanentBanReason').value.trim();
    if (!ip || !reason) return;
    if (!confirm(`确认永久拉黑 ${ip}？该封禁不会自动到期。`)) return;
    try {
        await api('/api/v1/media/admin/security/permanent-ban', {
            method: 'POST', headers: requestHeaders(), body: JSON.stringify({ip, reason}),
        });
        $('permanentBanForm').reset();
        await loadSecurityStatus(true);
    } catch (error) {
        alert(error.message);
    }
};

async function changeAdminKey(payload) {
    const data = await api('/api/v1/media/admin/key/rotate', {
        method: 'POST', headers: requestHeaders(), body: JSON.stringify(payload),
    });
    $('newKeyValue').textContent = data.admin_key;
    $('newKeyResult').classList.remove('hidden');
}

$('randomKey').onclick = async () => {
    if (!confirm('确认生成随机强 Key？其他已登录会话将立即失效。')) return;
    try { await changeAdminKey({mode: 'random'}); } catch (error) { alert(error.message); }
};
$('customKeyForm').onsubmit = async event => {
    event.preventDefault();
    const key = $('customKey').value;
    const confirmation = $('customKeyConfirm').value;
    if (key !== confirmation) { alert('两次输入的 Admin Key 不一致'); return; }
    if (!confirm('确认使用这个自定义 Key？其他已登录会话将立即失效。')) return;
    try {
        await changeAdminKey({mode: 'custom', key, confirmation});
        $('customKeyForm').reset();
    } catch (error) { alert(error.message); }
};
$('copyKey').onclick = async () => {
    await navigator.clipboard.writeText($('newKeyValue').textContent);
    $('copyKey').textContent = '已复制';
};

$('uploadFiles').onclick = () => $('fileInput').click();
$('uploadFolder').onclick = () => $('folderInput').click();
$('fileInput').onchange = async event => {
    await runUploadTask(event.target.files);
    event.target.value = '';
};
$('folderInput').onchange = async event => {
    const paths = [...event.target.files].map(file => file.webkitRelativePath || file.name);
    await runUploadTask(event.target.files, paths);
    event.target.value = '';
};

$('delete').onclick = () => {
    const paths = [...selected];
    showModal(
        '确认删除',
        `将删除选中的 ${paths.length} 个${selectionKind === 'directory' ? '目录及其全部内容' : '文件'}。此操作不可恢复。`,
        async () => {
            await api('/api/v1/media/admin/delete', {
                method: 'POST', headers: requestHeaders(), body: JSON.stringify({paths}),
            });
            selected.clear();
            selectionKind = null;
            await renderTree();
        },
    );
};

$('hide').onclick = () => {
    const paths = [...selected];
    const hidden = $('hide').textContent === '隐藏';
    showModal(
        hidden ? '确认隐藏' : '确认恢复',
        `${hidden ? '公共视图将隐藏' : '公共视图将恢复显示'}选中的 ${paths.length} 个目录。`,
        async () => {
            await api('/api/v1/media/admin/hide', {
                method: 'POST', headers: requestHeaders(), body: JSON.stringify({paths, hidden}),
            });
            selected.clear();
            selectionKind = null;
            await renderTree();
        },
    );
};

$('download').onclick = async () => {
    const paths = [...selected];
    const url = `/api/v1/media/admin/download?paths=${encodeURIComponent(JSON.stringify(paths))}`;
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = paths.length === 1
        ? paths[0].split('/').pop()
        : 'media-download.zip';
    anchor.click();
};

$('backPublic').onclick = () => { location.href = '/api/v1/media'; };
$('logout').onclick = async () => {
    try {
        await fetch('/api/v1/media/admin/logout', {method: 'POST', headers: requestHeaders(false)});
    } finally {
        location.href = '/api/v1/media';
    }
};

(async () => {
    try {
        const status = await api('/api/v1/media/admin/status');
        uploadLimits = {...uploadLimits, ...status.limits};
        csrfCookieName = status.csrf_cookie_name || csrfCookieName;
        await renderTree();
        await loadSecurityStatus(true);
        securityTimer = setInterval(loadSecurityStatus, 15000);
    } catch (_error) {
        // api() handles expired sessions and navigation.
    }
})();
