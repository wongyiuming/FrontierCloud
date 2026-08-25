(() => {
    'use strict';

    const segmentDurationMs = 10000;
    const maxClipCount = 3;
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    const legacyGetUserMedia = (
        navigator.getUserMedia
        || navigator.webkitGetUserMedia
        || navigator.mozGetUserMedia
    );

    const elements = {
        captureState: document.getElementById('captureState'),
        startCapture: document.getElementById('startCapture'),
        stopCapture: document.getElementById('stopCapture'),
        speakerTest: document.getElementById('speakerTest'),
        toggleMonitor: document.getElementById('toggleMonitor'),
        monitorGain: document.getElementById('monitorGain'),
        monitorGainValue: document.getElementById('monitorGainValue'),
        echoCancellation: document.getElementById('echoCancellation'),
        noiseSuppression: document.getElementById('noiseSuppression'),
        autoGainControl: document.getElementById('autoGainControl'),
        levelBar: document.getElementById('levelBar'),
        levelValue: document.getElementById('levelValue'),
        capabilities: document.getElementById('capabilities'),
        playLatest: document.getElementById('playLatest'),
        clearClips: document.getElementById('clearClips'),
        clipPlayer: document.getElementById('clipPlayer'),
        clipSummary: document.getElementById('clipSummary'),
        backingFile: document.getElementById('backingFile'),
        backingPlayer: document.getElementById('backingPlayer'),
        copyDiagnostics: document.getElementById('copyDiagnostics'),
        diagnostics: document.getElementById('diagnostics'),
    };

    let stream = null;
    let audioContext = null;
    let sourceNode = null;
    let analyserNode = null;
    let monitorGainNode = null;
    let meterFrame = null;
    let monitorEnabled = false;
    let recordingDesired = false;
    let recorder = null;
    let recorderTimer = null;
    let segmentChunks = [];
    let segmentStartedAt = 0;
    let clips = [];
    let backingObjectUrl = null;
    let permissionState = 'unknown';
    let inputDevices = [];
    let activeTrackDetails = 'none';
    const diagnosticLines = [];

    const capabilityRows = [
        ['安全上下文', () => window.isSecureContext ? '是（HTTPS 或本机地址）' : '否'],
        ['标准采集 API', () => navigator.mediaDevices && navigator.mediaDevices.getUserMedia ? '可用' : '不可用'],
        ['旧版采集 API', () => legacyGetUserMedia ? '可用' : '不可用'],
        ['麦克风权限', () => permissionState],
        ['可见音频输入', () => inputDevices.length ? inputDevices.map(deviceLabel).join('；') : '0 个'],
        ['Web Audio', () => AudioContextClass ? '可用' : '不可用'],
        ['MediaRecorder', () => window.MediaRecorder ? '可用' : '不可用'],
        ['当前输入轨道', () => activeTrackDetails],
        ['音频上下文', () => audioContextDetails()],
        ['浏览器标识', () => navigator.userAgent],
    ];

    function timestamp() {
        return new Date().toLocaleTimeString('zh-CN', {hour12: false});
    }

    function log(message, details) {
        let line = `[${timestamp()}] ${message}`;
        if (details !== undefined && details !== null && details !== '') {
            line += ` | ${typeof details === 'string' ? details : JSON.stringify(details)}`;
        }
        diagnosticLines.push(line);
        if (diagnosticLines.length > 120) {
            diagnosticLines.splice(0, diagnosticLines.length - 120);
        }
        elements.diagnostics.textContent = diagnosticLines.join('\n');
        elements.diagnostics.scrollTop = elements.diagnostics.scrollHeight;
    }

    function deviceLabel(device, index) {
        const fallbackIndex = inputDevices.indexOf(device);
        const number = index === undefined ? fallbackIndex + 1 : index + 1;
        return device.label || `音频输入 ${number}`;
    }

    function audioContextDetails() {
        if (!audioContext) {
            return '尚未创建';
        }
        const sampleRate = `${audioContext.sampleRate || '?'} Hz`;
        const baseLatency = Number.isFinite(audioContext.baseLatency)
            ? `${Math.round(audioContext.baseLatency * 1000)} ms`
            : '未知';
        const outputLatency = Number.isFinite(audioContext.outputLatency)
            ? `${Math.round(audioContext.outputLatency * 1000)} ms`
            : '未知';
        return `${audioContext.state}，${sampleRate}，基础延迟 ${baseLatency}，输出延迟 ${outputLatency}`;
    }

    function renderCapabilities() {
        elements.capabilities.replaceChildren();
        capabilityRows.forEach(([label, valueFactory], index) => {
            const dt = document.createElement('dt');
            const dd = document.createElement('dd');
            const value = String(valueFactory());
            dt.textContent = label;
            dd.textContent = value;
            if (index < 7) {
                dd.className = /不可用|否|denied|0 个/.test(value) ? 'bad' : 'good';
            }
            elements.capabilities.append(dt, dd);
        });
    }

    function setCaptureState(label, className) {
        elements.captureState.textContent = label;
        elements.captureState.className = `state ${className}`;
    }

    function describeError(error) {
        const name = error && error.name ? error.name : 'Error';
        const detail = error && error.message ? error.message : String(error);
        const explanations = {
            NotAllowedError: '车机浏览器拒绝了麦克风权限，或系统策略禁止网页访问内置麦克风。',
            NotFoundError: '车机浏览器没有向网页暴露任何音频输入设备。',
            NotReadableError: '系统或语音助手占用了采集设备，浏览器无法读取。',
            AbortError: '底层音频设备启动失败。',
            OverconstrainedError: '车机音频设备不支持请求的采集参数。',
            SecurityError: '浏览器安全策略禁止音频采集。',
            TypeError: '页面不是安全上下文，或浏览器不支持所需采集接口。',
            TimeoutError: '车机浏览器在 15 秒内没有返回权限或设备结果，可能没有实现授权界面。',
        };
        return `${name}: ${explanations[name] || detail} [${detail}]`;
    }

    async function queryPermission() {
        if (!navigator.permissions || !navigator.permissions.query) {
            permissionState = '浏览器不支持查询';
            renderCapabilities();
            return;
        }
        try {
            const permission = await navigator.permissions.query({name: 'microphone'});
            permissionState = permission.state;
            permission.onchange = () => {
                permissionState = permission.state;
                log('麦克风权限状态变化', permissionState);
                renderCapabilities();
            };
        } catch (error) {
            permissionState = '无法查询';
            log('麦克风权限查询不可用', `${error.name || 'Error'}: ${error.message || error}`);
        }
        renderCapabilities();
    }

    async function enumerateInputs() {
        if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
            inputDevices = [];
            renderCapabilities();
            return;
        }
        try {
            const devices = await navigator.mediaDevices.enumerateDevices();
            inputDevices = devices.filter(device => device.kind === 'audioinput');
            log('枚举音频输入完成', inputDevices.map((device, index) => deviceLabel(device, index)));
        } catch (error) {
            inputDevices = [];
            log('枚举音频输入失败', describeError(error));
        }
        renderCapabilities();
    }

    function requestAudioStream(constraints) {
        if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
            return navigator.mediaDevices.getUserMedia(constraints);
        }
        if (legacyGetUserMedia) {
            return new Promise((resolve, reject) => {
                legacyGetUserMedia.call(navigator, constraints, resolve, reject);
            });
        }
        return Promise.reject(new TypeError('getUserMedia is not available'));
    }

    function requestAudioStreamWithTimeout(constraints) {
        let expired = false;
        return new Promise((resolve, reject) => {
            const timer = window.setTimeout(() => {
                expired = true;
                const error = new Error('getUserMedia did not settle within 15 seconds');
                error.name = 'TimeoutError';
                reject(error);
            }, 15000);
            requestAudioStream(constraints).then(candidateStream => {
                if (expired) {
                    candidateStream.getTracks().forEach(track => track.stop());
                    return;
                }
                window.clearTimeout(timer);
                resolve(candidateStream);
            }).catch(error => {
                if (!expired) {
                    window.clearTimeout(timer);
                    reject(error);
                }
            });
        });
    }

    async function ensureAudioContext() {
        if (!AudioContextClass) {
            throw new Error('Web Audio API is not available');
        }
        if (!audioContext || audioContext.state === 'closed') {
            try {
                audioContext = new AudioContextClass({latencyHint: 'interactive'});
            } catch (error) {
                audioContext = new AudioContextClass();
                log('车机不支持低延迟构造参数，已使用默认音频上下文', error.message || String(error));
            }
            audioContext.onstatechange = () => {
                log('音频上下文状态变化', audioContext.state);
                renderCapabilities();
            };
        }
        if (audioContext.state === 'suspended') {
            await audioContext.resume();
        }
        renderCapabilities();
        return audioContext;
    }

    function updateMonitorGain() {
        const percentage = Number(elements.monitorGain.value);
        elements.monitorGainValue.textContent = `${percentage}%`;
        if (monitorGainNode && audioContext) {
            const target = monitorEnabled ? percentage / 100 : 0;
            monitorGainNode.gain.setTargetAtTime(target, audioContext.currentTime, 0.015);
        }
    }

    function startMeter() {
        if (!analyserNode) {
            return;
        }
        const samples = new Float32Array(analyserNode.fftSize);
        const legacySamples = new Uint8Array(analyserNode.fftSize);
        const draw = () => {
            if (!analyserNode) {
                return;
            }
            if (analyserNode.getFloatTimeDomainData) {
                analyserNode.getFloatTimeDomainData(samples);
            } else {
                analyserNode.getByteTimeDomainData(legacySamples);
                for (let index = 0; index < legacySamples.length; index += 1) {
                    samples[index] = (legacySamples[index] - 128) / 128;
                }
            }
            let energy = 0;
            for (let index = 0; index < samples.length; index += 1) {
                energy += samples[index] * samples[index];
            }
            const rms = Math.sqrt(energy / samples.length);
            const decibels = rms > 0 ? Math.max(-90, 20 * Math.log10(rms)) : -90;
            const width = Math.max(0, Math.min(100, ((decibels + 60) / 60) * 100));
            elements.levelBar.style.width = `${width}%`;
            elements.levelValue.textContent = decibels <= -89 ? '-∞ dB' : `${decibels.toFixed(1)} dB`;
            meterFrame = requestAnimationFrame(draw);
        };
        draw();
    }

    function resetMeter() {
        if (meterFrame !== null) {
            cancelAnimationFrame(meterFrame);
            meterFrame = null;
        }
        elements.levelBar.style.width = '0%';
        elements.levelValue.textContent = '-∞ dB';
    }

    function preferredRecorderMimeType() {
        if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) {
            return '';
        }
        return [
            'audio/webm;codecs=opus',
            'audio/webm',
            'audio/mp4',
            'audio/ogg;codecs=opus',
        ].find(type => MediaRecorder.isTypeSupported(type)) || '';
    }

    function renderClips() {
        elements.playLatest.disabled = clips.length === 0;
        elements.clearClips.disabled = clips.length === 0;
        if (!clips.length) {
            elements.clipSummary.textContent = window.MediaRecorder
                ? '暂无录音片段'
                : '当前浏览器不支持短缓存录音';
            return;
        }
        const latest = clips[clips.length - 1];
        elements.clipSummary.textContent = `已缓存 ${clips.length} 段；最近一段 ${latest.seconds.toFixed(1)} 秒，${Math.ceil(latest.blob.size / 1024)} KiB`;
    }

    function storeClip(blob, elapsedMs) {
        if (!blob.size) {
            return;
        }
        const clip = {
            blob,
            seconds: Math.max(0, elapsedMs) / 1000,
            url: URL.createObjectURL(blob),
        };
        clips.push(clip);
        while (clips.length > maxClipCount) {
            const expired = clips.shift();
            URL.revokeObjectURL(expired.url);
        }
        log('本地录音片段已缓存', `${clip.seconds.toFixed(1)} 秒，${blob.type || '默认格式'}，${blob.size} bytes`);
        renderClips();
    }

    function startRecorderSegment() {
        if (!recordingDesired || !stream || !stream.active || !window.MediaRecorder) {
            return;
        }
        const mimeType = preferredRecorderMimeType();
        segmentChunks = [];
        segmentStartedAt = performance.now();
        const options = mimeType
            ? {mimeType, audioBitsPerSecond: 128000}
            : {audioBitsPerSecond: 128000};
        try {
            recorder = new MediaRecorder(stream, options);
        } catch (optionsError) {
            try {
                recorder = new MediaRecorder(stream);
                log('车机不支持录音编码参数，已使用浏览器默认格式', optionsError.message || String(optionsError));
            } catch (error) {
                log('短缓存录音初始化失败', describeError(error));
                recorder = null;
                return;
            }
        }
        recorder.ondataavailable = event => {
            if (event.data && event.data.size) {
                segmentChunks.push(event.data);
            }
        };
        recorder.onerror = event => {
            log('短缓存录音失败', describeError(event.error || event));
        };
        recorder.onstop = () => {
            const elapsedMs = performance.now() - segmentStartedAt;
            const type = recorder && recorder.mimeType ? recorder.mimeType : mimeType;
            if (segmentChunks.length) {
                storeClip(new Blob(segmentChunks, type ? {type} : undefined), elapsedMs);
            }
            recorder = null;
            segmentChunks = [];
            if (recordingDesired) {
                window.setTimeout(startRecorderSegment, 0);
            }
        };
        recorder.start();
        recorderTimer = window.setTimeout(() => {
            if (recorder && recorder.state === 'recording') {
                recorder.stop();
            }
        }, segmentDurationMs);
    }

    function stopRecorder() {
        recordingDesired = false;
        if (recorderTimer !== null) {
            window.clearTimeout(recorderTimer);
            recorderTimer = null;
        }
        if (recorder && recorder.state !== 'inactive') {
            recorder.stop();
        }
    }

    async function startCapture() {
        elements.startCapture.disabled = true;
        setCaptureState('正在请求权限', 'idle');
        log('开始请求车机音频输入');
        if (!window.isSecureContext) {
            const error = new TypeError('Microphone capture requires HTTPS');
            log('采集失败', describeError(error));
            setCaptureState('非安全页面', 'error');
            elements.startCapture.disabled = false;
            return;
        }

        const audioConstraints = {
            channelCount: {ideal: 1},
            sampleRate: {ideal: 48000},
            echoCancellation: {ideal: elements.echoCancellation.checked},
            noiseSuppression: {ideal: elements.noiseSuppression.checked},
            autoGainControl: {ideal: elements.autoGainControl.checked},
        };

        try {
            stream = await requestAudioStreamWithTimeout({audio: audioConstraints, video: false});
            const tracks = stream.getAudioTracks();
            if (!tracks.length) {
                throw new Error('The returned stream has no audio track');
            }
            const track = tracks[0];
            const settings = track.getSettings ? track.getSettings() : {};
            activeTrackDetails = `${track.label || '未命名输入'}；${JSON.stringify(settings)}`;
            log('音频输入轨道已获取', activeTrackDetails);

            await ensureAudioContext();
            sourceNode = audioContext.createMediaStreamSource(stream);
            analyserNode = audioContext.createAnalyser();
            analyserNode.fftSize = 2048;
            analyserNode.smoothingTimeConstant = 0.72;
            monitorGainNode = audioContext.createGain();
            monitorGainNode.gain.value = 0;
            sourceNode.connect(analyserNode);
            sourceNode.connect(monitorGainNode);
            monitorGainNode.connect(audioContext.destination);

            track.onmute = () => log('音频输入轨道被系统静音');
            track.onunmute = () => log('音频输入轨道恢复');
            track.onended = () => {
                log('音频输入轨道被系统终止');
                setCaptureState('采集被系统终止', 'error');
                stopCapture();
            };

            monitorEnabled = false;
            updateMonitorGain();
            elements.toggleMonitor.textContent = '打开实时返听';
            elements.toggleMonitor.disabled = false;
            elements.stopCapture.disabled = false;
            setCaptureState('正在采集', 'live');
            startMeter();
            recordingDesired = Boolean(window.MediaRecorder);
            startRecorderSegment();
            await enumerateInputs();
            await queryPermission();
            renderCapabilities();
        } catch (error) {
            log('采集失败', describeError(error));
            setCaptureState('采集失败', 'error');
            if (stream) {
                stream.getTracks().forEach(track => track.stop());
                stream = null;
            }
            activeTrackDetails = 'none';
            renderCapabilities();
        } finally {
            elements.startCapture.disabled = Boolean(stream);
        }
    }

    function stopCapture() {
        stopRecorder();
        monitorEnabled = false;
        if (stream) {
            stream.getTracks().forEach(track => {
                track.onended = null;
                track.stop();
            });
            stream = null;
        }
        [sourceNode, analyserNode, monitorGainNode].forEach(node => {
            if (node) {
                try {
                    node.disconnect();
                } catch (error) {
                    log('音频节点释放警告', error.message || String(error));
                }
            }
        });
        sourceNode = null;
        analyserNode = null;
        monitorGainNode = null;
        activeTrackDetails = 'none';
        resetMeter();
        elements.startCapture.disabled = false;
        elements.stopCapture.disabled = true;
        elements.toggleMonitor.disabled = true;
        elements.toggleMonitor.textContent = '打开实时返听';
        setCaptureState('已停止', 'idle');
        log('音频采集已停止');
        renderCapabilities();
    }

    async function toggleMonitor() {
        if (!stream || !monitorGainNode) {
            return;
        }
        try {
            await ensureAudioContext();
            monitorEnabled = !monitorEnabled;
            updateMonitorGain();
            elements.toggleMonitor.textContent = monitorEnabled ? '关闭实时返听' : '打开实时返听';
            log(monitorEnabled ? '实时返听已打开' : '实时返听已关闭', `${elements.monitorGain.value}%`);
        } catch (error) {
            log('实时返听启动失败', describeError(error));
        }
    }

    async function testSpeaker() {
        try {
            const context = await ensureAudioContext();
            const oscillator = context.createOscillator();
            const gain = context.createGain();
            const startAt = context.currentTime;
            oscillator.frequency.value = 523.25;
            gain.gain.setValueAtTime(0.0001, startAt);
            gain.gain.exponentialRampToValueAtTime(0.045, startAt + 0.03);
            gain.gain.exponentialRampToValueAtTime(0.0001, startAt + 0.32);
            oscillator.connect(gain);
            gain.connect(context.destination);
            oscillator.start(startAt);
            oscillator.stop(startAt + 0.34);
            oscillator.onended = () => {
                oscillator.disconnect();
                gain.disconnect();
            };
            log('扬声器测试音已发送', audioContextDetails());
        } catch (error) {
            log('扬声器测试失败', describeError(error));
        }
    }

    async function playLatestClip() {
        if (!clips.length) {
            return;
        }
        const latest = clips[clips.length - 1];
        elements.clipPlayer.src = latest.url;
        try {
            await elements.clipPlayer.play();
            log('开始回放最近本地片段', `${latest.seconds.toFixed(1)} 秒`);
        } catch (error) {
            log('本地片段回放失败', describeError(error));
        }
    }

    function clearClips() {
        elements.clipPlayer.pause();
        elements.clipPlayer.removeAttribute('src');
        elements.clipPlayer.load();
        clips.forEach(clip => URL.revokeObjectURL(clip.url));
        clips = [];
        renderClips();
        log('本地录音缓存已清空');
    }

    function loadBackingFile() {
        const file = elements.backingFile.files && elements.backingFile.files[0];
        if (!file) {
            return;
        }
        if (backingObjectUrl) {
            URL.revokeObjectURL(backingObjectUrl);
        }
        backingObjectUrl = URL.createObjectURL(file);
        elements.backingPlayer.src = backingObjectUrl;
        log('本地伴奏已载入', `${file.name}，${file.size} bytes`);
    }

    async function copyDiagnostics() {
        const text = diagnosticLines.join('\n');
        try {
            if (!navigator.clipboard || !navigator.clipboard.writeText) {
                throw new Error('Clipboard API is unavailable');
            }
            await navigator.clipboard.writeText(text);
            elements.copyDiagnostics.textContent = '已复制';
        } catch (error) {
            const textarea = document.createElement('textarea');
            textarea.value = text;
            textarea.style.position = 'fixed';
            textarea.style.opacity = '0';
            document.body.appendChild(textarea);
            textarea.select();
            const copied = document.execCommand('copy');
            textarea.remove();
            elements.copyDiagnostics.textContent = copied ? '已复制' : '复制失败';
        }
        window.setTimeout(() => {
            elements.copyDiagnostics.textContent = '复制诊断';
        }, 1500);
    }

    elements.startCapture.addEventListener('click', startCapture);
    elements.stopCapture.addEventListener('click', stopCapture);
    elements.speakerTest.addEventListener('click', testSpeaker);
    elements.toggleMonitor.addEventListener('click', toggleMonitor);
    elements.monitorGain.addEventListener('input', updateMonitorGain);
    elements.playLatest.addEventListener('click', playLatestClip);
    elements.clearClips.addEventListener('click', clearClips);
    elements.backingFile.addEventListener('change', loadBackingFile);
    elements.copyDiagnostics.addEventListener('click', copyDiagnostics);

    if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
        navigator.mediaDevices.addEventListener('devicechange', enumerateInputs);
    }
    document.addEventListener('visibilitychange', () => {
        log('页面可见性变化', document.visibilityState);
        if (document.visibilityState === 'visible' && stream && audioContext && audioContext.state === 'suspended') {
            audioContext.resume().catch(error => log('恢复音频上下文失败', describeError(error)));
        }
    });
    window.addEventListener('pagehide', () => {
        stopRecorder();
        if (stream) {
            stream.getTracks().forEach(track => track.stop());
        }
        clips.forEach(clip => URL.revokeObjectURL(clip.url));
        if (backingObjectUrl) {
            URL.revokeObjectURL(backingObjectUrl);
        }
    });

    updateMonitorGain();
    renderClips();
    renderCapabilities();
    log('能力探测页面已加载', {
        secureContext: window.isSecureContext,
        protocol: location.protocol,
        mediaDevices: Boolean(navigator.mediaDevices),
        getUserMedia: Boolean(
            (navigator.mediaDevices && navigator.mediaDevices.getUserMedia)
            || legacyGetUserMedia
        ),
        audioContext: Boolean(AudioContextClass),
        mediaRecorder: Boolean(window.MediaRecorder),
        userAgent: navigator.userAgent,
    });
    queryPermission();
    enumerateInputs();
})();
