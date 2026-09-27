'use strict';

const uploadSiteLabels = {
    primary: '主站',
    direct: '直连站点',
    relay: '中继站点',
};

function ensureUploadSiteTypeSelector() {
    let selector = document.getElementById('uploadSiteType');
    if (selector) return selector;
    selector = document.createElement('select');
    selector.id = 'uploadSiteType';
    selector.className = 'upload-site-type';
    selector.setAttribute('aria-label', '上传站点类型');
    selector.innerHTML = '<option value="" selected>请选择上传站点类型</option>';
    const legacy = document.getElementById('uploadStorageMember');
    if (legacy?.parentNode) {
        legacy.hidden = true;
        legacy.setAttribute('aria-hidden', 'true');
        legacy.setAttribute('tabindex', '-1');
        legacy.parentNode.insertBefore(selector, legacy);
    }
    return selector;
}

const uploadSiteType = ensureUploadSiteTypeSelector();
const mediaSiteTypes = new Map();

function siteTypeFromItem(item) {
    if (item?.site_type && uploadSiteLabels[item.site_type]) return item.site_type;
    if (item?.transport === 'Direct') return 'direct';
    if (item?.transport === 'Relay') return 'relay';
    return 'primary';
}

function readyMember(member) {
    return Boolean(member?.storage_enabled && member?.health === 'online' && member?.writable);
}

function countReadySiteTypes(pool) {
    if (pool?.standalone) return {primary: 1, direct: 0, relay: 0};
    const counts = {primary: 0, direct: 0, relay: 0};
    for (const member of pool?.members || []) {
        if (!readyMember(member) || member.member_kind === 'Auto') continue;
        if (member.member_kind === 'MasterLocal') counts.primary += 1;
        else if (member.transport === 'Direct') counts.direct += 1;
        else if (member.transport === 'Relay') counts.relay += 1;
    }
    return counts;
}

function updateMediaUploadGate() {
    const blocked = uploadRunning || !uploadSiteType?.value;
    $('uploadFiles').disabled = blocked;
    $('uploadFolder').disabled = blocked;
    if (uploadSiteType) uploadSiteType.disabled = uploadRunning;
}

async function refreshUploadSiteTypes() {
    const pool = await api('/api/v1/media/admin/storage-pool', {cache: 'no-store'});
    clusterUpload = !pool.standalone;
    const previous = uploadSiteType.value;
    const counts = countReadySiteTypes(pool);
    uploadSiteType.replaceChildren();

    const empty = document.createElement('option');
    empty.value = '';
    empty.textContent = '请选择上传站点类型';
    uploadSiteType.append(empty);

    for (const value of ['primary', 'direct', 'relay']) {
        const option = document.createElement('option');
        option.value = value;
        option.disabled = counts[value] <= 0;
        option.textContent = counts[value] > 0
            ? `${uploadSiteLabels[value]} · ${counts[value]} ready`
            : `${uploadSiteLabels[value]} · 暂无 ready 站点`;
        uploadSiteType.append(option);
    }
    const previousOption = [...uploadSiteType.options]
        .find(option => option.value === previous && !option.disabled);
    uploadSiteType.value = previousOption ? previous : '';
    updateMediaUploadGate();
    return pool;
}

const originalSetUploadControlsDisabled = setUploadControlsDisabled;
setUploadControlsDisabled = function setUploadControlsDisabledWithSiteType(disabled) {
    originalSetUploadControlsDisabled(disabled);
    updateMediaUploadGate();
};

uploadSiteType.onchange = updateMediaUploadGate;
refreshStoragePool = refreshUploadSiteTypes;

const originalApi = api;
api = async function apiWithPlacementObservation(url, options = {}) {
    const data = await originalApi(url, options);
    if (String(url).startsWith('/api/v1/media/admin/tree') && Array.isArray(data?.items)) {
        mediaSiteTypes.clear();
        for (const item of data.items) {
            const root = String(item.path || '').split('/', 1)[0];
            if (item.kind === 'file' && (root === 'music' || root === 'vido')) {
                mediaSiteTypes.set(item.path, siteTypeFromItem(item));
            }
        }
    }
    return data;
};

function decorateMediaSiteBadges() {
    for (const row of document.querySelectorAll('.tree-row[data-path]')) {
        row.querySelector('.media-site-badge')?.remove();
        const siteType = mediaSiteTypes.get(row.dataset.path);
        if (!siteType) continue;
        const badge = document.createElement('span');
        badge.className = `media-site-badge site-${siteType}`;
        badge.textContent = uploadSiteLabels[siteType];
        badge.title = `实际归属：${uploadSiteLabels[siteType]}`;
        row.append(badge);
    }
}

const originalRenderTree = renderTree;
renderTree = async function renderTreeWithPlacementObservation() {
    await originalRenderTree();
    decorateMediaSiteBadges();
};

