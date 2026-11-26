//! `transcribe-cpp` session owner for local offline inference:
//! - Keeps immutable GGUF weights resident (with ggml's 3-minute Metal residency
//!   heartbeat) while dropping & recreating the native `Session` around every
//!   run and stream so input-sized scratch buffers never stay pinned.
//! - Uses `n_threads = 1` on GPU/iGPU so CPU workers never stall per-token decode.
//! - Skips vocabulary prompts during warm-ups (prevents Qwen3-ASR/Rumble-2-Flash
//!   from echoing terms on silence).
//! - Probes & prewarms streaming graphs with 8 s of silence on load so Metal
//!   compiles every window shape before the first dictation.

use std::borrow::Cow;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail};
use transcribe_cpp::{
    Backend, CancelToken, DeviceType, Feature, Model, ModelOptions,
    ParakeetBufferedStreamOptions, RunOptions, Session, SessionOptions,
    Stream, StreamExtension, StreamOptions, TimestampKind, Transcript,
};

use crate::gguf_header::StreamMenus;
use crate::pause_segmenter::{SAMPLE_RATE, chunk_ranges};

/// 8 s of silence reaches every buffered stream window shape (including 5.6 s
/// left context on Parakeet Unified English) so Metal compiles all shaders at load.
const STREAM_PREWARM_SAMPLES: usize = 8 * SAMPLE_RATE;

/// A run within 60 s leaves Metal weights hot (ggml keeps them resident for 3 min).
const RECENT_RUN: Duration = Duration::from_secs(60);

/// Default max single-window clip length (25 s) for chunked offline runs.
const DEFAULT_MAX_CHUNK_SAMPLES: usize = 25 * SAMPLE_RATE;

