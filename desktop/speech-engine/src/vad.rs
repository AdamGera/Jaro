//! Lightweight Voice Activity Detection replacing the 357 MB PyTorch CTC
//! speech screen for dictation. Uses Silero VAD GGUF on CPU when available
//! (loads in ~20 ms, ~2 MB RAM), with a DC-removed RMS energy fallback.

use std::ops::Range;
use std::path::Path;

use anyhow::Result;
use transcribe_cpp::{Backend, Model, ModelOptions, VadOptions, VadSession};

use crate::pause_segmenter::SAMPLE_RATE;

/// Tuned Silero thresholds: keeps short utterances ("Ok", "Yes")
/// and quiet whispers while rejecting keyboard clicks and room noise.
const MIN_SPEECH_MS: i32 = 160;
const THRESHOLD: f64 = 0.4;

pub struct SpeechDetector {
    session: Option<VadSession>,
}

impl SpeechDetector {
    pub fn none() -> Self {
        Self { session: None }
    }

    pub fn load_cpu(path: &Path) -> Result<Self> {
        let model = Model::load_with(
            path,
            &ModelOptions {
                backend: Backend::Cpu,
                ..Default::default()
            },
        )?;
        let session = model.vad_session()?;
        Ok(Self {
            session: Some(session),
        })
    }

    pub fn has_silero(&self) -> bool {
        self.session.is_some()
    }

    /// Returns whether `samples` (16 kHz mono f32) likely contain speech.
    pub fn contains_speech(&mut self, samples: &[f32]) -> bool {
        if !has_energy_signal(samples) {
            return false;
        }
        let Some(session) = self.session.as_mut() else {
            return true;
        };
        let options = VadOptions {
            threshold: THRESHOLD,
            min_speech_ms: MIN_SPEECH_MS,
            ..VadOptions::default()
        };
        match session.run(samples, &options) {
            Ok(result) => !result.segments.is_empty(),
            Err(error) => {
                tracing::warn!(%error, "Silero VAD failed; falling back to energy gate");
                true
            }
        }
    }

    /// Returns `[first_speech_sample..last_speech_sample]` when Silero is loaded
    /// and hears speech. Useful for trimming silent edges before Whisper runs.
    #[allow(dead_code)]
    pub fn speech_bounds(&mut self, samples: &[f32]) -> Option<Range<usize>> {
        let session = self.session.as_mut()?;
        let options = VadOptions {
            threshold: THRESHOLD,
            min_speech_ms: MIN_SPEECH_MS,
            ..VadOptions::default()
        };
        let segments = session.run(samples, &options).ok()?.segments;
        if segments.is_empty() {
            return None;
        }
        let first = segments
            .iter()
            .map(|s| s.start_sample)
            .min()
            .unwrap_or(0)
            .max(0) as usize;
        let last = segments
            .iter()
            .map(|s| s.end_sample)
            .max()
            .unwrap_or(0)
            .max(0) as usize;
        let start = first.min(samples.len());
        let end = last.min(samples.len());
        (start < end).then_some(start..end)
    }
}

/// Fast DC-removed windowed variance check (same -60 dBFS floor as
/// `mobile/rumble/jaro_rumble.cpp`), rejecting pure silence or DC bias.
pub fn has_energy_signal(samples: &[f32]) -> bool {
    if samples.len() < SAMPLE_RATE / 10 {
        return false;
    }
    let window = SAMPLE_RATE / 20; // 50 ms
    const MIN_VARIANCE: f64 = 1e-6; // -60 dBFS RMS
    for chunk in samples.chunks(window) {
        if chunk.is_empty() {
            continue;
        }
        let mut sum = 0.0f64;
        let mut sq_sum = 0.0f64;
        for &v in chunk {
            if !v.is_finite() {
                return false;
            }
            let d = f64::from(v);
            sum += d;
            sq_sum += d * d;
        }
        let n = chunk.len() as f64;
        let mean = sum / n;
        if sq_sum / n - mean * mean >= MIN_VARIANCE {
            return true;
        }
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn silence_and_dc_offset_are_rejected() {
        assert!(!has_energy_signal(&vec![0.0; 16_000]));
        assert!(!has_energy_signal(&vec![0.35; 16_000]));
    }

    #[test]
    fn audible_tone_passes_energy_gate() {
        let tone: Vec<f32> = (0..16_000)
            .map(|i| 0.05 * (i as f32 * 0.1).sin())
            .collect();
        assert!(has_energy_signal(&tone));
    }
}
