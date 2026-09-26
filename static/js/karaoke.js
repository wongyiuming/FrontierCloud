'use strict';

const MAX_RECORDING_BYTES = 256 * 1024 * 1024;
const elements = Object.fromEntries([
  'back', 'kind', 'title', 'original', 'accompaniment', 'media', 'lyrics',
  'record', 'pauseResume', 'stop', 'upload', 'fullLyrics', 'status', 'inputDevice', 'outputDevice',
  'refreshDevices', 'songGain', 'songValue', 'songMute', 'voiceGain', 'voiceValue',
  'monitorGain', 'monitorValue', 'monitor', 'aec', 'capabilities', 'previewCard',
  'preview', 'previewHint', 'lyricsOverlay', 'overlayLines', 'account', 'accountName',
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
  recordedBlob: null,
  account: null,
  accountStatus: null,
  authMode: 'login',
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
  state.recordedBlob = null;
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

async function addRecordingMetadata(blob) {
  const encoded = new TextEncoder().encode(JSON.stringify({
    version: 1, title: state.context?.title || '卡拉OK录音', lyrics: state.lyrics,
  }));
  const length = new Uint8Array(8);
  new DataView(length.buffer).setBigUint64(0, BigInt(encoded.byteLength));
  return new Blob([blob, encoded, length, new TextEncoder().encode('FRONTIERCLOUD-KARAOKE-V1')], {type: blob.type});
}

async function finishRecording() {
  const mimeType = state.recorder?.mimeType || state.chunks[0]?.type || 'audio/webm';
  const blob = await addRecordingMetadata(new Blob(state.chunks, {type: mimeType}));
  state.chunks = [];
  state.recordedBytes = 0;
  state.previewUrl = URL.createObjectURL(blob);
  state.recordedBlob = blob;
  elements.preview.src = state.previewUrl;
  elements.previewCard.hidden = false;
  state.phase = 'preview';
  elements.record.disabled = true;
  elements.pauseResume.disabled = false;
  elements.pauseResume.textContent = '暂停';
  elements.stop.disabled = true;
  elements.aec.disabled = false;
  state.recorder = null;
  stopMicrophone();
  elements.upload.disabled = !state.account;
  setStatus(state.account ? '录音已停止，可试听或上传到个人空间。' : '录音已停止；登录后可上传，当前可试听。');
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
    elements.pauseResume.disabled = false;
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

function pauseResume() {
  if (state.phase === 'recording' && state.recorder) {
    if (state.recorder.state === 'recording') {
      state.recorder.pause(); elements.media.pause(); elements.pauseResume.textContent = '恢复';
      setStatus('录音与媒体已暂停。');
    } else if (state.recorder.state === 'paused') {
      state.recorder.resume(); elements.media.play().catch(() => {}); elements.pauseResume.textContent = '暂停';
      setStatus('录音与媒体已恢复。');
    }
    return;
  }
  if (state.phase === 'preview') {
    if (elements.preview.paused) {
      elements.preview.play().then(() => { elements.pauseResume.textContent = '暂停'; })
        .catch(error => setStatus(errorText(error), true));
    } else {
      elements.preview.pause(); elements.pauseResume.textContent = '恢复';
    }
  }
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
  const clock = (!elements.preview.paused && elements.preview.currentSrc) ? elements.preview : elements.media;
  const active = activeLyricIndex(clock.currentTime);
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
  setStatus('浏览器能力检查通过。授权设备后即可开始 卡拉OK。');
}

elements.back.addEventListener('click', () => {
  if (history.length > 1) history.back();
  else location.assign('/api/v1/media');
});
elements.record.addEventListener('click', startRecording);
elements.pauseResume.addEventListener('click', pauseResume);
elements.stop.addEventListener('click', stopRecording);
elements.refreshDevices.addEventListener('click', () => refreshDevices().catch(error => setStatus(`无法读取设备：${errorText(error)}`, true)));
elements.outputDevice.addEventListener('change', () => applyOutputDevice().catch(error => setStatus(`无法切换输出设备：${errorText(error)}`, true)));
elements.inputDevice.addEventListener('change', () => {
  if (state.phase === 'recording') {
    stopRecording();
    setStatus('输入设备已变化，本次录音已安全停止。', true);
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
navigator.mediaDevices?.addEventListener?.('devicechange', () => {
  if (state.phase === 'recording') stopRecording();
  setStatus('音频设备已变化，录音已安全停止；请刷新设备后继续。', true);
});

const accountElements = Object.fromEntries([
  'accountModal', 'accountClose', 'guestAccount', 'profile', 'showLogin', 'showRegister',
  'authForm', 'authUsername', 'authPassword', 'authSubmit', 'authMessage', 'captchaRow',
  'captchaImage', 'captchaValue', 'captchaRefresh', 'profileSummary',
  'bindStorage', 'uploadFile', 'uploadFileInput', 'logoutAccount', 'passwordForm',
  'currentPassword', 'newPassword', 'recordingList', 'deleteAccount', 'storageModal',
].map(id => [id, document.getElementById(id)]));

function cookie(name) {
  return document.cookie.split(';').map(item => item.trim()).find(item => item.startsWith(`${name}=`))?.split('=').slice(1).join('=') || '';
}

function karaokeHeaders(json = true) {
  const headers = {};
  if (json) headers['Content-Type'] = 'application/json';
  const name = location.protocol === 'https:' ? '__Host-karaoke_csrf' : 'karaoke_csrf';
  const token = decodeURIComponent(cookie(name));
  if (token) headers['X-Karaoke-CSRF'] = token;
  return headers;
}

async function accountApi(path, options = {}) {
  const response = await fetch(`/api/v1/karaoke/account${path}`, {cache: 'no-store', ...options});
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.detail || `请求失败（HTTP ${response.status}）`);
    error.captchaRequired = response.headers.get('X-Captcha-Required') === '1';
    error.storageRequired = response.headers.get('X-Storage-Binding-Required') === '1';
    throw error;
  }
  return payload;
}

async function loadCaptcha() {
  const data = await accountApi('/captcha');
  accountElements.captchaImage.dataset.challenge = data.challenge;
  accountElements.captchaImage.src = `${data.image_url}?v=${Date.now()}`;
  accountElements.captchaValue.value = '';
}

function showCaptcha(show) {
  accountElements.captchaRow.hidden = !show;
  accountElements.captchaValue.required = show;
  if (show) loadCaptcha().catch(error => { accountElements.authMessage.textContent = errorText(error); });
}

function setAuthMode(mode) {
  state.authMode = mode;
  accountElements.showLogin.classList.toggle('selected', mode === 'login');
  accountElements.showRegister.classList.toggle('selected', mode === 'register');
  accountElements.authSubmit.textContent = mode === 'login' ? '登录' : '注册';
  accountElements.authPassword.autocomplete = mode === 'login' ? 'current-password' : 'new-password';
  accountElements.authMessage.textContent = mode === 'register' ? '密码需包含大小写字母、数字和特殊字符，至少 10 位。' : '';
  showCaptcha(mode === 'register');
}

function fillStorageSelect(select) {
  select.replaceChildren();
  select.add(new Option('由 Master Storage Scheduler 自动选择', 'auto'));
  select.disabled = true;
}

function renderAccount() {
  const authenticated = Boolean(state.account);
  elements.accountName.textContent = authenticated ? state.account.username : '游客';
  elements.account.textContent = authenticated ? '个人主页' : '登录 / 注册';
  elements.upload.disabled = !authenticated || !state.recordedBlob;
  accountElements.guestAccount.hidden = authenticated;
  accountElements.profile.hidden = !authenticated;
  if (authenticated) {
    accountElements.profileSummary.textContent = `${state.account.username} · 已用 ${(state.account.used_bytes / 1048576).toFixed(1)} / ${(state.account.quota_bytes / 1048576).toFixed(1)} MiB`;
  }
}

async function refreshAccount() {
  state.accountStatus = await accountApi('/status');
  state.account = state.accountStatus.user || null;
  renderAccount();
  if (state.account) await loadRecordings();
}

function recordingButton(label, handler) {
  const button = document.createElement('button'); button.type = 'button'; button.textContent = label;
  button.onclick = () => handler().catch(error => setStatus(errorText(error), true)); return button;
}

async function loadRecordings() {
  const data = await accountApi('/recordings');
  accountElements.recordingList.replaceChildren();
  for (const item of data.items) {
    const row = document.createElement('div'); row.className = 'recording-item';
    const info = document.createElement('div');
    const name = document.createElement('strong'); name.textContent = item.filename;
    const detail = document.createElement('small'); detail.textContent = `${(item.size_bytes / 1048576).toFixed(1)} MiB · ${new Date(item.created_at * 1000).toLocaleString()}`;
    info.append(name, detail);
    const actions = document.createElement('div'); actions.className = 'recording-actions';
    actions.append(
      recordingButton('试听', async () => {
        state.lyrics = Array.isArray(item.lyrics) ? item.lyrics : [];
        state.activeLyric = -2;
        elements.preview.src = `/api/v1/karaoke/account/recordings/${item.recording_id}/stream`;
        elements.previewCard.hidden = false; elements.previewHint.textContent = item.filename;
        await elements.preview.play(); accountElements.accountModal.hidden = true;
      }),
      recordingButton('下载', async () => {
        const anchor = document.createElement('a');
        anchor.href = `/api/v1/karaoke/account/recordings/${item.recording_id}/download`; anchor.download = item.filename; anchor.click();
      }),
      recordingButton('删除', async () => {
        if (!confirm(`删除录音 ${item.filename}？`)) return;
        await accountApi(`/recordings/${item.recording_id}`, {method: 'DELETE', headers: karaokeHeaders(false)});
        await refreshAccount();
      }),
    );
    row.append(info, actions); accountElements.recordingList.append(row);
  }
  if (!data.items.length) accountElements.recordingList.textContent = '暂无已上传录音。';
}

async function uploadBlob(blob, title, media = null) {
  if (!state.account) throw new Error('请先登录 卡拉OK账号');
  setStatus('正在预留个人空间…');
  const ticket = await accountApi('/recordings/ticket', {method: 'POST', headers: karaokeHeaders(), body: JSON.stringify({
    size_bytes: blob.size, content_type: blob.type || 'application/octet-stream', media, title,
  })});
  const headers = {'Content-Type': blob.type || 'application/octet-stream'};
  if (ticket.direct) headers['X-Recording-Capability'] = ticket.capability;
  else Object.assign(headers, karaokeHeaders(false));
  setStatus(`正在${ticket.direct ? '直传' : '中继上传'}到录音存储节点…`);
  try {
    const uploaded = await fetch(ticket.upload_url, {method: 'PUT', headers, body: blob});
    if (!uploaded.ok) throw new Error(`录音上传失败（HTTP ${uploaded.status}）`);
    await accountApi(`/recordings/${ticket.recording_id}/finalize`, {method: 'POST', headers: karaokeHeaders(false)});
  } catch (error) {
    await accountApi(`/recordings/${ticket.recording_id}/pending`, {method: 'DELETE', headers: karaokeHeaders(false)}).catch(() => {});
    throw error;
  }
  elements.previewHint.textContent = `已上传：${ticket.filename}`;
  setStatus('录音已上传到个人空间。');
  await refreshAccount();
}

elements.upload.addEventListener('click', () => {
  if (!state.recordedBlob) return;
  const media = new URLSearchParams(location.search).get('media');
  uploadBlob(state.recordedBlob, state.context?.title, media).catch(error => setStatus(errorText(error), true));
});
elements.account.addEventListener('click', async () => {
  accountElements.accountModal.hidden = false;
  if (state.account) await loadRecordings();
});
accountElements.accountClose.onclick = () => { accountElements.accountModal.hidden = true; };
accountElements.showLogin.onclick = () => setAuthMode('login');
accountElements.showRegister.onclick = () => setAuthMode('register');
accountElements.captchaRefresh.onclick = loadCaptcha;
accountElements.authForm.onsubmit = async event => {
  event.preventDefault(); accountElements.authMessage.textContent = '处理中…';
  const payload = {
    username: accountElements.authUsername.value, password: accountElements.authPassword.value,
    challenge: accountElements.captchaImage.dataset.challenge || null,
    captcha: accountElements.captchaValue.value || null,
    webrtc_addresses: window.frontierCloudObservedAddresses || [],
  };
  try {
    await accountApi(`/${state.authMode}`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    await refreshAccount(); accountElements.authMessage.textContent = ''; setStatus('卡拉OK账号已登录。');
  } catch (error) {
    accountElements.authMessage.textContent = errorText(error);
    if (state.authMode === 'register' || error.captchaRequired) showCaptcha(true);
  }
};
accountElements.uploadFile.onclick = () => accountElements.uploadFileInput.click();
accountElements.uploadFileInput.onchange = async event => {
  const file = event.target.files[0]; if (!file) return;
  try { await uploadBlob(file, file.name.replace(/\.[^.]+$/, ''), null); }
  catch (error) { setStatus(errorText(error), true); }
  finally { event.target.value = ''; }
};
accountElements.logoutAccount.onclick = async () => {
  await accountApi('/logout', {method: 'POST', headers: karaokeHeaders(false)}); state.account = null; await refreshAccount();
};
accountElements.passwordForm.onsubmit = async event => {
  event.preventDefault();
  await accountApi('/password', {method: 'POST', headers: karaokeHeaders(), body: JSON.stringify({current_password: accountElements.currentPassword.value, new_password: accountElements.newPassword.value})});
  state.account = null; accountElements.accountModal.hidden = true; await refreshAccount(); setStatus('密码已修改，请重新登录。');
};
accountElements.deleteAccount.onclick = async () => {
  if (!confirm('注销账号会永久删除全部录音，确定继续？')) return;
  await accountApi('', {method: 'DELETE', headers: karaokeHeaders(false)}); state.account = null; accountElements.accountModal.hidden = true; await refreshAccount();
};

window.addEventListener('pagehide', () => {
  if (state.recorder && state.recorder.state !== 'inactive') state.recorder.stop();
  stopMicrophone();
  clearPreview();
  state.audioContext?.close();
});

requestAnimationFrame(lyricClock);
Promise.all([initialize(), refreshAccount()]).catch(error => {
  setStatus(`卡拉OK页面初始化失败：${errorText(error)}`, true);
  elements.record.disabled = true;
});
setAuthMode('login');