/// Parakeet needs >= 1 s of audio + slight trailing silence for closing punctuation.
const MIN_PARAKEET_SAMPLES: usize = SAMPLE_RATE;
const PARAKEET_TRAILING_SILENCE: usize = SAMPLE_RATE / 4; // 250 ms

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RunKind {
    Transcript,
    Warmup,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum VocabularySlot {
    Bias,
    Prompt(usize),
    None,
}

impl VocabularySlot {
    fn of(model: &Model) -> Self {
        let arch = model.arch();
        if arch == "parakeet"
            && model.accepts_ext(
                transcribe_cpp::ExtSlot::Run,
                transcribe_cpp::sys::TRANSCRIBE_EXT_KIND_PARAKEET_VOCABULARY,
            )
        {
            return Self::Bias;
        }
        if !model.supports(Feature::ContextPrompt) {
            return Self::None;
        }
        match arch.as_str() {
            "whisper" => Self::Prompt(600),
            "qwen3_asr" => Self::Prompt(1_200),
            _ => Self::None,
        }
    }
}

#[allow(dead_code)]
#[derive(Clone, Debug)]
pub struct ModelInfo {
    pub path: PathBuf,
    pub architecture: String,
    pub variant: String,
    pub device_kind: String,
    pub device_name: String,
    pub weights_bytes: u64,
    pub native_streaming: bool,
    pub faster_live_text: bool,
    pub supports_language_detect: bool,
}

pub struct GgufSession {
    model: Model,
    session: Option<Session>,
    session_options: SessionOptions,
    base_options: RunOptions,
    last_run: Option<Instant>,
    vocabulary_slot: VocabularySlot,
    info: ModelInfo,
    stream_options: Option<StreamOptions>,
    stream_menus: Option<StreamMenus>,
    max_chunk_samples: usize,
}

impl GgufSession {
    pub fn load(path: &Path, faster_live_text: bool) -> Result<Self> {
        transcribe_cpp::disable_logging();

        let backend = preferred_backend();
        let model = Model::load_with(
            path,
            &ModelOptions {
                backend,
                ..Default::default()
            },
        )
        .or_else(|gpu_err| {
            if backend == Backend::Cpu {
                return Err(gpu_err);
            }
            tracing::warn!(%gpu_err, ?backend, "GPU backend load failed; falling back to CPU");
            Model::load_with(
                path,
                &ModelOptions {
                    backend: Backend::Cpu,
                    ..Default::default()
                },
            )
        })
        .with_context(|| format!("could not load GGUF model from {}", path.display()))?;

        let device = model.device()?;
        let session_options = session_options_for(device.device_type);
        let session = model.session_with(&session_options)?;
        let vocabulary_slot = VocabularySlot::of(&model);
        let architecture = model.arch();
        let variant = model.variant();
        let capabilities = model.capabilities()?;
        let weights_bytes = std::fs::metadata(path).map_or(0, |m| m.len());

        let base_options = RunOptions {
            timestamps: match capabilities.max_timestamp_kind {
                TimestampKind::Word | TimestampKind::Token => TimestampKind::Segment,
                other => other,
            },
            ..Default::default()
        };

        let max_chunk_samples = if capabilities.max_audio_ms > 0 {
            let cap = (capabilities.max_audio_ms as usize) * SAMPLE_RATE / 1_000;
            cap.clamp(10 * SAMPLE_RATE, 30 * SAMPLE_RATE)
        } else {
            DEFAULT_MAX_CHUNK_SAMPLES
        };

        let stream_menus = if architecture == "parakeet" {
            StreamMenus::read(path)
        } else {
            None
        };

        let info = ModelInfo {
            path: path.to_path_buf(),
            architecture,
            variant,
            device_kind: device.kind,
            device_name: device.description,
            weights_bytes,
            native_streaming: false,
            faster_live_text,
            supports_language_detect: capabilities.supports_language_detect,
        };

        let mut this = Self {
            model,
            session: Some(session),
            session_options,
            base_options,
            last_run: None,
            vocabulary_slot,
            info,
            stream_options: None,
            stream_menus,
            max_chunk_samples,
        };

        this.probe_stream(faster_live_text);
        if !this.info.native_streaming {
            // Warm offline graph once at load so shaders are compiled before first dictation.
            let _ = this.prewarm(true);
        }

        tracing::info!(
            arch = %this.info.architecture,
            variant = %this.info.variant,
            device = %this.info.device_kind,
            weights_mb = this.info.weights_bytes / (1024 * 1024),
            streaming = this.info.native_streaming,
            faster_live_text = this.info.faster_live_text,
            "loaded GGUF speech model"
        );

        Ok(this)
    }

    pub fn info(&self) -> &ModelInfo {
        &self.info
    }

    pub fn is_warm(&self) -> bool {
        self.last_run
            .is_some_and(|last| last.elapsed() < RECENT_RUN)
    }

    pub fn stream_options(&self) -> Option<&StreamOptions> {
        self.stream_options.as_ref()
    }

    pub fn set_faster_live_text(&mut self, faster: bool) {
        if self.info.faster_live_text == faster {
            return;
        }
        self.info.faster_live_text = faster;
        if self.info.architecture == "parakeet" && self.stream_options.is_some() {
            self.probe_stream(faster);
        }
    }

    fn probe_stream(&mut self, faster: bool) {
        let caps_ok = self
            .model
            .capabilities()
            .is_ok_and(|c| c.supports_streaming);
        if !caps_ok {
            self.stream_options = None;
            self.info.native_streaming = false;
            return;
        }
        let is_gpu = self
            .model
            .device()
            .is_ok_and(|d| matches!(d.device_type, DeviceType::Gpu | DeviceType::Igpu));
        let force = std::env::var("JARD_FORCE_STREAMING").ok().as_deref() == Some("1");
        if !is_gpu && !force {
            self.stream_options = None;
            self.info.native_streaming = false;
            return;
        }

        let requested = if faster {
            self.stream_menus
                .as_ref()
                .and_then(StreamMenus::faster_ms)
                .map(|(chunk_ms, right_ms)| StreamOptions {
                    family: Some(StreamExtension::ParakeetBuffered(
                        ParakeetBufferedStreamOptions {
                            left_ms: None,
                            chunk_ms: Some(chunk_ms),
                            right_ms: Some(right_ms),
                        },
                    )),
                    ..Default::default()
                })
                .unwrap_or_default()
        } else {
            StreamOptions::default()
        };

        let started = Instant::now();
        let mut chosen = requested;
        let mut probe = self.probe_silence(&chosen);
        if probe.is_err() && chosen.family.is_some() {
            tracing::warn!("requested faster stream geometry rejected; falling back to model default");
            chosen = StreamOptions::default();
            probe = self.probe_silence(&chosen);
        }

        match probe {
            Ok(()) => {
                self.stream_options = Some(chosen);
                self.info.native_streaming = true;
                tracing::info!(
                    prewarm_ms = started.elapsed().as_millis(),
                    faster,
                    "streaming transcription probed and prewarmed"
                );
            }
            Err(error) => {
                tracing::warn!(%error, "streaming probe failed; using offline decode");
                self.stream_options = None;
                self.info.native_streaming = false;
            }
        }
    }

    fn probe_silence(&mut self, stream_options: &StreamOptions) -> transcribe_cpp::Result<()> {
        let cancel = CancelToken::new();
        let silence = vec![0.0f32; STREAM_PREWARM_SAMPLES];
        self.with_stream(None, &[], stream_options, &cancel, |stream| {
            stream.feed(&silence)?;
            stream.finalize().map(|_| ())
        })?
    }

    /// Runs a lightweight silent pass so macOS faults weights back into RAM
    /// while the user is speaking instead of after key-up.
    pub fn prewarm(&mut self, force: bool) -> Result<bool> {
        if !force && self.is_warm() {
            return Ok(false);
        }
        let started = Instant::now();
        let mut silence = vec![0.0f32; SAMPLE_RATE / 2];
        if self.info.architecture == "parakeet" {
            silence.resize(MIN_PARAKEET_SAMPLES, 0.0);
        }
        let options = self.base_options.clone();
        match self.run_raw(&silence, &options, None, RunKind::Warmup) {
            Ok(_) => {}
            Err(transcribe_cpp::Error::OutputTruncated { .. }) => {
                // Model ran the full graph on silence (common on Qwen3-ASR); it is warm.
            }
            Err(error) => return Err(error.into()),
        }
        tracing::info!(
            prewarm_ms = started.elapsed().as_millis(),
            "prewarmed GGUF speech model"
        );
        Ok(true)
    }

    /// Runs `drive` against a native `transcribe_cpp::Stream`, then immediately
    /// drops the `Session` so streaming scratch buffers are freed.
    pub fn with_stream<R>(
        &mut self,
        language: Option<&str>,
        vocabulary: &[String],
        stream_options: &StreamOptions,
        cancel: &CancelToken,
        drive: impl FnOnce(&mut Stream<'_>) -> R,
    ) -> transcribe_cpp::Result<R> {
        let mut session = match self.session.take() {
            Some(s) => s,
            None => self.model.session_with(&self.session_options)?,
        };
        session.set_cancel_token(cancel);
        let run_opts = self.build_run_options(language, vocabulary, true);
        let result = session
            .stream(&run_opts, stream_options)
            .map(|mut stream| drive(&mut stream));
        drop(session);
        if result.is_ok() {
            self.last_run = Some(Instant::now());
        }
        self.session = self.model.session_with(&self.session_options).ok();
        result
    }

    /// Transcribes a whole clip (automatically splitting clips longer than
    /// `max_chunk_samples` at quiet pause boundaries).
    pub fn transcribe_offline(
        &mut self,
        samples: &[f32],
        language: Option<&str>,
        vocabulary: &[String],
        cancel: Option<&CancelToken>,
    ) -> Result<String> {
        if samples.is_empty() {
            return Ok(String::new());
        }
        let run_opts = self.build_run_options(language, vocabulary, false);
        let ranges = chunk_ranges(samples, self.max_chunk_samples);
        let mut parts = Vec::with_capacity(ranges.len());

        for range in ranges {
            if cancel.is_some_and(CancelToken::is_cancelled) {
                bail!("transcription cancelled");
            }
            let slice = &samples[range];
            let prepared = self.pad_clip(slice);
            let transcript = self
                .run_raw(&prepared, &run_opts, cancel, RunKind::Transcript)
                .context("offline transcription failed")?;
            let text = clean_transcript(&transcript.text, run_opts.prompt.as_deref());
            if !text.is_empty() {
                parts.push(text);
            }
        }
        Ok(parts.join(" "))
    }

    fn pad_clip<'a>(&self, samples: &'a [f32]) -> Cow<'a, [f32]> {
        if self.info.architecture != "parakeet" {
            return Cow::Borrowed(samples);
        }
        let target_len = (samples.len() + PARAKEET_TRAILING_SILENCE).max(MIN_PARAKEET_SAMPLES);
        let mut out = Vec::with_capacity(target_len);
        out.extend_from_slice(samples);
        out.resize(target_len, 0.0);
        Cow::Owned(out)
    }

    fn build_run_options(
        &self,
        language: Option<&str>,
        vocabulary: &[String],
        is_stream: bool,
    ) -> RunOptions {
        let mut opts = self.base_options.clone();
        if let Some(lang) = language.map(str::trim).filter(|l| !l.is_empty() && *l != "auto") {
            opts.language = Some(lang.to_string());
        }
        if vocabulary.is_empty() {
            return opts;
        }
        match self.vocabulary_slot {
            VocabularySlot::Bias => {
                opts.vocabulary = vocabulary.to_vec();
            }
            VocabularySlot::Prompt(max_chars) if !is_stream => {
                let prompt = format_vocabulary_prompt(vocabulary, max_chars);
                if !prompt.is_empty() {
                    opts.prompt = Some(prompt);
                }
            }
            _ => {}
        }
        opts
    }

    fn run_raw(
        &mut self,
        samples: &[f32],
        options: &RunOptions,
        cancel: Option<&CancelToken>,
        kind: RunKind,
    ) -> transcribe_cpp::Result<Transcript> {
        let mut session = match self.session.take() {
            Some(s) => s,
            None => self.model.session_with(&self.session_options)?,
        };
        if let Some(token) = cancel {
            session.set_cancel_token(token);
        }
        let effective = if kind == RunKind::Warmup {
            Cow::Owned(RunOptions {
                prompt: None,
                vocabulary: Vec::new(),
                family: None,
                ..options.clone()
            })
        } else {
            Cow::Borrowed(options)
        };

        let result = session.run(samples, &effective);
        // Drop used session immediately so Metal/Vulkan scratch pools return to OS.
        drop(session);

        if result.is_ok()
            || (kind == RunKind::Warmup
                && matches!(result, Err(transcribe_cpp::Error::OutputTruncated { .. })))
        {
            self.last_run = Some(Instant::now());
        }
        self.session = self.model.session_with(&self.session_options).ok();
        result
    }
}

