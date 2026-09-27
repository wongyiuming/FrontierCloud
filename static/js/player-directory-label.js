(() => {
    const host = document.getElementById('playerDirectoryLabel');
    if (!host) return;

    const rawPath = new URLSearchParams(window.location.search).get('path') || '';
    const parts = rawPath.replace(/\\/g, '/').split('/').filter(Boolean);
    const relativeParts = (parts[0] === 'music' || parts[0] === 'vido')
        ? parts.slice(1)
        : parts;
    const relativePath = relativeParts.join('/');

    host.textContent = relativePath || '当前目录';
    host.title = rawPath ? `/data/media/${rawPath}` : '';
})();
