//! Adaptive noise-floor pause detector, quiet-window cutter, and in-memory
//! 48 kHz -> 16 kHz FIR decimator.

use std::f64::consts::PI;
use std::ops::Range;
use std::sync::OnceLock;
use std::time::Duration;

pub const SAMPLE_RATE: usize = 16_000;
pub const FRAME_SAMPLES: usize = SAMPLE_RATE / 50; // 20 ms

const SPEECH_MARGIN_DB: f32 = 12.0;
const QUIET_MARGIN_DB: f32 = 4.0;
const MINIMUM_SPEECH_DB: f32 = -55.0;
const QUIET_MINIMUM_DB: f32 = -70.0;
const LOWEST_FLOOR_DB: f32 = -90.0;
const FLOOR_RISE_DB_PER_FRAME: f32 = 0.06;

const MINIMUM_PIECE: Duration = Duration::from_secs(3);
const PAUSE: Duration = Duration::from_millis(550);
const LONG_PIECE: Duration = Duration::from_secs(8);
const LONG_PIECE_PAUSE: Duration = Duration::from_millis(300);

const QUIET_SPAN_SAMPLES: usize = SAMPLE_RATE / 10; // 100 ms
const QUIET_SEARCH_SAMPLES: usize = 5 * SAMPLE_RATE;

#[allow(dead_code)]
#[derive(Clone, Copy, Debug)]
pub struct FrameLevel {
    pub quiet: bool,
    pub speech: bool,
}

#[derive(Default)]
pub struct SpeechFrames {
    floor_db: Option<f32>,
}

impl SpeechFrames {
    pub fn is_speech(&mut self, frame: &[f32]) -> bool {
        self.classify(frame).speech
    }

    pub fn classify(&mut self, frame: &[f32]) -> FrameLevel {
        let (level_db, floor_db) = self.measure(frame);
        FrameLevel {
            quiet: level_db <= (floor_db + QUIET_MARGIN_DB).max(QUIET_MINIMUM_DB),
            speech: level_db > (floor_db + SPEECH_MARGIN_DB).max(MINIMUM_SPEECH_DB),
        }
    }

    fn measure(&mut self, frame: &[f32]) -> (f32, f32) {
        if frame.is_empty() {
            return (LOWEST_FLOOR_DB, LOWEST_FLOOR_DB);
        }
        let count = frame.len() as f32;
        let mean = frame.iter().sum::<f32>() / count;
        let energy = frame
            .iter()
            .map(|&s| {
                let d = s - mean;
                d * d
            })
            .sum::<f32>()
            / count;
        let level_db = if energy.is_finite() {
            10.0 * energy.max(1e-18).log10()
        } else {
            0.0
        };
        let floor_db = match self.floor_db {
            Some(floor_db) if level_db >= floor_db => floor_db + FLOOR_RISE_DB_PER_FRAME,
            _ => level_db.max(LOWEST_FLOOR_DB),
        };
        self.floor_db = Some(floor_db);
        (level_db, floor_db)
    }
}

#[derive(Default)]
pub struct PauseSegmenter {
    speech: SpeechFrames,
    frame: Vec<f32>,
    position: usize,
    piece_start: usize,
    silent_frames: usize,
    heard_speech: bool,
}

impl PauseSegmenter {
    pub fn push(&mut self, mut samples: &[f32]) -> Option<usize> {
        let mut cut = None;
        while !samples.is_empty() {
            let take = samples.len().min(FRAME_SAMPLES - self.frame.len());
            self.frame.extend_from_slice(&samples[..take]);
            samples = &samples[take..];
            if self.frame.len() == FRAME_SAMPLES {
                cut = self.complete_frame().or(cut);
                self.frame.clear();
            }
        }
        cut
    }

    fn complete_frame(&mut self) -> Option<usize> {
        self.position += FRAME_SAMPLES;
        if self.speech.is_speech(&self.frame) {
            self.silent_frames = 0;
            self.heard_speech = true;
            return None;
        }
        self.silent_frames += 1;
        let piece = self.position - self.piece_start;
        let pause = if piece >= duration_samples(LONG_PIECE) {
            LONG_PIECE_PAUSE
        } else {
            PAUSE
        };
        if !self.heard_speech
            || piece < duration_samples(MINIMUM_PIECE)
            || self.silent_frames * FRAME_SAMPLES < duration_samples(pause)
        {
            return None;
        }
        let cut = self.position - self.silent_frames * FRAME_SAMPLES / 2;
        self.piece_start = cut;
        self.silent_frames = 0;
        self.heard_speech = false;
        Some(cut)
    }
}

const fn duration_samples(duration: Duration) -> usize {
    duration.as_millis() as usize * SAMPLE_RATE / 1_000
}

pub fn quiet_cut(samples: &[f32], max_samples: usize) -> usize {
    let max_samples = max_samples.max(1).min(samples.len());
    let search = QUIET_SEARCH_SAMPLES.min(max_samples / 2).max(1);
    let span = QUIET_SPAN_SAMPLES.min(search).max(1);
    let slice = &samples[max_samples - search..max_samples];
    let quiet = max_samples - search + quietest_span(slice, span);
    quiet.max(1)
}

