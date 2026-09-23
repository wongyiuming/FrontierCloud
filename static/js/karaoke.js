'use strict';

const MAX_RECORDING_BYTES = 256 * 1024 * 1024;
const elements = Object.fromEntries([
  'back', 'kind', 'title', 'original', 'accompaniment', 'media', 'lyrics', 'play',
  'record', 'stop', 'fullLyrics', 'status', 'inputDevice', 'outputDevice',
  'refreshDevices', 'songGain', 'songValue', 'songMute', 'voiceGain', 'voiceValue',
  'monitorGain', 'monitorValue', 'monitor', 'aec', 'capabilities', 'previewCard',
  'preview', 'lyricsOverlay', 'overlayLines',
].map(id => [id, document.getElementById(id)]));

const state = {
  phase: 'idle',
  context: null,
  lyrics: [],
  activeLyric: -2,
  audioContext: null,
  graph: null,
  microphone: null,
  recorder: null,
  chunks: [],
  recordedBytes: 0,
  previewUrl: null,
  accompaniment: false,
};

function errorText(error) {
  if (!error) return '未知错误';
  const name = error.name && error.name !== 'Error' ? `${error.name}: ` : '';
  return `${name}${error.message || String(error)}`;
}

function setStatus(message, isError = false) {
  elements.status.textContent = message;
  elements.status.style.color = isError ? '#ff9f9f' : '';
}

function percentage(id) {
  return Number(elements[id].value) / 100;
}

function applyLevels() {
  if (!state.graph) return;
  state.graph.songGain.gain.value = elements.songMute.checked ? 0 : percentage('songGain');
  state.graph.recordGain.gain.value = Math.min(6, Math.max(0, percentage('voiceGain')));
  state.graph.monitorGain.gain.value = elements.monitor.checked
    ? Math.min(2, Math.max(0, percentage('monitorGain')))
    : 0;
}

function setAccompaniment(enabled) {
  state.accompaniment = enabled;
  elements.original.classList.toggle('selected', !enabled);
  elements.accompaniment.classList.toggle('selected', enabled);
  if (!state.graph) return;
  state.graph.mediaSource.disconnect();
  state.graph.mediaSource.connect(enabled ? state.graph.splitter : state.graph.songGain);
}

function prepareAudioGraph() {
  if (state.graph) {
    void state.audioContext.resume();
    return state.graph;
  }
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  const context = new AudioContextClass({latencyHint: 'interactive'});
  state.audioContext = context;
  void context.resume();

  const songGain = context.createGain();
  songGain.connect(context.destination);

  const highPass = context.createBiquadFilter();
  highPass.type = 'highpass';
  highPass.frequency.value = 80;
  const lowPass = context.createBiquadFilter();
  lowPass.type = 'lowpass';
  lowPass.frequency.value = 14000;
  highPass.connect(lowPass);

  const recordGain = context.createGain();
  const limiter = context.createDynamicsCompressor();
  limiter.threshold.value = -3;
  limiter.knee.value = 6;
  limiter.ratio.value = 12;
  limiter.attack.value = 0.003;
  limiter.release.value = 0.25;
  const recordDestination = context.createMediaStreamDestination();
  lowPass.connect(recordGain);
  recordGain.connect(limiter);
  limiter.connect(recordDestination);

  const monitorGain = context.createGain();
  monitorGain.gain.value = 0;
  lowPass.connect(monitorGain);
  monitorGain.connect(context.destination);

  const splitter = context.createChannelSplitter(2);
  const left = context.createGain();
  const right = context.createGain();
  right.gain.value = -1;
  const merger = context.createChannelMerger(2);
  splitter.connect(left, 0);
  splitter.connect(right, 1);
  left.connect(merger, 0, 0);
  right.connect(merger, 0, 0);
  left.connect(merger, 0, 1);
  right.connect(merger, 0, 1);
  merger.connect(songGain);

  // This binding may only be created once for a media element. Store it before
  // any later asynchronous work so repeated record clicks always reuse it.
  const mediaSource = context.createMediaElementSource(elements.media);
  const graph = {
    context, songGain, highPass, lowPass, recordGain, limiter, recordDestination,
    monitorGain, splitter, mediaSource, microphoneSource: null,
  };
  state.graph = graph;
  mediaSource.connect(state.accompaniment ? splitter : songGain);
  applyLevels();
  void applyOutputDevice();
  return graph;
}

