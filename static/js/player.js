let art = null;
let currentIndex = 0;
let isNextPreloaded = false;
let clickTimer = null;
let playbackState = null;
let playbackReporter = null;

// 防御 DOM XSS：全局 HTML 字符转义函数
function escapeHTML(str) {
    if (typeof str !== 'string') return str;
    return str.replace(/[&<>'\"]/g, tag => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        "'": '&#39;',
        '"': '&quot;'
    }[tag] || tag));
}

function initCornerTyping() {
    const config = [
        { id: 'cornerTL', text: '中', delay: 0 },
        { id: 'cornerTR', text: '国', delay: 250 },
        { id: 'cornerBL', text: '爱', delay: 500 },
        { id: 'cornerBR', text: '我', delay: 750 }
    ];

    config.forEach(item => {
        const el = document.getElementById(item.id);
        if (!el) return;
        el.innerText = '';
        setTimeout(() => {
            let index = 0;
            const timer = setInterval(() => {
                if (index < item.text.length) {
                    el.innerText += item.text.charAt(index);
                    index++;
                } else {
                    clearInterval(timer);
                }
            }, 150);
        }, item.delay);
    });
}

function updateMediaSession(media) {
    if ('mediaSession' in navigator) {
        navigator.mediaSession.metadata = new MediaMetadata({
            title: media.title || '未知曲目',
            artist: media.artist || '前沿视界',
            album: typeof PAGE_TITLE !== 'undefined' ? PAGE_TITLE : '前沿视界',
            artwork: [
                { src: media.cover, sizes: '512x512', type: 'image/png' }
            ]
        });

        navigator.mediaSession.setActionHandler('play', () => { if (art) art.play(); });
        navigator.mediaSession.setActionHandler('pause', () => { if (art) art.pause(); });
        navigator.mediaSession.setActionHandler('previoustrack', () => { playPrev(); });
        navigator.mediaSession.setActionHandler('nexttrack', () => { playNext(); });
    }
}

function checkAndPreloadNext(currentTime) {
    if (currentTime >= 20 && !isNextPreloaded) {
        isNextPreloaded = true;
        if (!currentMediaList || currentMediaList.length <= 1) return;
        const nextIndex = (currentIndex + 1) % currentMediaList.length;
        const nextUrl = currentMediaList[nextIndex].url;

        fetch(nextUrl, { method: 'GET', headers: { 'Range': 'bytes=0-2097152' } })
            .catch(() => {});
    }
}

function playNext() {
    if (!currentMediaList || currentMediaList.length === 0) return;
    const nextIndex = (currentIndex + 1) % currentMediaList.length;
    selectMedia(nextIndex);
}

function playPrev() {
    if (!currentMediaList || currentMediaList.length === 0) return;
    const prevIndex = (currentIndex - 1 + currentMediaList.length) % currentMediaList.length;
    selectMedia(prevIndex);
}

function playbackThreshold(duration) {
    return Math.max(5, Math.min(30, duration * 0.5));
}

function updateTrackStats(media) {
    const row = document.querySelector(`.media-item[data-media-id="${media.media_id}"]`);
    if (!row) return;
    const preference = row.querySelector('.media-preference');
    const score = row.querySelector('.media-score');
    if (preference) preference.textContent = `喜好 ${media.preference > 0 ? '+' : ''}${media.preference}`;
    if (score) score.textContent = `播放 ${media.play_score}`;
    for (const button of row.querySelectorAll('.preference-btn')) {
        const delta = Number(button.dataset.delta);
        button.disabled = (delta > 0 && media.preference >= 2) || (delta < 0 && media.preference <= -2);
    }
}

function resetPlaybackAccounting(media) {
    playbackState = {
        mediaId: media.media_id,
        accumulated: 0,
        lastTick: null,
        reporting: false,
        reported: false,
    };
}

function accountPlaybackTime() {
    if (!art || !playbackState) return;
    const now = performance.now();
    const playing = art.playing === true || (art.video && !art.video.paused);
    if (playing && playbackState.lastTick !== null) {
        const elapsed = (now - playbackState.lastTick) / 1000;
        if (elapsed > 0 && elapsed <= 2.5) playbackState.accumulated += elapsed;
    }
    playbackState.lastTick = playing ? now : null;
}