pub fn chunk_ranges(samples: &[f32], max_samples: usize) -> Vec<Range<usize>> {
    let max_samples = max_samples.max(1);
    let mut ranges = Vec::new();
    let mut start = 0;
    while start < samples.len() {
        if samples.len() - start <= max_samples {
            ranges.push(start..samples.len());
            break;
        }
        let cut = start + quiet_cut(&samples[start..], max_samples);
        ranges.push(start..cut);
        start = cut;
    }
    ranges
}

fn quietest_span(samples: &[f32], span: usize) -> usize {
    if samples.is_empty() {
        return 0;
    }
    let span = span.clamp(1, samples.len());
    let sq = |s: f32| f64::from(s) * f64::from(s);
    let mut energy: f64 = samples[..span].iter().copied().map(sq).sum();
    let mut best = energy;
    let mut best_start = 0;
    for next in 1..=samples.len() - span {
        energy += sq(samples[next + span - 1]) - sq(samples[next - 1]);
        if energy <= best {
            best = energy;
            best_start = next;
        }
    }
    best_start + span / 2
}

fn decimate_48k_kernel() -> &'static [f32; 31] {
    static KERNEL: OnceLock<[f32; 31]> = OnceLock::new();
    KERNEL.get_or_init(|| {
        let mut raw = [0.0f64; 31];
        let half = 15.0f64;
        let cutoff = 7_200.0 / 48_000.0;
        let mut sum = 0.0;
        for (i, slot) in raw.iter_mut().enumerate() {
            let x = i as f64 - half;
            let sinc = if x == 0.0 {
                1.0
            } else {
                let arg = 2.0 * PI * cutoff * x;
                arg.sin() / (PI * x / (2.0 * cutoff) * 2.0 * cutoff)
            };
            // Normalized sinc(2 * cutoff * x)
            let norm_sinc = if x == 0.0 {
                1.0
            } else {
                let t = 2.0 * PI * cutoff * x;
                t.sin() / t
            };
            let _ = sinc;
            let hamming = 0.54 - 0.46 * (2.0 * PI * i as f64 / 30.0).cos();
            *slot = norm_sinc * hamming;
            sum += *slot;
        }
        let mut out = [0.0f32; 31];
        for (dst, src) in out.iter_mut().zip(raw.iter()) {
            *dst = (*src / sum) as f32;
        }
        out
    })
}

/// Resamples mono float PCM from `input_rate` to 16 kHz in memory without
/// spawning ffmpeg. Uses a 31-tap Hamming-windowed sinc FIR filter for the
/// exact 48 kHz -> 16 kHz capture path and linear interpolation otherwise.
pub fn resample_to_16k(samples: &[f32], input_rate: u32) -> Vec<f32> {
    if input_rate == SAMPLE_RATE as u32 || samples.is_empty() {
        return samples.to_vec();
    }
    if input_rate == 48_000 {
        let kernel = decimate_48k_kernel();
        let half = kernel.len() / 2;
        let n = samples.len();
        let out_len = n.div_ceil(3);
        let mut out = Vec::with_capacity(out_len);
        for out_idx in 0..out_len {
            let center = out_idx * 3;
            let mut acc = 0.0f32;
            for (k, &weight) in kernel.iter().enumerate() {
                let pos = center as isize + k as isize - half as isize;
                let clamped = pos.clamp(0, (n - 1) as isize) as usize;
                acc += samples[clamped] * weight;
            }
            out.push(acc);
        }
        return out;
    }
    let ratio = input_rate as f64 / SAMPLE_RATE as f64;
    let out_len = ((samples.len() as f64) / ratio).round() as usize;
    let mut out = Vec::with_capacity(out_len);
    for i in 0..out_len {
        let pos = i as f64 * ratio;
        let idx = (pos as usize).min(samples.len() - 1);
        let next = (idx + 1).min(samples.len() - 1);
        let frac = (pos - idx as f64) as f32;
        out.push(samples[idx] * (1.0 - frac) + samples[next] * frac);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decimate_48k_preserves_dc_and_length() {
        let input = vec![0.5f32; 48_000];
        let out = resample_to_16k(&input, 48_000);
        assert_eq!(out.len(), 16_000);
        for &v in &out {
            assert!((v - 0.5).abs() < 1e-4, "unexpected value {v}");
        }
    }

    #[test]
    fn chunk_ranges_cover_entire_clip() {
        let audio = vec![0.01f32; SAMPLE_RATE * 65];
        let ranges = chunk_ranges(&audio, SAMPLE_RATE * 25);
        assert_eq!(ranges.first().unwrap().start, 0);
        assert_eq!(ranges.last().unwrap().end, audio.len());
        for pair in ranges.windows(2) {
            assert_eq!(pair[0].end, pair[1].start);
        }
    }
}
