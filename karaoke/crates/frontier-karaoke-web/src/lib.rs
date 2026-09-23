#![cfg(target_arch = "wasm32")]

use std::cell::{Cell, RefCell};
use std::rc::Rc;

use frontier_karaoke_api_client::{KaraokeContext, MediaType};
use frontier_karaoke_audio::browser::AudioSession;
use frontier_karaoke_core::{lyric_index, LyricCue, RecordingState};
use js_sys::{Array, Function, Object, Promise, Reflect};
use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;
use wasm_bindgen_futures::{spawn_local, JsFuture};
use web_sys::{
    Blob, BlobEvent, Document, Element, Event, HtmlAudioElement, HtmlButtonElement,
    HtmlInputElement, HtmlMediaElement, HtmlOptionElement, HtmlSelectElement, MediaDeviceInfo,
    MediaDeviceKind, MediaRecorder, MediaStream, MediaStreamConstraints, Url, UrlSearchParams,
};

const MAX_RECORDING_BYTES: u64 = 256 * 1024 * 1024;

struct App {
    document: Document,
    media: HtmlMediaElement,
    preview: HtmlAudioElement,
    lyrics: RefCell<Vec<LyricCue>>,
    audio: RefCell<Option<AudioSession>>,
    microphone: RefCell<Option<MediaStream>>,
    recorder: RefCell<Option<MediaRecorder>>,
    chunks: Rc<RefCell<Vec<Blob>>>,
    recorded_bytes: Rc<Cell<u64>>,
    preview_url: RefCell<Option<String>>,
    recording_state: Cell<RecordingState>,
    accompaniment: Cell<bool>,
}

fn js_error(value: JsValue) -> String {
    value.as_string().unwrap_or_else(|| format!("{value:?}"))
}

impl App {
    fn by_id<T: JsCast>(&self, id: &str) -> T {
        self.document
            .get_element_by_id(id)
            .unwrap_or_else(|| panic!("missing #{id}"))
            .dyn_into()
            .unwrap()
    }

    fn status(&self, text: &str, error: bool) {
        let element: web_sys::HtmlElement = self.by_id("status");
        element.set_text_content(Some(text));
        let _ = element
            .style()
            .set_property("color", if error { "#ff9f9f" } else { "" });
    }

    fn render_lyrics(&self) {
        let cues = self.lyrics.borrow();
        let active = lyric_index(&cues, self.media.current_time());
        for container_id in ["lyrics", "overlayLines"] {
            let container = self.document.get_element_by_id(container_id).unwrap();
            container.set_inner_html("");
            if cues.is_empty() {
                let line = self.document.create_element("p").unwrap();
                line.set_text_content(Some("当前媒体没有已关联歌词。"));
                let _ = container.append_child(&line);
                continue;
            }
            let center = active.unwrap_or(0);
            let start = center.saturating_sub(1);
            for index in start..(start + 4).min(cues.len()) {
                let line = self.document.create_element("p").unwrap();
                line.set_text_content(Some(&cues[index].text));
                if Some(index) == active {
                    line.set_class_name("current");
                }
                let _ = container.append_child(&line);
            }
        }
    }

    fn slider(&self, id: &str) -> f32 {
        self.by_id::<HtmlInputElement>(id).value_as_number() as f32 / 100.0
    }

    fn apply_levels(&self) {
        if let Some(audio) = self.audio.borrow().as_ref() {
            let muted = self.by_id::<HtmlInputElement>("songMute").checked();
            audio.set_song_gain(if muted { 0.0 } else { self.slider("songGain") });
            audio.set_record_gain(self.slider("voiceGain"));
            let monitor = self.by_id::<HtmlInputElement>("monitor").checked();
            audio.set_monitor_gain(if monitor {
                self.slider("monitorGain")
            } else {
                0.0
            });
        }
    }