async function cancelClusterReservation(reservation) {
    if (!reservation?.upload_id) return;
    await api(`/api/v1/media/admin/upload/session/${reservation.upload_id}`, {
        method: 'DELETE', headers: requestHeaders(false),
    }).catch(() => {});
}

runUploadTask = async function runUploadTaskWithReservationCleanup(fileList, relativePaths = null, lyricUpload = false) {
    if (uploadRunning) return;
    const files = [...fileList];
    if (!files.length) return;
    if (files.length > uploadLimits.max_upload_task_files) {
        alert(`一次上传任务最多选择 ${uploadLimits.max_upload_task_files} 个文件`);
        return;
    }
    const selectedSiteType = uploadSiteType.value;
    if (!lyricUpload && !selectedSiteType) {
        alert('请先选择上传站点类型');
        updateMediaUploadGate();
        return;
    }
    if (!lyricUpload && !relativePaths && !currentPath) {
        alert('上传文件前请先进入 data/media/music 或 data/media/vido 下的分类目录');
        return;
    }

    setUploadControlsDisabled(true);
    $('uploadProgress').classList.remove('hidden');
    $('uploadResults').innerHTML = '';
    $('uploadTaskTitle').textContent = lyricUpload ? '歌词上传任务' : (relativePaths ? '文件夹上传任务' : '多文件上传任务');
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

            const fileLimit = lyricUpload ? uploadLimits.max_lyric_file_size : uploadLimits.max_upload_file_size;
            if (file.size > fileLimit) {
                failedCount += 1;
                completedUnits += fileUnits;
                addUploadResult(displayName, 'error', `超过 ${formatSize(fileLimit)} 限制`);
                setProgress('totalProgress', 'totalPercent', completedUnits / totalUnits * 100);
                continue;
            }

            let reservation = null;
            try {
                const progress = fraction => {
                    setProgress('currentProgress', 'currentPercent', fraction * 100);
                    setProgress(
                        'totalProgress',
                        'totalPercent',
                        (completedUnits + fileUnits * fraction) / totalUnits * 100,
                    );
                };
                let result;
                if (clusterUpload && !lyricUpload) {
                    reservation = await api('/api/v1/media/admin/upload/session', {
                        method: 'POST', headers: requestHeaders(), body: JSON.stringify({
                            site_type: selectedSiteType,
                            target_dir: currentPath,
                            relative_path: relativePaths ? relativePaths[index] : null,
                            filename: file.name,
                            size_bytes: file.size,
                        }),
                    });
                    try {
                        await uploadRaw(reservation.upload_url, file, progress, reservation.transport === 'Direct');
                        result = reservation.transport === 'Direct'
                            ? await api(`/api/v1/media/admin/upload/session/${reservation.upload_id}/finalize`, {
                                method: 'POST', headers: requestHeaders(), body: '{}',
                            })
                            : {path: reservation.path};
                    } catch (error) {
                        await cancelClusterReservation(reservation);
                        throw error;
                    }
                } else {
                    const formData = new FormData();
                    if (!lyricUpload) {
                        formData.append('target_dir', currentPath);
                        formData.append('site_type', selectedSiteType);
                    }
                    if (relativePaths) formData.append('relative_path', relativePaths[index]);
                    formData.append('file', file, file.name);
                    result = await uploadOne(formData, progress,
                        lyricUpload ? '/api/v1/media/admin/upload/lyric' : '/api/v1/media/admin/upload/item');
                }
                successCount += 1;
                const siteType = lyricUpload ? null : (reservation?.site_type || result?.site_type || selectedSiteType);
                const detail = siteType ? `${result.path} · ${uploadSiteLabels[siteType]}` : result.path;
                addUploadResult(displayName, 'ok', detail);
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
        if (lyricUpload) await loadLyricCatalog().catch(() => {});
    }
};

$('delete').onclick = () => {
    const paths = [...selected];
    showModal(
        '确认删除',
        `将删除选中的 ${paths.length} 个${selectionKind === 'directory' ? '目录及其全部内容' : '文件'}。此操作不可恢复。`,
        async () => {
            const result = await api('/api/v1/media/admin/delete', {
                method: 'POST', headers: requestHeaders(), body: JSON.stringify({paths}),
            });
            selected.clear();
            selectionKind = null;
            await renderTree();
            if (result.pending_delete?.length) {
                alert(`已有 ${result.deleted || 0} 个对象完成删除；仍有 ${result.pending_delete.length} 个对象等待存储节点确认删除。\n\n等待中的路径暂时不会允许同名重传，系统会自动重试。`);
            }
        },
    );
};

updateMediaUploadGate();
refreshUploadSiteTypes().catch(() => updateMediaUploadGate());