fn preferred_backend() -> Backend {
    if let Ok(forced) = std::env::var("JARD_SPEECH_BACKEND") {
        match forced.to_ascii_lowercase().as_str() {
            "cpu" => return Backend::Cpu,
            "metal" => return Backend::Metal,
            "vulkan" => return Backend::Vulkan,
            _ => {}
        }
    }
    #[cfg(target_os = "macos")]
    {
        Backend::Metal
    }
    #[cfg(any(
        target_os = "linux",
        all(target_os = "windows", target_arch = "x86_64")
    ))]
    {
        Backend::Vulkan
    }
    #[cfg(not(any(
        target_os = "macos",
        target_os = "linux",
        all(target_os = "windows", target_arch = "x86_64")
    )))]
    {
        Backend::Cpu
    }
}

fn session_options_for(device_type: DeviceType) -> SessionOptions {
    match device_type {
        DeviceType::Gpu | DeviceType::Igpu => SessionOptions {
            n_threads: 1,
            ..Default::default()
        },
        _ => SessionOptions::default(),
    }
}

fn format_vocabulary_prompt(terms: &[String], max_chars: usize) -> String {
    let mut out = String::new();
    for term in terms {
        let trimmed = term.trim();
        if trimmed.is_empty() {
            continue;
        }
        let sep = if out.is_empty() { "" } else { ", " };
        if out.len() + sep.len() + trimmed.len() > max_chars {
            break;
        }
        out.push_str(sep);
        out.push_str(trimmed);
    }
    out
}

