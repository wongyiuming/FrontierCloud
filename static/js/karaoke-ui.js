'use strict';

(() => {
  const overlay = document.getElementById('lyricsOverlay');
  const overlayLines = document.getElementById('overlayLines');
  const preview = document.getElementById('preview');
  const voiceGain = document.getElementById('voiceGain');
  const voiceValue = document.getElementById('voiceValue');
  const monitorGain = document.getElementById('monitorGain');
  const monitorValue = document.getElementById('monitorValue');

  // Keep the declared HTML defaults and the live control state aligned even if
  // an older page shell was restored by the browser from bfcache.
  if (voiceGain && voiceValue) {
    voiceGain.value = '30';
    voiceValue.textContent = '30%';
  }
  if (monitorGain && monitorValue) {
    monitorGain.value = '150';
    monitorValue.textContent = '150%';
  }

  const style = document.createElement('style');
  style.textContent = `
    #lyricsOverlay {
      --fullscreen-lyric-font-size: 24px;
      z-index: 2147483600;
      place-items: stretch;
      overflow: hidden;
      padding: clamp(20px, 3vw, 54px);
      color: #fff;
      background: radial-gradient(circle at 50% 45%, #17212a, #05080c 72%);
      touch-action: manipulation;
      user-select: none;
    }
    #lyricsOverlay.open { display: grid; }
    #overlayLines {
      display: grid;
      width: 100%;
      height: 100%;
      min-width: 0;
      min-height: 0;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: clamp(12px, 2vw, 36px);
      place-content: stretch;
      text-align: center;
    }
    #overlayLines .fullscreen-lyrics-column {
      display: flex;
      min-width: 0;
      min-height: 0;
      flex-direction: column;
      justify-content: center;
      gap: .2em;
      overflow: hidden;
    }
    #overlayLines .fullscreen-lyric-line {
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
    #overlayLines .fullscreen-lyric-line:nth-child(6n+2) { color: #b9c4a6; }
    #overlayLines .fullscreen-lyric-line:nth-child(6n+3) { color: #c5b49f; }
    #overlayLines .fullscreen-lyric-line:nth-child(6n+4) { color: #b8adc4; }
    #overlayLines .fullscreen-lyric-line:nth-child(6n+5) { color: #9fbeb9; }
    #overlayLines .fullscreen-lyric-line:nth-child(6n) { color: #c4bda4; }
    #overlayLines .fullscreen-lyric-line.current {
      color: #fff;
      opacity: 1;
      transform: scale(1.04);
      text-shadow: 0 0 14px rgba(94, 214, 255, .7);
    }
  `;
  document.head.append(style);

  function sizeFullscreenLyrics() {
    if (!overlay || !overlay.classList.contains('open') || !overlayLines) return;
    const lines = overlayLines.querySelectorAll('.fullscreen-lyric-line');
    if (!lines.length) return;
    const rowsPerColumn = Math.max(1, Math.ceil(lines.length / 3));
    const longest = Math.max(1, ...Array.from(lines, line => Array.from(line.textContent || '').length));
    const heightSize = Math.max(10, (overlay.clientHeight - 80) / (rowsPerColumn * 1.22));
    const widthSize = Math.max(10, (overlay.clientWidth / 3 - 56) / Math.max(4, longest * 1.02));
    overlay.style.setProperty('--fullscreen-lyric-font-size', `${Math.max(10, Math.min(48, heightSize, widthSize))}px`);
  }

  function renderFullscreenLyrics(active) {
    if (!overlayLines) return;
    overlayLines.replaceChildren();
    if (!state.lyrics.length) {
      const line = document.createElement('p');
      line.className = 'fullscreen-lyric-line';
      line.textContent = '当前媒体没有已关联歌词。';
      overlayLines.append(line);
      return;
    }

    const columns = Array.from({length: 3}, () => {
      const column = document.createElement('div');
      column.className = 'fullscreen-lyrics-column';
      overlayLines.append(column);
      return column;
    });
    const rowsPerColumn = Math.max(1, Math.ceil(state.lyrics.length / columns.length));
    state.lyrics.forEach((entry, index) => {
      const row = document.createElement('p');
      row.className = 'fullscreen-lyric-line';
      row.dataset.lyricIndex = String(index);
      row.textContent = entry.text || '\u00a0';
      if (index === active) row.classList.add('current');
      columns[Math.min(columns.length - 1, Math.floor(index / rowsPerColumn))].append(row);
    });
    requestAnimationFrame(sizeFullscreenLyrics);
  }

  const originalRenderLyricContainer = window.renderLyricContainer;
  if (typeof originalRenderLyricContainer === 'function') {
    window.renderLyricContainer = function renderKaraokeLyrics(container, active) {
      if (container === overlayLines) {
        renderFullscreenLyrics(active);
        return;
      }
      return originalRenderLyricContainer(container, active);
    };
  }

  document.getElementById('fullLyrics')?.addEventListener('click', () => {
    renderFullscreenLyrics(state.activeLyric);
    requestAnimationFrame(sizeFullscreenLyrics);
  });
  window.addEventListener('resize', sizeFullscreenLyrics);

  // Chrome may report MediaRecorder WebM blobs with duration=Infinity because
  // the recording has no finalized duration element. Probe the end of the local
  // blob once so the browser computes a stable finite duration and native seek
  // controls stop stretching as playback time advances.
  function stabilizeLocalRecordingDuration() {
    if (!preview || !preview.currentSrc.startsWith('blob:') || Number.isFinite(preview.duration)) return;
    const restore = () => {
      if (!Number.isFinite(preview.duration) || preview.duration <= 0) return;
      preview.removeEventListener('durationchange', restore);
      preview.removeEventListener('timeupdate', restore);
      try { preview.currentTime = 0; } catch (_error) {}
    };
    preview.addEventListener('durationchange', restore);
    preview.addEventListener('timeupdate', restore);
    try {
      preview.currentTime = 1e101;
    } catch (_error) {
      preview.removeEventListener('durationchange', restore);
      preview.removeEventListener('timeupdate', restore);
    }
  }

  preview?.addEventListener('loadedmetadata', stabilizeLocalRecordingDuration);
})();