    async fn microphone_stream(&self) -> Result<MediaStream, JsValue> {
        let devices = web_sys::window().unwrap().navigator().media_devices()?;
        let audio = Object::new();
        let input = self.by_id::<HtmlSelectElement>("inputDevice").value();
        if !input.is_empty() {
            let exact = Object::new();
            Reflect::set(&exact, &"exact".into(), &input.into())?;
            Reflect::set(&audio, &"deviceId".into(), &exact)?;
        }
        Reflect::set(
            &audio,
            &"echoCancellation".into(),
            &self.by_id::<HtmlInputElement>("aec").checked().into(),
        )?;
        Reflect::set(&audio, &"noiseSuppression".into(), &false.into())?;
        Reflect::set(&audio, &"autoGainControl".into(), &false.into())?;
        Reflect::set(&audio, &"channelCount".into(), &1.into())?;
        let constraints = MediaStreamConstraints::new();
        constraints.set_audio(&audio.into());
        constraints.set_video(&false.into());
        JsFuture::from(devices.get_user_media_with_constraints(&constraints)?)
            .await?
            .dyn_into()
    }

    async fn ensure_audio(&self, stream: &MediaStream) -> Result<(), JsValue> {
        if let Some(audio) = self.audio.borrow().as_ref() {
            audio.replace_microphone(stream)?;
        } else {
            let session = AudioSession::build(self.media.clone(), stream).await?;
            *self.audio.borrow_mut() = Some(session);
        }
        self.apply_levels();
        if let Some(audio) = self.audio.borrow().as_ref() {
            audio.set_accompaniment(self.accompaniment.get())?;
        }
        self.apply_output().await?;
        if let Some(audio) = self.audio.borrow().as_ref() {
            JsFuture::from(audio.context.resume()?).await?;
        }
        Ok(())
    }

    async fn apply_output(&self) -> Result<(), JsValue> {
        let output = self.by_id::<HtmlSelectElement>("outputDevice").value();
        if output.is_empty() || self.audio.borrow().is_none() {
            return Ok(());
        }
        let target = JsValue::from(self.audio.borrow().as_ref().unwrap().context.clone());
        let setter = Reflect::get(&target, &"setSinkId".into())?;
        if setter.is_function() {
            let promise: Promise = setter
                .dyn_into::<Function>()?
                .call1(&target, &output.into())?
                .dyn_into()?;
            JsFuture::from(promise).await?;
        }
        Ok(())
    }

    async fn refresh_devices(&self) -> Result<(), JsValue> {
        let devices = web_sys::window().unwrap().navigator().media_devices()?;
        let permission = self.microphone_stream().await?;
        for track in permission.get_tracks().iter() {
            track.dyn_into::<web_sys::MediaStreamTrack>()?.stop();
        }
        let values = JsFuture::from(devices.enumerate_devices()?).await?;
        let entries: Array = values.dyn_into()?;
        let input = self.by_id::<HtmlSelectElement>("inputDevice");
        let output = self.by_id::<HtmlSelectElement>("outputDevice");
        let input_selected = input.value();
        let output_selected = output.value();
        input.set_inner_html("<option value=\"\">系统默认麦克风</option>");
        output.set_inner_html("<option value=\"\">系统默认输出</option>");
        let mut input_number = 1;
        let mut output_number = 1;
        for entry in entries.iter() {
            let device: MediaDeviceInfo = entry.dyn_into()?;
            let (select, fallback) = match device.kind() {
                MediaDeviceKind::Audioinput => {
                    let value = input_number;
                    input_number += 1;
                    (&input, format!("麦克风 {value}"))
                }
                MediaDeviceKind::Audiooutput => {
                    let value = output_number;
                    output_number += 1;
                    (&output, format!("输出设备 {value}"))
                }
                _ => continue,
            };
            let option: HtmlOptionElement = self.document.create_element("option")?.dyn_into()?;
            option.set_value(&device.device_id());
            let label = device.label();
            option.set_text(if label.is_empty() { &fallback } else { &label });
            select.add_with_html_option_element(&option)?;
        }
        input.set_value(&input_selected);
        output.set_value(&output_selected);
        self.status("输入和输出设备列表已刷新。", false);
        Ok(())
    }

