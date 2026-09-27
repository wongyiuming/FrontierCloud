(() => {
    if (typeof lyricDirectoryButton !== 'function') return;

    const originalLyricDirectoryButton = lyricDirectoryButton;
    lyricDirectoryButton = function(item, kind) {
        const button = originalLyricDirectoryButton(item, kind);
        const count = Number(item?.count);
        const trailing = button.lastElementChild;
        if (trailing && Number.isFinite(count)) {
            const unit = kind === 'track' ? '首曲目' : '份歌词';
            trailing.textContent = `${count} ${unit} · 进入`;
        }
        return button;
    };
})();
