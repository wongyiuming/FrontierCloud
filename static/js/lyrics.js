const lyricPalette = ['#5ee7ff', '#8cff73', '#ffe066', '#ff8ebf', '#c39bff', '#ff9d66'];

function renderLyrics() {
    const board = document.getElementById('lyricsBoard');
    const left = document.getElementById('lyricsLeft');
    const right = document.getElementById('lyricsRight');
    left.innerHTML = '';
    right.innerHTML = '';
    const splitAt = Math.ceil(lyricLines.length / 2);
    lyricLines.forEach((line, index) => {
        const row = document.createElement('p');
        row.className = 'lyric-line';
        row.textContent = line || '\u00a0';
        row.style.color = lyricPalette[index % lyricPalette.length];
        (index < splitAt ? left : right).appendChild(row);
    });

    const rowsPerColumn = Math.max(1, splitAt);
    const availableHeight = Math.max(120, board.clientHeight - 40);
    const heightSize = availableHeight / (rowsPerColumn * 1.38);
    const longest = Math.max(1, ...lyricLines.map(line => Array.from(line).length));
    const availableWidth = Math.max(120, board.clientWidth / 2 - 80);
    const widthSize = availableWidth / Math.max(4, longest * 1.05);
    const fontSize = Math.max(10, Math.min(48, heightSize, widthSize));
    document.documentElement.style.setProperty('--lyric-font-size', `${fontSize}px`);
}

window.addEventListener('DOMContentLoaded', renderLyrics);
window.addEventListener('resize', renderLyrics);