function microphoneConstraints() {
  const audio = {
    echoCancellation: elements.aec.checked,
    noiseSuppression: false,
    autoGainControl: false,
    channelCount: 1,
  };
  if (elements.inputDevice.value) audio.deviceId = {exact: elements.inputDevice.value};
  return {audio, video: false};
}

async function openMicrophone() {
  return navigator.mediaDevices.getUserMedia(microphoneConstraints());
}

function replaceMicrophone(stream) {
  const graph = prepareAudioGraph();
  if (graph.microphoneSource) graph.microphoneSource.disconnect();
  graph.microphoneSource = graph.context.createMediaStreamSource(stream);
  graph.microphoneSource.connect(graph.highPass);
}

function stopMicrophone() {
  if (!state.microphone) return;
  for (const track of state.microphone.getTracks()) track.stop();
  state.microphone = null;
}

function clearPreview() {
  elements.preview.pause();
  elements.preview.removeAttribute('src');
  if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
  state.previewUrl = null;
  elements.previewCard.hidden = true;
}

function preferredRecordingOptions() {
  for (const mimeType of ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus']) {
    if (MediaRecorder.isTypeSupported(mimeType)) return {mimeType};
  }
  return undefined;
}

function resetFailedStart(message) {
  state.phase = 'idle';
  elements.record.disabled = false;
  elements.stop.disabled = true;
  elements.aec.disabled = false;
  stopMicrophone();
  setStatus(`无法开始录音：${message}`, true);
}

function finishRecording() {
  const mimeType = state.recorder?.mimeType || state.chunks[0]?.type || 'audio/webm';
  const blob = new Blob(state.chunks, {type: mimeType});
  state.chunks = [];
  state.recordedBytes = 0;
  state.previewUrl = URL.createObjectURL(blob);
  elements.preview.src = state.previewUrl;
  elements.previewCard.hidden = false;
  state.phase = 'preview';
  elements.record.disabled = false;
  elements.record.textContent = '重录';
  elements.stop.disabled = true;
  elements.aec.disabled = false;
  state.recorder = null;
  stopMicrophone();
  setStatus('录音已停止，仅在当前页面提供试听。');
  void applyOutputDevice();
}

async function startRecording() {
  if (state.phase === 'initializing' || state.phase === 'recording') return;
  state.phase = 'initializing';
  elements.record.disabled = true;
  elements.stop.disabled = true;
  elements.aec.disabled = true;
  clearPreview();
  setStatus('正在初始化麦克风和纯人声录音支路…');
  try {
    // Create and retain the media binding synchronously inside the trusted click.
    // Every later recording reuses this graph and replaces only the microphone.
    prepareAudioGraph();
    stopMicrophone();
    const stream = await openMicrophone();
    state.microphone = stream;
    replaceMicrophone(stream);
    await state.audioContext.resume();
    await applyOutputDevice();

    state.chunks = [];
    state.recordedBytes = 0;
    const recorder = new MediaRecorder(state.graph.recordDestination.stream, preferredRecordingOptions());
    state.recorder = recorder;
    recorder.addEventListener('dataavailable', event => {
      if (!event.data?.size) return;
      state.recordedBytes += event.data.size;
      if (state.recordedBytes > MAX_RECORDING_BYTES) {
        setStatus('录音达到 256MB 安全上限，已自动停止。', true);
        if (recorder.state !== 'inactive') recorder.stop();
        return;
      }
      state.chunks.push(event.data);
    });
    recorder.addEventListener('stop', finishRecording, {once: true});
    recorder.addEventListener('error', event => {
      resetFailedStart(errorText(event.error));
    }, {once: true});
    recorder.start(1000);
    state.phase = 'recording';
    elements.stop.disabled = false;
    setStatus('正在录制纯人声支路；媒体和返听数字信号不会进入录音。');
    elements.media.play().catch(error => {
      setStatus(`录音继续，但媒体播放失败：${errorText(error)}`, true);
    });
  } catch (error) {
    resetFailedStart(errorText(error));
  }
}