    async fn start_recording(self: &Rc<Self>) -> Result<(), JsValue> {
        if self.recording_state.get() == RecordingState::Recording {
            return Ok(());
        }
        self.clear_preview();
        if let Some(stream) = self.microphone.borrow_mut().take() {
            for track in stream.get_tracks().iter() {
                track.dyn_into::<web_sys::MediaStreamTrack>()?.stop();
            }
        }
        let stream = self.microphone_stream().await?;
        self.ensure_audio(&stream).await?;
        *self.microphone.borrow_mut() = Some(stream);
        let record_stream = self.audio.borrow().as_ref().unwrap().record_stream.clone();
        let recorder = MediaRecorder::new_with_media_stream(&record_stream)?;
        self.chunks.borrow_mut().clear();
        self.recorded_bytes.set(0);

        let chunks = self.chunks.clone();
        let bytes = self.recorded_bytes.clone();
        let status = self.document.get_element_by_id("status").unwrap();
        let data_callback = Closure::<dyn FnMut(BlobEvent)>::new(move |event: BlobEvent| {
            let blob = event.data().unwrap();
            let next = bytes.get().saturating_add(blob.size() as u64);
            if next <= MAX_RECORDING_BYTES {
                bytes.set(next);
                chunks.borrow_mut().push(blob);
            } else if let Some(target) = event
                .target()
                .and_then(|value| value.dyn_into::<MediaRecorder>().ok())
            {
                status.set_text_content(Some("录音达到 256MB 安全上限，已自动停止。"));
                let _ = target.stop();
            }
        });
        recorder.set_ondataavailable(Some(data_callback.as_ref().unchecked_ref()));
        data_callback.forget();

        let app = self.clone();
        let stop_callback = Closure::<dyn FnMut(Event)>::new(move |_| {
            app.finish_recording();
        });
        recorder.set_onstop(Some(stop_callback.as_ref().unchecked_ref()));
        stop_callback.forget();
        recorder.start_with_time_slice(1000)?;
        *self.recorder.borrow_mut() = Some(recorder);
        self.recording_state.set(
            self.recording_state
                .get()
                .start()
                .map_err(JsValue::from_str)?,
        );
        self.by_id::<HtmlButtonElement>("record").set_disabled(true);
        self.by_id::<HtmlButtonElement>("stop").set_disabled(false);
        self.by_id::<HtmlInputElement>("aec").set_disabled(true);
        let _ = JsFuture::from(self.media.play()?).await;
        self.status(
            "正在录制纯人声支路；媒体和返听数字信号不会进入录音。",
            false,
        );
        Ok(())
    }

    fn stop_recording(&self) {
        if let Some(recorder) = self.recorder.borrow().as_ref() {
            let _ = recorder.stop();
        }
        self.media.pause().ok();
        if let Some(stream) = self.microphone.borrow_mut().take() {
            for track in stream
                .get_tracks()
                .iter()
                .filter_map(|value| value.dyn_into::<web_sys::MediaStreamTrack>().ok())
            {
                track.stop();
            }
        }
    }

    fn finish_recording(&self) {
        let array = Array::new();
        for chunk in self.chunks.borrow().iter() {
            array.push(chunk);
        }
        if let Ok(blob) = Blob::new_with_blob_sequence(&array) {
            if let Ok(url) = Url::create_object_url_with_blob(&blob) {
                self.preview.set_src(&url);
                self.by_id::<web_sys::HtmlElement>("previewCard")
                    .set_hidden(false);
                *self.preview_url.borrow_mut() = Some(url);
            }
        }
        self.chunks.borrow_mut().clear();
        self.recording_state.set(RecordingState::PreviewReady);
        self.by_id::<HtmlButtonElement>("record")
            .set_disabled(false);
        self.by_id::<HtmlButtonElement>("record")
            .set_text_content(Some("重录"));
        self.by_id::<HtmlButtonElement>("stop").set_disabled(true);
        self.by_id::<HtmlInputElement>("aec").set_disabled(false);
        self.status("录音已停止，仅在当前页面提供试听。", false);
    }

    fn clear_preview(&self) {
        self.preview.pause().ok();
        self.preview.remove_attribute("src").ok();
        if let Some(url) = self.preview_url.borrow_mut().take() {
            Url::revoke_object_url(&url).ok();
        }
        self.by_id::<web_sys::HtmlElement>("previewCard")
            .set_hidden(true);
    }

    fn cleanup(&self) {
        if let Some(recorder) = self.recorder.borrow().as_ref() {
            recorder.set_onstop(None);
        }
        self.stop_recording();
        self.clear_preview();
        self.chunks.borrow_mut().clear();
        if let Some(audio) = self.audio.borrow().as_ref() {
            let _ = audio.context.close();
        }
    }
}

