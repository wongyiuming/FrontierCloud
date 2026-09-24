'use strict';

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
                            storage_member_id: $('uploadStorageMember').value,
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
                    if (!lyricUpload) formData.append('target_dir', currentPath);
                    if (!lyricUpload && relativePaths) formData.append('relative_path', relativePaths[index]);
                    formData.append('file', file, file.name);
                    result = await uploadOne(formData, progress,
                        lyricUpload ? '/api/v1/media/admin/upload/lyric' : '/api/v1/media/admin/upload/item');
                }
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