function stopRecording() {
  if (state.recorder?.state !== 'inactive') state.recorder.stop();
  elements.media.pause();
}

async function applyOutputDevice() {
  const sinkId = elements.outputDevice.value;
  if (!sinkId) return;
  if (state.audioContext && typeof state.audioContext.setSinkId === 'function') {
    await state.audioContext.setSinkId(sinkId);
  }
  if (typeof elements.preview.setSinkId === 'function') await elements.preview.setSinkId(sinkId);
}

function fillDeviceOptions(select, devices, defaultLabel, fallbackLabel) {
  const selected = select.value;
  select.replaceChildren(new Option(defaultLabel, ''));
  devices.forEach((device, index) => select.add(new Option(device.label || `${fallbackLabel} ${index + 1}`, device.deviceId)));
  if ([...select.options].some(option => option.value === selected)) select.value = selected;
}

async function refreshDevices() {
  const permission = await openMicrophone();
  permission.getTracks().forEach(track => track.stop());
  const devices = await navigator.mediaDevices.enumerateDevices();
  fillDeviceOptions(elements.inputDevice, devices.filter(device => device.kind === 'audioinput'), '系统默认麦克风', '麦克风');
  fillDeviceOptions(elements.outputDevice, devices.filter(device => device.kind === 'audiooutput'), '系统默认输出', '输出设备');
  setStatus('输入和输出设备列表已刷新。');
}

function activeLyricIndex(time) {
  let low = 0;
  let high = state.lyrics.length - 1;
  let found = -1;
  while (low <= high) {
    const middle = (low + high) >> 1;
    if (Number(state.lyrics[middle].time) <= time) {
      found = middle;
      low = middle + 1;
    } else {
      high = middle - 1;
    }
  }
  return found;
}

function renderLyricContainer(container, active) {
  container.replaceChildren();
  if (!state.lyrics.length) {
    const line = document.createElement('p');
    line.textContent = '当前媒体没有已关联歌词。';
    container.append(line);
    return;
  }
  const center = Math.max(0, active);
  const start = Math.max(0, center - 1);
  for (let index = start; index < Math.min(state.lyrics.length, start + 4); index += 1) {
    const line = document.createElement('p');
    line.textContent = state.lyrics[index].text;
    if (index === active) line.className = 'current';
    container.append(line);
  }
}

function lyricClock() {
  const active = activeLyricIndex(elements.media.currentTime);
  if (active !== state.activeLyric) {
    state.activeLyric = active;
    renderLyricContainer(elements.lyrics, active);
    renderLyricContainer(elements.overlayLines, active);
  }
  requestAnimationFrame(lyricClock);
}

function capabilityReport() {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  const missing = [];
  if (!AudioContextClass) missing.push('AudioContext');
  if (!navigator.mediaDevices?.getUserMedia) missing.push('麦克风访问');
  if (!window.MediaRecorder) missing.push('MediaRecorder');
  if (missing.length) throw new Error(`当前浏览器缺少：${missing.join('、')}`);

  const supported = navigator.mediaDevices.getSupportedConstraints?.() || {};
  if (!supported.echoCancellation) elements.aec.checked = false;
  const outputSelection = typeof AudioContextClass.prototype.setSinkId === 'function';
  elements.outputDevice.disabled = !outputSelection;
  elements.capabilities.textContent = `AEC ${supported.echoCancellation ? '可用' : '降级'} · 输出设备选择 ${outputSelection ? '可用' : '使用系统默认'} · 触控点 ${navigator.maxTouchPoints || 0}`;
}

