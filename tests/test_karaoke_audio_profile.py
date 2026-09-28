from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static/media/karaoke.html").read_text(encoding="utf-8")
QUALITY = (ROOT / "static/js/karaoke-audio-quality.js").read_text(encoding="utf-8")
CORE = (ROOT / "static/js/karaoke.js").read_text(encoding="utf-8")


def test_karaoke_defaults_keep_recording_near_unity_and_monitor_loud():
    assert 'id="voiceValue">100%</output>' in HTML
    assert re.search(r'id="voiceGain"[^>]*max="200"[^>]*value="100"', HTML)
    assert 'id="monitorValue">100%</output>' in HTML
    assert re.search(r'id="monitorGain"[^>]*value="100"', HTML)
    assert re.search(r'id="monitor" type="checkbox" checked', HTML)
    assert not re.search(r'id="aec" type="checkbox" checked', HTML)


def test_quality_profile_turns_limiter_into_peak_guard():
    assert "graph.limiter.threshold.value = -1;" in QUALITY
    assert "graph.limiter.knee.value = 0;" in QUALITY
    assert "graph.limiter.ratio.value = 20;" in QUALITY
    assert "graph.limiter.attack.value = 0.002;" in QUALITY
    assert "graph.limiter.release.value = 0.06;" in QUALITY
    assert "audioBitsPerSecond: 128000" in QUALITY


def test_karaoke_capture_keeps_browser_leveling_disabled():
    assert "noiseSuppression: false" in CORE
    assert "autoGainControl: false" in CORE
    assert "channelCount: 1" in CORE
    assert "echoCancellation: elements.aec.checked" in CORE


def test_quality_profile_exposes_actual_capture_and_input_meter():
    assert 'id="inputMeter"' in HTML
    assert 'id="captureSettings"' in HTML
    assert "getSettings?.()" in QUALITY
    assert "getFloatTimeDomainData" in QUALITY
    assert "RMS ${dbfs(rms).toFixed(1)} dBFS" in QUALITY
    assert "接近削波" in QUALITY


def test_audio_quality_profile_loads_after_karaoke_core():
    core = HTML.index("{{KARAOKE_JS_URL}}")
    profile = HTML.index("/static/js/karaoke-audio-quality.js")
    assert core < profile
