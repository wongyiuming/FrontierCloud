'use strict';

(() => {
  const OFFSETS = Object.freeze([-3, -2, -1, 0, 1, 2, 3]);
  const ROW_PERCENT = 100 / OFFSETS.length;

  function rowClass(offset) {
    if (offset <= -4) return 'sync-lyric departing';
    if (offset === -3) return 'sync-lyric previous farthest';
    if (offset === -2) return 'sync-lyric previous far';
    if (offset === -1) return 'sync-lyric previous';
    if (offset === 0) return 'sync-lyric current';
    if (offset === 1) return 'sync-lyric next';
    if (offset === 2) return 'sync-lyric next far';
    return 'sync-lyric trailing';
  }

  function makeRow(entry, offset) {
    const row = document.createElement('p');
    row.className = rowClass(offset);
    row.textContent = entry?.text || (offset === 0 ? '♪' : '\u00a0');
    return row;
  }

  function renderWindow(container, entries, active) {
    if (!container) return;
    const rows = OFFSETS.map(offset => makeRow(entries[active + offset], offset));
    container.replaceChildren(...rows);
  }

  function installStyles() {
    if (document.getElementById('frontier-seven-line-lyrics-style')) return;
    const style = document.createElement('style');
    style.id = 'frontier-seven-line-lyrics-style';
    style.textContent = `
      .inline-lyrics .sync-lyrics-track { height: 100%; }
      .inline-lyrics .sync-lyric { height: ${ROW_PERCENT}%; }
      .sync-lyric.previous.farthest { opacity: .28; transform: scale(.76); }
      .sync-lyric.previous.far { opacity: .46; transform: scale(.82); }
      .sync-lyric.previous { opacity: .7; transform: scale(.9); }
      .sync-lyric.current { opacity: 1; transform: scale(1); }
      .sync-lyric.next { opacity: .7; transform: scale(.9); }
      .sync-lyric.next.far { opacity: .46; transform: scale(.82); }
      .sync-lyric.trailing { opacity: .28; transform: scale(.76); }
      .frontier-karaoke-lyrics {
        --sync-lyric-font-size: clamp(15px, 1.55vw, 24px);
        min-height: 252px;
        display: flex !important;
        flex-direction: column;
        justify-content: center;
        place-content: initial !important;
        gap: 0 !important;
        overflow: hidden;
      }
      .frontier-karaoke-lyrics .sync-lyric {
        flex: 1 1 ${ROW_PERCENT}%;
        height: auto;
        min-height: 0;
        padding: 2px 0;
        font-size: var(--sync-lyric-font-size);
        line-height: 1.08;
      }
      .frontier-karaoke-lyrics .sync-lyric.current {
        font-size: calc(var(--sync-lyric-font-size) * 1.16);
      }
      .fullscreen-lyrics {
        --fullscreen-lyric-font-size: 24px;
        position: fixed;
        inset: 0;
        z-index: 2147483600;
        display: grid;
        place-items: stretch;
        overflow: hidden;
        padding: clamp(20px, 3vw, 54px);
        color: #fff;
        background: radial-gradient(circle at 50% 45%, #17212a, #05080c 72%);
        cursor: pointer;
        touch-action: manipulation;
        user-select: none;
      }
      .fullscreen-lyrics.hidden { display: none; }
      .fullscreen-lyrics-columns {
        display: grid;
        min-width: 0;
        min-height: 0;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: clamp(12px, 2vw, 36px);
      }
      .fullscreen-lyrics-column {
        display: flex;
        min-width: 0;
        min-height: 0;
        flex-direction: column;
        justify-content: center;
        gap: .2em;
        overflow: hidden;
      }
      .fullscreen-lyric-line {
        margin: 0;
        overflow: hidden;
        color: #aebfca;
        font-size: var(--fullscreen-lyric-font-size);
        font-weight: 800;
        line-height: 1.18;
        text-align: center;
        text-overflow: ellipsis;
        white-space: nowrap;
        transition: color .25s ease, opacity .25s ease, transform .25s ease;
      }
      .fullscreen-lyric-line:nth-child(6n+2) { color: #b9c4a6; }
      .fullscreen-lyric-line:nth-child(6n+3) { color: #c5b49f; }
      .fullscreen-lyric-line:nth-child(6n+4) { color: #b8adc4; }
      .fullscreen-lyric-line:nth-child(6n+5) { color: #9fbeb9; }
      .fullscreen-lyric-line:nth-child(6n) { color: #c4bda4; }
      .fullscreen-lyric-line.current {
        color: #fff;
        opacity: 1;
        transform: scale(1.04);
        text-shadow: 0 0 14px rgba(94, 214, 255, .7);
      }
      .fullscreen-lyrics-hint {
        position: absolute;
        right: 18px;
        bottom: 12px;
        color: rgba(220, 232, 241, .42);
        font-size: 11px;
        pointer-events: none;
      }
    `;
    document.head.append(style);
  }

  function sizeFullscreenLyrics(overlay, entries) {
    if (!overlay || overlay.classList.contains('hidden') || !entries.length) return;
    const rowsPerColumn = Math.max(1, Math.ceil(entries.length / 3));
    const longest = Math.max(1, ...entries.map(entry => Array.from(entry.text || '').length));
    const heightSize = Math.max(10, (overlay.clientHeight - 80) / (rowsPerColumn * 1.22));
    const widthSize = Math.max(10, (overlay.clientWidth / 3 - 56) / Math.max(4, longest * 1.02));
    overlay.style.setProperty('--fullscreen-lyric-font-size', `${Math.max(10, Math.min(48, heightSize, widthSize))}px`);
  }

  function syncFullscreenLyrics(container, active) {
    if (!container) return;
    container.querySelector('.fullscreen-lyric-line.current')?.classList.remove('current');
    if (active >= 0) container.querySelector(`[data-lyric-index="${active}"]`)?.classList.add('current');
  }

  function renderFullscreenColumns(columns, entries, active, overlay) {
    if (!Array.isArray(columns) || columns.length !== 3 || columns.some(column => !column)) return;
    columns.forEach(column => column.replaceChildren());
    const rowsPerColumn = Math.max(1, Math.ceil(entries.length / columns.length));
    entries.forEach((entry, index) => {
      const row = document.createElement('p');
      row.className = 'fullscreen-lyric-line';
      row.dataset.lyricIndex = String(index);
      row.textContent = entry.text || '\u00a0';
      columns[Math.min(columns.length - 1, Math.floor(index / rowsPerColumn))].appendChild(row);
    });
    syncFullscreenLyrics(overlay || columns[0]?.parentElement, active);
    requestAnimationFrame(() => sizeFullscreenLyrics(overlay, entries));
  }

  function renderFullscreenLyrics(container, entries, active, overlay) {
    if (!container) return;
    container.replaceChildren();
    container.classList.add('fullscreen-lyrics-columns');
    const columns = Array.from({length: 3}, () => {
      const column = document.createElement('div');
      column.className = 'fullscreen-lyrics-column';
      container.append(column);
      return column;
    });
    renderFullscreenColumns(columns, entries, active, overlay);
  }

  function installAudioPlayerOverrides() {
    if (typeof window.renderLyricWindow !== 'function' || typeof window.updateSynchronizedLyrics !== 'function') return;

    window.lyricRowClass = rowClass;
    window.sizeSynchronizedLyrics = function sizeSevenLineLyrics() {
      const panel = document.getElementById('inlineLyrics');
      const track = document.getElementById('inlineLyricsTrack');
      if (!panel || !track || activeLyricEntries.length === 0) return;
      const visible = Array.from(track.children).slice(0, OFFSETS.length).map(row => row.textContent || '');
      const longest = Math.max(1, ...visible.map(line => Array.from(line).length));
      const heightSize = Math.max(14, panel.clientHeight / 7.3);
      const widthSize = Math.max(14, (panel.clientWidth - 40) / Math.max(6, longest * 1.02));
      panel.style.setProperty('--sync-lyric-font-size', `${Math.max(14, Math.min(44, heightSize, widthSize))}px`);
    };

    window.renderLyricWindow = function renderSevenLineWindow(index) {
      const panel = document.getElementById('inlineLyrics');
      const track = document.getElementById('inlineLyricsTrack');
      if (!panel || !track) return;
      clearLyricSlide();
      track.classList.remove('sliding');
      track.style.transform = 'translateY(0)';
      renderWindow(track, activeLyricEntries, index);
      activeLyricIndex = index;
      setActiveLyricTime(panel, index);
      window.sizeSynchronizedLyrics();
    };

    window.animateLyricForward = function animateSevenLineForward(nextIndex) {
      const panel = document.getElementById('inlineLyrics');
      const track = document.getElementById('inlineLyricsTrack');
      if (!panel || !track || track.children.length !== OFFSETS.length) {
        window.renderLyricWindow(nextIndex);
        return;
      }
      clearLyricSlide();
      Array.from(track.children).forEach((row, rowIndex) => {
        row.className = rowClass(OFFSETS[rowIndex] - 1);
      });
      track.append(makeRow(activeLyricEntries[nextIndex + 3], 3));
      activeLyricIndex = nextIndex;
      setActiveLyricTime(panel, nextIndex);
      void track.offsetWidth;
      track.classList.add('sliding');
      track.style.transform = `translateY(-${ROW_PERCENT}%)`;
      lyricSlideTimer = setTimeout(() => window.renderLyricWindow(nextIndex), LYRIC_SLIDE_MS + 30);
    };

    window.sizeFullscreenLyrics = function sizeSharedPlayerFullscreenLyrics() {
      sizeFullscreenLyrics(document.getElementById('fullscreenLyrics'), activeLyricEntries);
    };

    window.syncFullscreenLyrics = function syncSharedPlayerFullscreenLyrics(index) {
      const overlay = document.getElementById('fullscreenLyrics');
      if (!overlay || overlay.classList.contains('hidden') || index === fullscreenLyricIndex) return;
      syncFullscreenLyrics(overlay, index);
      fullscreenLyricIndex = index;
    };

    window.renderFullscreenLyrics = function renderSharedPlayerFullscreenLyrics() {
      const overlay = document.getElementById('fullscreenLyrics');
      const columns = [0, 1, 2].map(index => document.getElementById(`fullscreenLyricsColumn${index}`));
      if (!overlay || columns.some(column => !column)) return;
      renderFullscreenColumns(columns, activeLyricEntries, activeLyricIndex ?? -1, overlay);
      fullscreenLyricIndex = activeLyricIndex ?? -1;
      sizeFullscreenLyrics(overlay, activeLyricEntries);
    };

    const originalLoadInlineLyrics = window.loadInlineLyrics;
    if (typeof originalLoadInlineLyrics === 'function') {
      window.loadInlineLyrics = async function loadLyricsWithDefault(media) {
        if (media) media.has_lyrics = true;
        const button = document.getElementById('lyricsLink');
        if (button && media) {
          button.classList.remove('unavailable');
          button.disabled = false;
          button.setAttribute('aria-label', `全屏显示 ${media.title} 的歌词`);
        }
        return originalLoadInlineLyrics.call(window, media);
      };
    }
  }

  installStyles();
  window.FrontierLyricsUI = {
    offsets: OFFSETS,
    rowClass,
    renderWindow,
    renderFullscreenLyrics,
    renderFullscreenColumns,
    syncFullscreenLyrics,
    sizeFullscreenLyrics,
  };
  installAudioPlayerOverrides();
})();