async function reportValidPlayback() {
    accountPlaybackTime();
    if (!art || !playbackState || playbackState.reporting || playbackState.reported) return;
    const media = currentMediaList[currentIndex];
    const duration = Number(art.duration || 0);
    if (!media || !duration || playbackState.mediaId !== media.media_id) return;
    if (playbackState.accumulated + 0.05 < playbackThreshold(duration)) return;

    playbackState.reporting = true;
    try {
        const response = await fetch('/api/v1/media/playback', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                media_path: media.media_path,
                playback_session_id: playbackSessionId,
                played_seconds: playbackState.accumulated,
                duration,
            }),
        });
        if (!response.ok) return;
        const data = await response.json();
        playbackState.reported = true;
        media.play_score = data.play_score;
        updateTrackStats(media);
    } catch (_error) {
        // Playback remains available while transient accounting failures retry.
    } finally {
        playbackState.reporting = false;
    }
}

async function changePreference(index, delta) {
    const media = currentMediaList[index];
    if (!media) return;
    const response = await fetch('/api/v1/media/preference', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({media_path: media.media_path, delta}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || '喜好调整失败');
    media.preference = data.preference;
    media.play_score = data.play_score;
    updateTrackStats(media);
}

function initPlayer(media, index) {
    accountPlaybackTime();
    currentIndex = index;
    isNextPreloaded = false;
    resetPlaybackAccounting(media);
    const isAudio = media.type === 'audio';
    const audioCover = document.getElementById('audioCover');
    const audioDisk = document.getElementById('audioDisk');
    const audioBlurBg = document.getElementById('audioBlurBg');

    if (isAudio) {
        audioCover.style.display = 'flex';
        const safeCover = encodeURI(media.cover);
        audioDisk.style.backgroundImage = `url('${safeCover}')`;
        audioBlurBg.style.backgroundImage = `url('${safeCover}')`;
    } else {
        audioCover.style.display = 'none';
    }

    if (art) {
        art.switchUrl(media.url).then(() => {
            art.title = media.title;
            art.play();
            if (isAudio) {
                audioDisk.classList.add('rotate-disk');
            } else {
                audioDisk.classList.remove('rotate-disk');
            }
            updateMediaSession(media);
        }).catch(() => {
            art.url = media.url;
            art.play();
            updateMediaSession(media);
        });
        return;
    }

    art = new Artplayer({
        container: '#artplayer',
        url: media.url,
        title: media.title,
        volume: 0.7,
        autoplay: true,
        autoSize: true,
        fullscreen: true,
        fullscreenWeb: true,
    });

    art.on('play', () => {
        const activeMedia = currentMediaList[currentIndex];
        if (activeMedia.type === 'audio') audioDisk.classList.add('rotate-disk');
        if (playbackState) playbackState.lastTick = performance.now();
        if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'playing';
    });

    art.on('pause', () => {
        accountPlaybackTime();
        audioDisk.classList.remove('rotate-disk');
        if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'paused';
    });

    art.on('video:timeupdate', () => {
        checkAndPreloadNext(art.currentTime);
        reportValidPlayback();
    });

    art.on('video:ended', () => {
        playNext();
    });

    updateMediaSession(media);
}

function formatTime(seconds) {
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}:${s < 10 ? '0' : ''}${s}`;
}

function initGestureControl() {
    const playerSection = document.getElementById('playerSection');
    const gestureHud = document.getElementById('gestureHud');

    let touchStartX = 0;
    let touchStartY = 0;
    let initialTime = 0;
    let targetTime = 0;
    let isDragging = false;

    playerSection.addEventListener('touchstart', (e) => {
        if (e.touches.length > 1 || !art) return;

        touchStartX = e.touches[0].clientX;
        touchStartY = e.touches[0].clientY;
        initialTime = art.currentTime;
        targetTime = initialTime;
        isDragging = false;
    }, { passive: false });

    playerSection.addEventListener('touchmove', (e) => {
        if (e.touches.length > 1 || !art) return;

        const currentX = e.touches[0].clientX;
        const currentY = e.touches[0].clientY;
        const deltaX = currentX - touchStartX;
        const deltaY = currentY - touchStartY;

        if (!isDragging && Math.abs(deltaX) > 10 && Math.abs(deltaX) > Math.abs(deltaY)) {
            isDragging = true;
        }

        if (isDragging) {
            e.preventDefault();

            const duration = art.duration || 1;
            const sensitivity = 0.2;
            const seekOffset = deltaX * sensitivity;

            targetTime = Math.min(Math.max(0, initialTime + seekOffset), duration);

            const sign = seekOffset >= 0 ? '+' : '';
            gestureHud.innerText = `${sign}${Math.round(seekOffset)}s (${formatTime(targetTime)} / ${formatTime(duration)})`;
            gestureHud.style.display = 'block';
        }
    }, { passive: false });

    playerSection.addEventListener('touchend', (e) => {
        if (isDragging) {
            if (art) art.currentTime = targetTime;
            gestureHud.style.display = 'none';
            isDragging = false;
            if (e.cancelable) e.preventDefault();
        }
    });

    playerSection.addEventListener('touchcancel', () => {
        if (isDragging) {
            gestureHud.style.display = 'none';
            isDragging = false;
        }
    });

    playerSection.addEventListener('dblclick', (e) => {
        const isControl = e.target.closest('.sidebar') ||
                          e.target.closest('.art-bottom') ||
                          e.target.closest('.art-controls') ||
                          e.target.closest('.art-setting') ||
                          e.target.closest('.art-contextmenu');
        if (isControl) return;

        e.stopPropagation();
        e.preventDefault();
    }, true);

    playerSection.addEventListener('click', (e) => {
        const isControl = e.target.closest('.sidebar') ||
                          e.target.closest('.art-bottom') ||
                          e.target.closest('.art-controls') ||
                          e.target.closest('.art-setting') ||
                          e.target.closest('.art-contextmenu');
        if (isControl) return;

        e.stopPropagation();
        e.preventDefault();

        if (clickTimer) {
            clearTimeout(clickTimer);
            clickTimer = null;
            playPrev();
        } else {
            clickTimer = setTimeout(() => {
                clickTimer = null;
                playNext();
            }, 250);
        }
    }, true);
}

window.addEventListener('DOMContentLoaded', () => {
    const listContainer = document.getElementById('mediaList');

    if (typeof currentMediaList === 'undefined' || currentMediaList.length === 0) {
        listContainer.innerHTML = '<li style="padding:20px;color:#666;text-align:center;">该分类下暂无媒体数据</li>';
        return;
    }

    listContainer.innerHTML = currentMediaList.map((item, index) => `
        <li class="media-item ${index === 0 ? 'active' : ''}" data-media-id="${escapeHTML(item.media_id)}" onclick="selectMedia(${index})">
            <img src="${escapeHTML(item.cover)}" alt="cover">
            <div class="media-info">
                <div class="media-title">${escapeHTML(item.title)}</div>
                <div class="media-artist">${escapeHTML(item.artist)}</div>
                <div class="media-stats"><span class="media-preference">喜好 ${item.preference > 0 ? '+' : ''}${item.preference}</span><span class="media-score">播放 ${item.play_score}</span></div>
            </div>
            <div class="preference-controls">
                <button type="button" class="preference-btn" data-index="${index}" data-delta="-1" aria-label="降低喜好" ${item.preference <= -2 ? 'disabled' : ''}>−</button>
                <button type="button" class="preference-btn" data-index="${index}" data-delta="1" aria-label="提高喜好" ${item.preference >= 2 ? 'disabled' : ''}>＋</button>
            </div>
        </li>
    `).join('');

    for (const button of listContainer.querySelectorAll('.preference-btn')) {
        button.addEventListener('click', async event => {
            event.stopPropagation();
            button.disabled = true;
            try {
                await changePreference(Number(button.dataset.index), Number(button.dataset.delta));
            } catch (error) {
                alert(error.message);
            } finally {
                updateTrackStats(currentMediaList[Number(button.dataset.index)]);
            }
        });
    }

    initPlayer(currentMediaList[0], 0);
    initGestureControl();
    initCornerTyping();
    playbackReporter = setInterval(reportValidPlayback, 1000);
});

window.addEventListener('pagehide', () => {
    if (playbackReporter) clearInterval(playbackReporter);
    accountPlaybackTime();
});

function selectMedia(index) {
    const items = document.querySelectorAll('.media-item');
    items.forEach(item => item.classList.remove('active'));

    const targetElement = items[index];
    if (targetElement) {
        targetElement.classList.add('active');
        targetElement.scrollIntoView({ block: 'nearest', behavior: 'auto' });
    }

    initPlayer(currentMediaList[index], index);
}

window.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowRight' || e.key === 'MediaTrackNext' || e.code === 'MediaTrackNext') {
        e.preventDefault();
        playNext();
    } else if (e.key === 'ArrowLeft' || e.key === 'MediaTrackPrevious' || e.code === 'MediaTrackPrevious') {
        e.preventDefault();
        playPrev();
    }
});
