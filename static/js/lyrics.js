const lyricPalette = ['#aebfca', '#b9c4a6', '#c5b49f', '#b8adc4', '#9fbeb9', '#c4bda4'];

function renderLyrics() {
    const board = document.getElementById('lyricsBoard');
    const columns = [0, 1, 2].map(index => document.getElementById(`lyricsColumn${index}`));
    columns.forEach(column => { column.innerHTML = ''; });
    const rowsPerColumn = Math.max(1, Math.ceil(lyricLines.length / columns.length));
    lyricLines.forEach((line, index) => {
        const row = document.createElement('p');
        row.className = 'lyric-line';
        row.textContent = line || '\u00a0';
        row.style.color = lyricPalette[index % lyricPalette.length];
        columns[Math.min(columns.length - 1, Math.floor(index / rowsPerColumn))].appendChild(row);
    });

    const availableHeight = Math.max(120, board.clientHeight - 40);
    const heightSize = availableHeight / (rowsPerColumn * 1.38);
    const longest = Math.max(1, ...lyricLines.map(line => Array.from(line).length));
    const availableWidth = Math.max(100, board.clientWidth / columns.length - 56);
    const widthSize = availableWidth / Math.max(4, longest * 1.05);
    const fontSize = Math.max(10, Math.min(48, heightSize, widthSize));
    document.documentElement.style.setProperty('--lyric-font-size', `${fontSize}px`);
}

window.addEventListener('DOMContentLoaded', renderLyrics);
window.addEventListener('resize', renderLyrics);
