'use strict';

(() => {
  const meter = document.getElementById('inputMeter');
  const meterValue = document.getElementById('inputMeterValue');
  const captureSettings = document.getElementById('captureSettings');
  const quality = {
    frame: 0,
    source: null,
    analyser: null,
    silentGain: null,
    data: null,
    peakHold: 0,
  };

  function dbfs(value) {
    return value > 1e-7 ? 20 * Math.log10(value) : -120;
  }

  function stopMeter() {
    if (quality.frame) cancelAnimationFrame(quality.frame);
    quality.frame = 0;
    for (const node of [quality.source, quality.analyser, quality.silentGain]) {
      try { node?.disconnect(); } catch (_error) {}
    }
    quality.source = null;
    quality.analyser = null;
    quality.silentGain = null;
    quality.data = null;
    quality.peakHold = 0;
    if (meter) meter.value = 0;
    if (meterValue) meterValue.textContent = '待机';
  }

  function renderCaptureSettings(stream, context) {
    if (!captureSettings) return;
    const track = stream.getAudioTracks?.()[0];
    const settings = track?.getSettings?.() || {};
    const sampleRate = settings.sampleRate || context.sampleRate || '未知';
    const channels = settings.channelCount || 1;
    const flag = (name, label) => `${label} ${settings[name] === true ? '开' : settings[name] === false ? '关' : '未知'}`;
    captureSettings.textContent = [
      `实际采集 ${sampleRate} Hz · ${channels} 声道`,
      flag('echoCancellation', 'AEC'),
      flag('noiseSuppression', '降噪'),
      flag('autoGainControl', 'AGC'),
    ].join(' · ');
  }

  function startMeter(stream, graph) {
    stopMeter();
    if (!meter || !meterValue || !graph?.context) return;
    const context = graph.context;
    const source = context.createMediaStreamSource(stream);
    const analyser = context.createAnalyser();
    const silentGain = context.createGain();
    analyser.fftSize = 2048;
    analyser.smoothingTimeConstant = 0.18;
    silentGain.gain.value = 0;
    source.connect(analyser);
    analyser.connect(silentGain);
    silentGain.connect(context.destination);
    quality.source = source;
    quality.analyser = analyser;
    quality.silentGain = silentGain;
    quality.data = new Float32Array(analyser.fftSize);
    renderCaptureSettings(stream, context);

    const tick = () => {
      if (!quality.analyser || !quality.data) return;
      quality.analyser.getFloatTimeDomainData(quality.data);
      let sum = 0;
      let peak = 0;
      for (const sample of quality.data) {
        sum += sample * sample;
        peak = Math.max(peak, Math.abs(sample));
      }
      const rms = Math.sqrt(sum / quality.data.length);
      quality.peakHold = Math.max(peak, quality.peakHold * 0.965);
      meter.value = Math.min(1, quality.peakHold);
      const peakDb = dbfs(quality.peakHold);
      const warning = peakDb > -1 ? ' · 接近削波' : '';
      meterValue.textContent = `RMS ${dbfs(rms).toFixed(1)} dBFS · Peak ${peakDb.toFixed(1)} dBFS${warning}`;
      quality.frame = requestAnimationFrame(tick);
    };
    quality.frame = requestAnimationFrame(tick);
  }

  const originalPrepareAudioGraph = window.prepareAudioGraph;
  const originalReplaceMicrophone = window.replaceMicrophone;
  const originalStopMicrophone = window.stopMicrophone;
  const originalPreferredRecordingOptions = window.preferredRecordingOptions;

  if (typeof originalPrepareAudioGraph !== 'function'
      || typeof originalReplaceMicrophone !== 'function'
      || typeof originalStopMicrophone !== 'function') {
    if (captureSettings) captureSettings.textContent = '音频质量模块未能绑定录音链。';
    return;
  }

  window.prepareAudioGraph = function prepareAudioGraphQualityProfile(...args) {
    const graph = originalPrepareAudioGraph(...args);
    if (graph && !graph.__frontierQualityProfile) {
      // Record close to unity gain. The compressor is a peak guard, not a
      // loudness processor, so normal vocal dynamics do not pump the level.
      graph.limiter.threshold.value = -1;
      graph.limiter.knee.value = 0;
      graph.limiter.ratio.value = 20;
      graph.limiter.attack.value = 0.002;
      graph.limiter.release.value = 0.06;
      graph.__frontierQualityProfile = true;
    }
    return graph;
  };

  window.replaceMicrophone = function replaceMicrophoneQualityProfile(stream) {
    originalReplaceMicrophone(stream);
    startMeter(stream, window.prepareAudioGraph());
  };

  window.stopMicrophone = function stopMicrophoneQualityProfile(...args) {
    stopMeter();
    return originalStopMicrophone(...args);
  };

  if (typeof originalPreferredRecordingOptions === 'function') {
    window.preferredRecordingOptions = function preferredRecordingQualityProfile() {
      const options = originalPreferredRecordingOptions() || {};
      return {...options, audioBitsPerSecond: 128000};
    };
  }

  window.addEventListener('pagehide', stopMeter);
})();
