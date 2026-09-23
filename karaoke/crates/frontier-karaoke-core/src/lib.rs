use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
pub enum RecordingState {
    #[default]
    Idle,
    Initializing,
    Recording,
    PreviewReady,
}

impl RecordingState {
    pub fn begin(self) -> Result<Self, &'static str> {
        match self {
            Self::Idle | Self::PreviewReady => Ok(Self::Initializing),
            Self::Initializing | Self::Recording => Err("recording is already active"),
        }
    }

    pub fn activate(self) -> Result<Self, &'static str> {
        match self {
            Self::Initializing => Ok(Self::Recording),
            _ => Err("recording is not initializing"),
        }
    }

    pub fn stop(self) -> Result<Self, &'static str> {
        match self {
            Self::Recording => Ok(Self::PreviewReady),
            _ => Err("recording is not active"),
        }
    }

    pub fn discard(self) -> Self {
        Self::Idle
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct LyricCue {
    pub time: f64,
    pub text: String,
}

pub fn lyric_index(cues: &[LyricCue], current_time: f64) -> Option<usize> {
    cues.partition_point(|cue| cue.time <= current_time + 0.03)
        .checked_sub(1)
}

pub trait AuthProvider {
    fn subject(&self) -> Option<&str>;
}

#[derive(Default)]
pub struct AnonymousAuth;
impl AuthProvider for AnonymousAuth {
    fn subject(&self) -> Option<&str> {
        None
    }
}

pub trait RecordingSink {
    fn replace(&mut self, object_url: String);
    fn current(&self) -> Option<&str>;
    fn clear(&mut self);
}

#[derive(Default)]
pub struct EphemeralRecordingSink(Option<String>);
impl RecordingSink for EphemeralRecordingSink {
    fn replace(&mut self, object_url: String) {
        self.0 = Some(object_url);
    }
    fn current(&self) -> Option<&str> {
        self.0.as_deref()
    }
    fn clear(&mut self) {
        self.0 = None;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recording_requires_valid_transitions() {
        assert_eq!(
            RecordingState::Idle.begin().unwrap(),
            RecordingState::Initializing
        );
        assert!(RecordingState::Initializing.begin().is_err());
        assert_eq!(
            RecordingState::Initializing.activate().unwrap(),
            RecordingState::Recording
        );
        assert!(RecordingState::Idle.activate().is_err());
        assert_eq!(
            RecordingState::Recording.stop().unwrap(),
            RecordingState::PreviewReady
        );
        assert!(RecordingState::Idle.stop().is_err());
    }

    #[test]
    fn lyric_clock_selects_latest_cue() {
        let cues = vec![
            LyricCue {
                time: 1.0,
                text: "one".into(),
            },
            LyricCue {
                time: 2.0,
                text: "two".into(),
            },
        ];
        assert_eq!(lyric_index(&cues, 0.5), None);
        assert_eq!(lyric_index(&cues, 1.99), Some(1));
    }
}
