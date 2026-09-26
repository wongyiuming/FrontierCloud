'use strict';

(() => {
  const overlay = document.getElementById('lyricsOverlay');
  const overlayLines = document.getElementById('overlayLines');
  const inlineLyrics = document.getElementById('lyrics');
  const fullLyrics = document.getElementById('fullLyrics');
  const preview = document.getElementById('preview');
  const voiceGain = document.getElementById('voiceGain');
  const voiceValue = document.getElementById('voiceValue');
  const monitorGain = document.getElementById('monitorGain');
  const monitorValue = document.getElementById('monitorValue');
  const lyricUi = window.FrontierLyricsUI;
  const fallbackLyrics = Object.freeze([{time: 0, text: '建设中，暂无歌词'}]);

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

  function visibleLyrics() {
    return state.lyrics.length ? state.lyrics : fallbackLyrics;
  }

  function visibleActive(active) {
    return state.lyrics.length ? active : 0;
  }

  if (inlineLyrics) inlineLyrics.classList.add('frontier-karaoke-lyrics');
  if (overlay) {
    overlay.classList.add('fullscreen-lyrics', 'hidden');
    overlay.classList.remove('open');
  }
  if (overlayLines) overlayLines.classList.add('fullscreen-lyrics-columns');
  overlay?.querySelector(':scope > span')?.classList.add('fullscreen-lyrics-hint');

  if (lyricUi && typeof window.renderLyricContainer === 'function') {
    const originalRenderLyricContainer = window.renderLyricContainer;
    window.renderLyricContainer = function renderSharedKaraokeLyrics(container, active) {
      if (container === inlineLyrics) {
        lyricUi.renderWindow(container, visibleLyrics(), visibleActive(active));
        return;
      }
      if (container === overlayLines) {
        lyricUi.syncFullscreenLyrics(container, visibleActive(active));
        return;
      }
      return originalRenderLyricContainer(container, active);
    };
  }

  function renderFullscreen() {
    if (!lyricUi || !overlay || !overlayLines) return;
    lyricUi.renderFullscreenLyrics(
      overlayLines,
      visibleLyrics(),
      visibleActive(state.activeLyric),
      overlay,
    );
    lyricUi.sizeFullscreenLyrics(overlay, visibleLyrics());
  }

  if (fullLyrics) {
    // The fallback lyric keeps fullscreen meaningful even for media types that
    // do not participate in the audio lyric-link database.
    const keepAvailable = () => {
      if (fullLyrics.disabled) fullLyrics.disabled = false;
    };
    keepAvailable();
    new MutationObserver(keepAvailable).observe(fullLyrics, {
      attributes: true,
      attributeFilter: ['disabled'],
    });
    fullLyrics.addEventListener('click', () => {
      if (overlay) overlay.style.display = 'grid';
      overlay?.classList.remove('hidden');
      overlay?.classList.add('open');
      overlay?.setAttribute('aria-hidden', 'false');
      renderFullscreen();
    });
  }

  overlay?.addEventListener('click', () => {
    overlay.style.display = '';
    overlay.classList.add('hidden');
    overlay.classList.remove('open');
    overlay.setAttribute('aria-hidden', 'true');
  });
  window.addEventListener('resize', () => {
    if (!overlay?.classList.contains('hidden')) {
      lyricUi?.sizeFullscreenLyrics(overlay, visibleLyrics());
    }
  });

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