/// Strips accidental prompt echoes (when an autoregressive decoder repeats the
/// vocabulary list verbatim on short/quiet audio) and trims whitespace.
pub fn clean_transcript(text: &str, prompt: Option<&str>) -> String {
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return String::new();
    }
    if let Some(prompt) = prompt {
        let norm = |s: &str| {
            s.chars()
                .filter(|c| c.is_alphanumeric() || c.is_whitespace())
                .collect::<String>()
                .split_whitespace()
                .map(str::to_lowercase)
                .collect::<Vec<_>>()
        };
        let prompt_words = norm(prompt);
        if prompt_words.len() >= 2 && norm(trimmed) == prompt_words {
            return String::new();
        }
    }
    trimmed.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clean_transcript_drops_exact_vocabulary_echo() {
        assert_eq!(
            clean_transcript("Jaro, Soundfolio, Parakeet", Some("Jaro, Soundfolio, Parakeet")),
            ""
        );
        assert_eq!(
            clean_transcript("Hello from Jaro.", Some("Jaro, Soundfolio, Parakeet")),
            "Hello from Jaro."
        );
    }

    #[test]
    fn vocabulary_prompt_respects_char_limit() {
        let terms = vec!["Alpha".into(), "Beta".into(), "Gamma".into()];
        assert_eq!(format_vocabulary_prompt(&terms, 12), "Alpha, Beta");
    }
}
