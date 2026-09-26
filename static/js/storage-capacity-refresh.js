/* Keep Media Admin storage facts aligned with the live Storage Pool snapshot. */
(() => {
    const target = document.getElementById('uploadStorageMember');
    if (!target) return;

    const gib = value => `${(Number(value || 0) / 1073741824).toFixed(2)} GiB`;
    let timer = null;
    let loading = false;

    async function refreshStorageCapacity() {
        if (loading) return;
        loading = true;
        try {
            const response = await fetch('/api/v1/media/admin/storage-pool', {
                credentials: 'same-origin', cache: 'no-store',
            });
            if (!response.ok) return;
            const pool = await response.json();
            const writable = (pool.members || []).filter(member =>
                member.storage_enabled && member.health === 'online' && member.writable);
            if (!writable.length) return;

            const previous = target.value;
            target.replaceChildren();
            for (const member of writable) {
                const option = document.createElement('option');
                option.value = member.member_id;
                if (member.member_kind === 'Auto') {
                    option.textContent = `自动选择 · 当前可写 ${gib(member.available_bytes)}`;
                } else {
                    const name = member.member_kind === 'MasterLocal' ? 'Master Local' : member.member_id;
                    option.textContent = `${name} · ${member.transport} · 物理总容量 ${gib(member.physical_total_bytes)} · 当前物理 ${gib(member.physical_free_bytes)} · 当前分配 ${gib(member.current_allocated_bytes ?? member.allocated_bytes)} · 当前项目资源占用 ${gib(member.project_used_bytes ?? member.used_bytes)}`;
                }
                target.append(option);
            }
            if ([...target.options].some(option => option.value === previous)) target.value = previous;
            target.classList.remove('hidden');
        } catch (_error) {
            // The primary Admin script owns session-expiry UI. Capacity refresh is best-effort.
        } finally {
            loading = false;
        }
    }

    // Admin's initial role/status request is asynchronous; give it one turn to settle,
    // then keep the selector current while the page remains open.
    setTimeout(() => {
        refreshStorageCapacity();
        timer = setInterval(refreshStorageCapacity, 10000);
    }, 1000);

    window.addEventListener('pagehide', () => {
        if (timer) clearInterval(timer);
    }, {once: true});
})();