fn install_view(document: &Document) -> Result<(), JsValue> {
    document.body().ok_or_else(|| JsValue::from_str("document body unavailable"))?.set_inner_html(r#"
<main class="shell">
  <header><button id="back" class="quiet">← 返回播放器</button><div><p class="eyebrow">前沿娱乐</p><h1>K歌</h1></div><span class="stateless">本页关闭即清除录音</span></header>
  <section class="song card"><div><span id="kind" class="badge">媒体</span><h2 id="title">正在载入当前媒体…</h2></div><div class="mode"><button id="original" class="selected">原唱</button><button id="accompaniment">伴奏（中置消除）</button></div></section>
  <section class="workspace"><div class="stage card"><video id="media" playsinline crossorigin="anonymous"></video>
    <div id="lyrics" class="lyrics" aria-live="polite"><p>正在载入歌词…</p></div>
    <div class="transport"><button id="play">只播放歌曲</button><button id="record" class="primary">开始 K 歌</button><button id="stop" disabled>停止录音</button><button id="fullLyrics">歌词全屏</button></div>
    <p id="status" class="status">正在检查浏览器实时音频能力…</p></div>
    <aside class="mixer card"><h3>设备与实时音频</h3>
      <label>输入设备<select id="inputDevice"><option value="">系统默认麦克风</option></select></label>
      <label>输出设备<select id="outputDevice"><option value="">系统默认输出</option></select></label>
      <button id="refreshDevices" class="quiet">授权并刷新设备</button>
      <label>媒体播放音量 <output id="songValue">100%</output><input id="songGain" type="range" min="0" max="100" value="100"></label>
      <label class="switch"><input id="songMute" type="checkbox"> 静音媒体</label>
      <label>录音人声增益 <output id="voiceValue">100%</output><input id="voiceGain" type="range" min="0" max="200" value="100"></label>
      <label>返听增益 <output id="monitorValue">20%</output><input id="monitorGain" type="range" min="0" max="200" value="20"></label>
      <label class="switch"><input id="monitor" type="checkbox"> 开启实时返听</label>
      <label class="switch"><input id="aec" type="checkbox" checked> 回声消除</label>
      <p class="route">麦克风 → Rust DSP → 人声录音<br>麦克风 → Rust DSP → 返听<br>媒体 → 输出（不进入录音）</p>
      <p id="capabilities" class="capabilities"></p><p class="warning">推荐使用耳机。扬声器声音仍可能经空气进入麦克风。</p>
    </aside></section>
  <section id="previewCard" class="preview card" hidden><div><h3>本次录音</h3><p>仅存在于当前页面，刷新或关闭后清除。</p></div><audio id="preview" controls></audio></section>
</main><div id="lyricsOverlay" class="overlay"><div id="overlayLines"></div><span>轻按任意位置返回 K 歌</span></div>
"#);
    Ok(())
}

fn event<T: JsCast>(
    element: &T,
    name: &str,
    mut callback: impl 'static + FnMut(Event),
) -> Result<(), JsValue> {
    let closure = Closure::<dyn FnMut(Event)>::new(move |event| callback(event));
    element
        .unchecked_ref::<web_sys::EventTarget>()
        .add_event_listener_with_callback(name, closure.as_ref().unchecked_ref())?;
    closure.forget();
    Ok(())
}

fn bind(app: &Rc<App>) -> Result<(), JsValue> {
    let window = web_sys::window().unwrap();
    let back_window = window.clone();
    event(
        &app.by_id::<HtmlButtonElement>("back"),
        "click",
        move |_| {
            back_window
                .history()
                .and_then(|history| history.back())
                .ok();
        },
    )?;
    let media = app.media.clone();
    let app_play = app.clone();
    event(
        &app.by_id::<HtmlButtonElement>("play"),
        "click",
        move |_| {
            if media.paused() {
                let _ = media.play();
                app_play.status("仅播放媒体，没有录音。", false);
            } else {
                media.pause().ok();
                app_play.status("媒体已暂停。", false);
            }
        },
    )?;
    let record_app = app.clone();
    event(
        &app.by_id::<HtmlButtonElement>("record"),
        "click",
        move |_| {
            let record_app = record_app.clone();
            spawn_local(async move {
                if let Err(error) = record_app.start_recording().await {
                    record_app.status(&format!("无法开始录音：{}", js_error(error)), true);
                }
            });
        },
    )?;
    let stop_app = app.clone();
    event(
        &app.by_id::<HtmlButtonElement>("stop"),
        "click",
        move |_| stop_app.stop_recording(),
    )?;
    let refresh_app = app.clone();
    event(
        &app.by_id::<HtmlButtonElement>("refreshDevices"),
        "click",
        move |_| {
            let refresh_app = refresh_app.clone();
            spawn_local(async move {
                if let Err(error) = refresh_app.refresh_devices().await {
                    refresh_app.status(&format!("无法读取设备：{}", js_error(error)), true);
                }
            });
        },
    )?;
    let output_app = app.clone();
    event(
        &app.by_id::<HtmlSelectElement>("outputDevice"),
        "change",
        move |_| {
            let output_app = output_app.clone();
            spawn_local(async move {
                if let Err(error) = output_app.apply_output().await {
                    output_app.status(&js_error(error), true);
                }
            });
        },
    )?;
    let original_app = app.clone();
    event(
        &app.by_id::<HtmlButtonElement>("original"),
        "click",
        move |_| {
            original_app.accompaniment.set(false);
            original_app
                .by_id::<HtmlButtonElement>("original")
                .set_class_name("selected");
            original_app
                .by_id::<HtmlButtonElement>("accompaniment")
                .set_class_name("");
            if let Some(audio) = original_app.audio.borrow().as_ref() {
                let _ = audio.set_accompaniment(false);
            }
            original_app.status("已选择原唱。", false);
        },
    )?;
    let accompaniment_app = app.clone();
    event(
        &app.by_id::<HtmlButtonElement>("accompaniment"),
        "click",
        move |_| {
            accompaniment_app.accompaniment.set(true);
            accompaniment_app
                .by_id::<HtmlButtonElement>("original")
                .set_class_name("");
            accompaniment_app
                .by_id::<HtmlButtonElement>("accompaniment")
                .set_class_name("selected");
            if let Some(audio) = accompaniment_app.audio.borrow().as_ref() {
                let _ = audio.set_accompaniment(true);
            }
            accompaniment_app.status("已选择中置消除伴奏；不同音源的分离效果会有差异。", false);
        },
    )?;
    for (id, output) in [
        ("songGain", "songValue"),
        ("voiceGain", "voiceValue"),
        ("monitorGain", "monitorValue"),
    ] {
        let level_app = app.clone();
        let output = output.to_string();
        event(&app.by_id::<HtmlInputElement>(id), "input", move |event| {
            let input: HtmlInputElement = event.target().unwrap().dyn_into().unwrap();
            level_app
                .document
                .get_element_by_id(&output)
                .unwrap()
                .set_text_content(Some(&format!("{}%", input.value())));
            level_app.apply_levels();
        })?;
    }
    for id in ["songMute", "monitor"] {
        let level_app = app.clone();
        event(&app.by_id::<HtmlInputElement>(id), "change", move |_| {
            level_app.apply_levels()
        })?;
    }
    let overlay = app.document.get_element_by_id("lyricsOverlay").unwrap();
    let open_overlay = overlay.clone();
    event(
        &app.by_id::<HtmlButtonElement>("fullLyrics"),
        "click",
        move |_| {
            open_overlay.set_class_name("overlay open");
        },
    )?;
    event(&overlay, "click", move |event| {
        event
            .current_target()
            .unwrap()
            .dyn_into::<Element>()
            .unwrap()
            .set_class_name("overlay");
    })?;
    let cleanup_app = app.clone();
    event(&window, "pagehide", move |_| cleanup_app.cleanup())?;
    if let Ok(devices) = window.navigator().media_devices() {
        let device_app = app.clone();
        event(&devices, "devicechange", move |_| {
            device_app.stop_recording();
            device_app.status("音频设备已变化，录音已安全停止；请刷新设备后继续。", true);
        })?;
    }
    Ok(())
}

fn start_lyric_clock(app: Rc<App>) {
    let callback: Rc<RefCell<Option<Closure<dyn FnMut(f64)>>>> = Rc::new(RefCell::new(None));
    let next = callback.clone();
    *callback.borrow_mut() = Some(Closure::new(move |_| {
        app.render_lyrics();
        if let Some(window) = web_sys::window() {
            let _ = window
                .request_animation_frame(next.borrow().as_ref().unwrap().as_ref().unchecked_ref());
        }
    }));
    let _ = web_sys::window()
        .unwrap()
        .request_animation_frame(callback.borrow().as_ref().unwrap().as_ref().unchecked_ref());
}

fn capability_report(app: &App) -> Result<(), JsValue> {
    let global = js_sys::global();
    let navigator = web_sys::window().unwrap().navigator();
    let devices = navigator.media_devices()?;
    let supported = devices.get_supported_constraints();
    let aec = Reflect::get(&supported, &"echoCancellation".into())
        .unwrap_or(JsValue::FALSE)
        .as_bool()
        .unwrap_or(false);
    if !aec {
        app.by_id::<HtmlInputElement>("aec").set_checked(false);
    }
    let audio_context = Reflect::get(&global, &"AudioContext".into()).unwrap_or(JsValue::UNDEFINED);
    let audio_context_prototype =
        Reflect::get(&audio_context, &"prototype".into()).unwrap_or(JsValue::UNDEFINED);
    let output = Reflect::get(&audio_context_prototype, &"setSinkId".into())
        .map(|value| value.is_function())
        .unwrap_or(false);
    app.by_id::<HtmlSelectElement>("outputDevice")
        .set_disabled(!output);
    let required = [
        ("WebAssembly", "WebAssembly"),
        ("AudioContext", "AudioContext"),
        ("AudioWorklet", "AudioWorkletNode"),
        ("MediaRecorder", "MediaRecorder"),
    ];
    let missing: Vec<&str> = required
        .into_iter()
        .filter_map(|(label, key)| {
            Reflect::has(&global, &key.into())
                .ok()
                .filter(|present| !present)
                .map(|_| label)
        })
        .collect();
    if !missing.is_empty() {
        return Err(JsValue::from_str(&format!(
            "当前浏览器缺少：{}",
            missing.join("、")
        )));
    }
    app.by_id::<web_sys::HtmlElement>("capabilities")
        .set_text_content(Some(&format!(
            "AEC {} · 输出设备选择 {} · 触控点 {}",
            if aec { "可用" } else { "降级" },
            if output {
                "可用"
            } else {
                "使用系统默认"
            },
            navigator.max_touch_points()
        )));
    Ok(())
}

async fn initialize(app: Rc<App>) -> Result<(), JsValue> {
    capability_report(&app)?;
    let search = web_sys::window().unwrap().location().search()?;
    let params = UrlSearchParams::new_with_str(&search)?;
    let media_id = params
        .get("media")
        .ok_or_else(|| JsValue::from_str("缺少当前媒体身份"))?;
    let context: KaraokeContext = frontier_karaoke_api_client::context(&media_id).await?;
    app.document
        .get_element_by_id("title")
        .unwrap()
        .set_text_content(Some(&context.title));
    app.document
        .get_element_by_id("kind")
        .unwrap()
        .set_text_content(Some(match context.media_type {
            MediaType::Audio => "音乐",
            MediaType::Video => "视频",
        }));
    app.media.set_src(&context.stream_url);
    if context.media_type == MediaType::Audio {
        app.media.style().set_property("display", "none")?;
    }
    if let Some(url) = context.lyrics_url.as_deref() {
        *app.lyrics.borrow_mut() = frontier_karaoke_api_client::lyrics(url).await?;
    } else {
        app.by_id::<HtmlButtonElement>("fullLyrics")
            .set_disabled(true);
    }
    app.render_lyrics();
    app.status("浏览器能力检查通过。授权设备后即可开始 K 歌。", false);
    Ok(())
}

#[wasm_bindgen(start)]
pub fn start() -> Result<(), JsValue> {
    let window = web_sys::window().ok_or_else(|| JsValue::from_str("window unavailable"))?;
    let document = window
        .document()
        .ok_or_else(|| JsValue::from_str("document unavailable"))?;
    install_view(&document)?;
    let app = Rc::new(App {
        media: document.get_element_by_id("media").unwrap().dyn_into()?,
        preview: document.get_element_by_id("preview").unwrap().dyn_into()?,
        document,
        lyrics: RefCell::new(Vec::new()),
        audio: RefCell::new(None),
        microphone: RefCell::new(None),
        recorder: RefCell::new(None),
        chunks: Rc::new(RefCell::new(Vec::new())),
        recorded_bytes: Rc::new(Cell::new(0)),
        preview_url: RefCell::new(None),
        recording_state: Cell::new(RecordingState::Idle),
        accompaniment: Cell::new(false),
    });
    bind(&app)?;
    start_lyric_clock(app.clone());
    spawn_local(async move {
        if let Err(error) = initialize(app.clone()).await {
            app.status(&js_error(error), true);
        }
    });
    Ok(())
}