async function initialize() {
  capabilityReport();
  const mediaId = new URLSearchParams(location.search).get('media');
  if (!mediaId) throw new Error('缺少当前媒体身份');
  const response = await fetch(`/api/v1/karaoke/context?media=${encodeURIComponent(mediaId)}`, {cache: 'no-store'});
  if (!response.ok) throw new Error(`无法读取当前媒体（HTTP ${response.status}）`);
  state.context = await response.json();
  elements.title.textContent = state.context.title;
  elements.kind.textContent = state.context.type === 'video' ? '视频' : '音乐';
  elements.media.src = state.context.stream_url;
  if (state.context.type === 'audio') elements.media.style.display = 'none';
  if (state.context.has_lyrics && state.context.lyrics_url) {
    const lyricResponse = await fetch(state.context.lyrics_url, {cache: 'no-store'});
    if (!lyricResponse.ok) throw new Error(`无法读取歌词（HTTP ${lyricResponse.status}）`);
    const payload = await lyricResponse.json();
    state.lyrics = Array.isArray(payload.entries) ? payload.entries : [];
  } else {
    elements.fullLyrics.disabled = true;
  }
  state.activeLyric = -2;
  renderLyricContainer(elements.lyrics, -1);
  renderLyricContainer(elements.overlayLines, -1);
  setStatus('浏览器能力检查通过。授权设备后即可开始 K 歌。');
}

elements.back.addEventListener('click', () => {
  if (history.length > 1) history.back();
  else location.assign('/api/v1/media');
});
elements.play.addEventListener('click', () => {
  if (elements.media.paused) {
    elements.media.play().then(() => setStatus('仅播放媒体，没有录音。')).catch(error => setStatus(errorText(error), true));
  } else {
    elements.media.pause();
    setStatus('媒体已暂停。');
  }
});
elements.record.addEventListener('click', startRecording);
elements.stop.addEventListener('click', stopRecording);
elements.refreshDevices.addEventListener('click', () => refreshDevices().catch(error => setStatus(`无法读取设备：${errorText(error)}`, true)));
elements.outputDevice.addEventListener('change', () => applyOutputDevice().catch(error => setStatus(`无法切换输出设备：${errorText(error)}`, true)));
elements.inputDevice.addEventListener('change', () => {
  if (state.phase === 'recording') {
    stopRecording();
    setStatus('输入设备已变化，本次录音已停止；请重录。', true);
  }
});
elements.original.addEventListener('click', () => {
  setAccompaniment(false);
  setStatus('已选择原唱。');
});
elements.accompaniment.addEventListener('click', () => {
  setAccompaniment(true);
  setStatus('已选择中置消除伴奏；不同音源的分离效果会有差异。');
});
for (const [input, output] of [['songGain', 'songValue'], ['voiceGain', 'voiceValue'], ['monitorGain', 'monitorValue']]) {
  elements[input].addEventListener('input', () => {
    elements[output].textContent = `${elements[input].value}%`;
    applyLevels();
  });
}
elements.songMute.addEventListener('change', applyLevels);
elements.monitor.addEventListener('change', applyLevels);
elements.fullLyrics.addEventListener('click', () => {
  elements.lyricsOverlay.classList.add('open');
  elements.lyricsOverlay.setAttribute('aria-hidden', 'false');
});
elements.lyricsOverlay.addEventListener('click', () => {
  elements.lyricsOverlay.classList.remove('open');
  elements.lyricsOverlay.setAttribute('aria-hidden', 'true');
});
navigator.mediaDevices?.addEventListener?.('devicechange', () => {
  if (state.phase === 'recording') stopRecording();
  setStatus('音频设备已变化，录音已安全停止；请刷新设备后继续。', true);
});
window.addEventListener('pagehide', () => {
  if (state.recorder?.state !== 'inactive') state.recorder.stop();
  stopMicrophone();
  clearPreview();
  state.audioContext?.close();
});

requestAnimationFrame(lyricClock);
initialize().catch(error => {
  setStatus(`K歌页面初始化失败：${errorText(error)}`, true);
  elements.record.disabled = true;
});
