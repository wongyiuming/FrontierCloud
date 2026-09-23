#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Bus {
    Media,
    VocalRecord,
    VocalMonitor,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GraphPlan {
    pub recorder_inputs: Vec<Bus>,
    pub output_inputs: Vec<Bus>,
}

impl Default for GraphPlan {
    fn default() -> Self {
        Self {
            recorder_inputs: vec![Bus::VocalRecord],
            output_inputs: vec![Bus::Media, Bus::VocalMonitor],
        }
    }
}

impl GraphPlan {
    pub fn recorder_is_vocal_only(&self) -> bool {
        self.recorder_inputs == [Bus::VocalRecord]
            && !self.recorder_inputs.contains(&Bus::Media)
            && !self.recorder_inputs.contains(&Bus::VocalMonitor)
    }
}

#[cfg(target_arch = "wasm32")]
pub mod browser {
    use js_sys::{Object, Reflect, WebAssembly};
    use wasm_bindgen::JsCast;
    use wasm_bindgen::JsValue;
    use wasm_bindgen_futures::JsFuture;
    use web_sys::{
        AudioContext, AudioWorkletNode, AudioWorkletNodeOptions, BiquadFilterNode,
        BiquadFilterType, GainNode, HtmlMediaElement, MediaElementAudioSourceNode, MediaStream,
        MediaStreamAudioDestinationNode, Response,
    };

    async fn load_worklet_module() -> Result<JsValue, JsValue> {
        let window = web_sys::window().ok_or_else(|| JsValue::from_str("window unavailable"))?;
        let response: Response =
            JsFuture::from(window.fetch_with_str("/karaoke/frontier_karaoke_worklet.wasm"))
                .await?
                .dyn_into()?;
        if !response.ok() {
            return Err(JsValue::from_str(&format!(
                "Rust DSP returned HTTP {}",
                response.status()
            )));
        }
        let bytes = JsFuture::from(response.array_buffer()?).await?;
        JsFuture::from(WebAssembly::compile(&bytes)).await
    }

    pub struct AudioSession {
        pub context: AudioContext,
        pub media: HtmlMediaElement,
        pub record_stream: MediaStream,
        _media_source: MediaElementAudioSourceNode,
        high_pass: BiquadFilterNode,
        song_gain: GainNode,
        record_gain: GainNode,
        monitor_gain: GainNode,
        _record_destination: MediaStreamAudioDestinationNode,
        _vocal_worklet: AudioWorkletNode,
    }

    impl AudioSession {
        pub async fn build(
            media: HtmlMediaElement,
            microphone: &MediaStream,
        ) -> Result<Self, JsValue> {
            let context = AudioContext::new()?;
            JsFuture::from(
                context
                    .audio_worklet()?
                    .add_module("/karaoke/audio-worklet.js")?,
            )
            .await?;
            let worklet_module = load_worklet_module().await?;
            let destination = context.destination();
            let song_gain = context.create_gain()?;
            song_gain.connect_with_audio_node(&destination)?;
            let microphone_source = context.create_media_stream_source(microphone)?;
            let high_pass = context.create_biquad_filter()?;
            high_pass.set_type(BiquadFilterType::Highpass);
            high_pass.frequency().set_value(80.0);
            let low_pass = context.create_biquad_filter()?;
            low_pass.set_type(BiquadFilterType::Lowpass);
            low_pass.frequency().set_value(14_000.0);
            let processor_options = Object::new();
            Reflect::set(&processor_options, &"wasmModule".into(), &worklet_module)?;
            let node_options = AudioWorkletNodeOptions::new();
            node_options.set_processor_options(Some(&processor_options));
            let vocal_worklet =
                AudioWorkletNode::new_with_options(&context, "frontier-vocal-dsp", &node_options)?;
            microphone_source.connect_with_audio_node(&high_pass)?;
            high_pass.connect_with_audio_node(&low_pass)?;
            low_pass.connect_with_audio_node(&vocal_worklet)?;
            let record_gain = context.create_gain()?;
            let limiter = context.create_dynamics_compressor()?;
            limiter.threshold().set_value(-3.0);
            limiter.knee().set_value(6.0);
            limiter.ratio().set_value(12.0);
            limiter.attack().set_value(0.003);
            limiter.release().set_value(0.25);
            let record_destination = context.create_media_stream_destination()?;
            vocal_worklet.connect_with_audio_node(&record_gain)?;
            record_gain.connect_with_audio_node(&limiter)?;
            limiter.connect_with_audio_node(&record_destination)?;
            let monitor_gain = context.create_gain()?;
            monitor_gain.gain().set_value(0.0);
            vocal_worklet.connect_with_audio_node(&monitor_gain)?;
            monitor_gain.connect_with_audio_node(&destination)?;
            // Bind the media element only after every fallible graph component
            // is ready. Browsers never allow this association to be repeated.
            let media_source = context.create_media_element_source(&media)?;
            media_source.connect_with_audio_node(&song_gain)?;
            let record_stream = record_destination.stream();
            Ok(Self {
                context,
                media,
                record_stream,
                _media_source: media_source,
                high_pass,
                song_gain,
                record_gain,
                monitor_gain,
                _record_destination: record_destination,
                _vocal_worklet: vocal_worklet,
            })
        }

        pub fn replace_microphone(&self, microphone: &MediaStream) -> Result<(), JsValue> {
            let source = self.context.create_media_stream_source(microphone)?;
            source.connect_with_audio_node(&self.high_pass)?;
            Ok(())
        }

        pub fn set_accompaniment(&self, enabled: bool) -> Result<(), JsValue> {
            self._media_source.disconnect()?;
            if !enabled {
                self._media_source
                    .connect_with_audio_node(&self.song_gain)?;
                return Ok(());
            }
            let splitter = self
                .context
                .create_channel_splitter_with_number_of_outputs(2)?;
            let left = self.context.create_gain()?;
            let right = self.context.create_gain()?;
            right.gain().set_value(-1.0);
            let merger = self
                .context
                .create_channel_merger_with_number_of_inputs(2)?;
            self._media_source.connect_with_audio_node(&splitter)?;
            splitter.connect_with_audio_node_and_output(&left, 0)?;
            splitter.connect_with_audio_node_and_output(&right, 1)?;
            left.connect_with_audio_node_and_output_and_input(&merger, 0, 0)?;
            right.connect_with_audio_node_and_output_and_input(&merger, 0, 0)?;
            left.connect_with_audio_node_and_output_and_input(&merger, 0, 1)?;
            right.connect_with_audio_node_and_output_and_input(&merger, 0, 1)?;
            merger.connect_with_audio_node(&self.song_gain)?;
            Ok(())
        }

        pub fn set_song_gain(&self, value: f32) {
            self.song_gain.gain().set_value(value.clamp(0.0, 1.0));
        }
        pub fn set_record_gain(&self, value: f32) {
            self.record_gain.gain().set_value(value.clamp(0.0, 6.0));
        }
        pub fn set_monitor_gain(&self, value: f32) {
            self.monitor_gain.gain().set_value(value.clamp(0.0, 2.0));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn recorder_accepts_only_vocal_record_bus() {
        assert!(GraphPlan::default().recorder_is_vocal_only());
    }
    #[test]
    fn media_or_monitor_can_never_be_recorder_inputs() {
        for bus in [Bus::Media, Bus::VocalMonitor] {
            assert!(!GraphPlan {
                recorder_inputs: vec![bus],
                output_inputs: vec![]
            }
            .recorder_is_vocal_only());
        }
    }
}
