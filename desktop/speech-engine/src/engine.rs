//! Single-owner speech engine worker managing:
//! - Model residency, prewarming, hot-swapping, and optional idle unloading
//! - Native GPU streaming (`transcribe_cpp::Stream`) for FastConformer models
//! - Pause-aware rolling live preview for non-streaming models (Rumble-2-Flash / Whisper)
//! - Silero VAD + energy speech screening
//! - Structured `Partial { stable, tentative }` streaming updates

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use anyhow::{Result, anyhow};
use serde::Serialize;
use tokio::sync::broadcast;
use transcribe_cpp::CancelToken;

use crate::pause_segmenter::{PauseSegmenter, SAMPLE_RATE};
use crate::session::{GgufSession, ModelInfo};
use crate::vad::SpeechDetector;

const IDLE_POLL: Duration = Duration::from_secs(2);
/// Minimum audio before triggering a rolling preview decode on non-streaming models (~1.4 s).
const ROLLING_MIN_DELTA_SAMPLES: usize = SAMPLE_RATE * 14 / 10;
/// Cap how long a non-streaming clip stays uncommitted before forcing a commit at quiet cut.
const ROLLING_MAX_UNCOMMITTED_SAMPLES: usize = SAMPLE_RATE * 12;

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Partial {
    pub stable: String,
    pub tentative: String,
}

#[derive(Clone, Debug, Serialize)]
#[serde(tag = "type", rename_all = "camelCase")]
pub enum StreamEvent {
    Ready {
        id: u64,
        partials: bool,
        native_streaming: bool,
        model: String,
    },
    Partial {
        stable: String,
        tentative: String,
    },
    Final {
        text: String,
        inference_ms: u64,
        duration_ms: u64,
        source: String,
    },
    Error {
        message: String,
    },
    Cancelled,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EngineCapabilities {
    pub loaded: bool,
    pub warm: bool,
    pub model_path: String,
    pub architecture: String,
    pub variant: String,
    pub device_kind: String,
    pub device_name: String,
    pub weights_bytes: u64,
    pub native_streaming: bool,
    pub faster_live_text: bool,
    pub idle_unload_secs: Option<u64>,
    pub silero_vad: bool,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TranscriptionResult {
    pub text: String,
    pub inference_ms: u64,
    pub duration_ms: u64,
    pub source: String,
}

#[derive(Clone, Debug)]
pub struct StreamConfig {
    pub language: Option<String>,
    pub vocabulary: Vec<String>,
    pub partials: bool,
    pub screen_speech: bool,
}

#[derive(Clone, Debug)]
pub struct EngineConfig {
    pub model_path: PathBuf,
    pub vad_path: Option<PathBuf>,
    pub faster_live_text: bool,
    pub idle_unload_secs: Option<u64>,
}

enum Command {
    Prewarm {
        force: bool,
        reply: Sender<Result<bool>>,
    },
    TranscribeOffline {
        samples: Vec<f32>,
        language: Option<String>,
        vocabulary: Vec<String>,
        screen_speech: bool,
        reply: Sender<Result<TranscriptionResult>>,
    },
    BeginStream {
        id: u64,
        config: StreamConfig,
        cancel: CancelToken,
        events: broadcast::Sender<StreamEvent>,
    },
    FeedStream {
        id: u64,
        samples: Vec<f32>,
    },
    FinishStream {
        id: u64,
        reply: Sender<Result<TranscriptionResult>>,
    },
    CancelStream {
        id: u64,
    },
    UpdateConfig {
        faster_live_text: Option<bool>,
        idle_unload_secs: Option<Option<u64>>,
        model_path: Option<PathBuf>,
        reply: Sender<Result<EngineCapabilities>>,
    },
    Shutdown,
}

pub struct SpeechEngine {
    commands: Sender<Command>,
    capabilities: Arc<Mutex<EngineCapabilities>>,
    next_stream_id: AtomicU64,
    shutting_down: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
}

impl SpeechEngine {
    pub fn start(config: EngineConfig) -> Result<Self> {
        let vad = match config.vad_path.as_deref() {
            Some(path) if path.is_file() => match SpeechDetector::load_cpu(path) {
                Ok(detector) => {
                    tracing::info!(path = %path.display(), "loaded Silero VAD on CPU");
                    detector
                }
                Err(error) => {
                    tracing::warn!(%error, "could not load Silero VAD; using energy gate");
                    SpeechDetector::none()
                }
            },
            _ => SpeechDetector::none(),
        };

        let session = GgufSession::load(&config.model_path, config.faster_live_text)?;
        let initial_caps = snapshot_caps(Some(&session), &config, vad.has_silero());
        let capabilities = Arc::new(Mutex::new(initial_caps));
        let shutting_down = Arc::new(AtomicBool::new(false));
        let (tx, rx) = mpsc::channel();

        let worker_caps = capabilities.clone();
        let worker = thread::Builder::new()
            .name("jaro-speech-worker".into())
            .spawn(move || {
                let mut state = Worker {
                    model_path: config.model_path,
                    faster_live_text: config.faster_live_text,
                    idle_unload_secs: config.idle_unload_secs,
                    session: Some(session),
                    vad,
                    idle_since: Instant::now(),
                    capabilities: worker_caps,
                };
                state.run(rx);
            })?;

        Ok(Self {
            commands: tx,
            capabilities,
            next_stream_id: AtomicU64::new(1),
            shutting_down,
            worker: Some(worker),
        })
    }

    pub fn capabilities(&self) -> EngineCapabilities {
        self.capabilities
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }

    pub fn prewarm_async(&self, force: bool) -> bool {
        let (tx, _rx) = mpsc::channel();
        self.commands.send(Command::Prewarm { force, reply: tx }).is_ok()
    }

    pub fn transcribe_offline(
        &self,
        samples: Vec<f32>,
        language: Option<String>,
        vocabulary: Vec<String>,
        screen_speech: bool,
    ) -> Result<TranscriptionResult> {
        let (tx, rx) = mpsc::channel();
        self.commands
            .send(Command::TranscribeOffline {
                samples,
                language,
                vocabulary,
                screen_speech,
                reply: tx,
            })
            .map_err(|_| anyhow!("speech engine worker stopped"))?;
        rx.recv().map_err(|_| anyhow!("speech engine worker disconnected"))?
    }

    pub fn begin_stream(
        &self,
        config: StreamConfig,
    ) -> Result<(u64, CancelToken, broadcast::Sender<StreamEvent>)> {
        let id = self.next_stream_id.fetch_add(1, Ordering::AcqRel);
        let cancel = CancelToken::new();
        let (events, _) = broadcast::channel(128);
        self.commands
            .send(Command::BeginStream {
                id,
                config,
                cancel: cancel.clone(),
                events: events.clone(),
            })
            .map_err(|_| anyhow!("speech engine worker stopped"))?;
        Ok((id, cancel, events))
    }

    pub fn feed_stream(&self, id: u64, samples: Vec<f32>) -> Result<()> {
        if samples.is_empty() {
            return Ok(());
        }
        self.commands
            .send(Command::FeedStream { id, samples })
            .map_err(|_| anyhow!("speech engine worker stopped"))
    }

    pub fn finish_stream(&self, id: u64) -> Result<TranscriptionResult> {
        let (tx, rx) = mpsc::channel();
        self.commands
            .send(Command::FinishStream { id, reply: tx })
            .map_err(|_| anyhow!("speech engine worker stopped"))?;
        rx.recv().map_err(|_| anyhow!("speech engine worker disconnected"))?
    }

    pub fn cancel_stream(&self, id: u64, cancel: &CancelToken) {
        cancel.cancel();
        let _ = self.commands.send(Command::CancelStream { id });
    }

    pub fn update_config(
        &self,
        faster_live_text: Option<bool>,
        idle_unload_secs: Option<Option<u64>>,
        model_path: Option<PathBuf>,
    ) -> Result<EngineCapabilities> {
        let (tx, rx) = mpsc::channel();
        self.commands
            .send(Command::UpdateConfig {
                faster_live_text,
                idle_unload_secs,
                model_path,
                reply: tx,
            })
            .map_err(|_| anyhow!("speech engine worker stopped"))?;
        rx.recv().map_err(|_| anyhow!("speech engine worker disconnected"))?
    }
}

impl Drop for SpeechEngine {
    fn drop(&mut self) {
        self.shutting_down.store(true, Ordering::Release);
        let _ = self.commands.send(Command::Shutdown);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

struct Worker {
    model_path: PathBuf,
    faster_live_text: bool,
    idle_unload_secs: Option<u64>,
    session: Option<GgufSession>,
    vad: SpeechDetector,
    idle_since: Instant,
    capabilities: Arc<Mutex<EngineCapabilities>>,
}

impl Worker {
    fn run(&mut self, rx: Receiver<Command>) {
        loop {
            let cmd = match rx.recv_timeout(IDLE_POLL) {
                Ok(cmd) => cmd,
                Err(RecvTimeoutError::Timeout) => {
                    if let Some(limit) = self.idle_unload_secs
                        && self.session.is_some()
                        && self.idle_since.elapsed() >= Duration::from_secs(limit)
                    {
                        tracing::info!(idle_secs = limit, "unloading idle speech model to free RAM");
                        self.session = None;
                        self.publish_caps();
                    }
                    continue;
                }
                Err(RecvTimeoutError::Disconnected) => return,
            };

            match cmd {
                Command::Shutdown => return,
                Command::Prewarm { force, reply } => {
                    let result = self
                        .ensure_session()
                        .and_then(|session| session.prewarm(force));
                    self.idle_since = Instant::now();
                    self.publish_caps();
                    let _ = reply.send(result);
                }
                Command::TranscribeOffline {
                    samples,
                    language,
                    vocabulary,
                    screen_speech,
                    reply,
                } => {
                    let result = self.run_offline(
                        &samples,
                        language.as_deref(),
                        &vocabulary,
                        screen_speech,
                    );
                    self.idle_since = Instant::now();
                    self.publish_caps();
                    let _ = reply.send(result);
                }
                Command::BeginStream {
                    id,
                    config,
                    cancel,
                    events,
                } => {
                    self.handle_stream(id, config, cancel, events, &rx);
                    self.idle_since = Instant::now();
                    self.publish_caps();
                }
                Command::UpdateConfig {
                    faster_live_text,
                    idle_unload_secs,
                    model_path,
                    reply,
                } => {
                    if let Some(v) = idle_unload_secs {
                        self.idle_unload_secs = v;
                    }
                    if let Some(faster) = faster_live_text {
                        self.faster_live_text = faster;
                        if let Some(session) = self.session.as_mut() {
                            session.set_faster_live_text(faster);
                        }
                    }
                    let mut result = Ok(());
                    if let Some(new_path) = model_path
                        && new_path != self.model_path
                    {
                        // Drop old model first to stay <1 GB peak during swap.
                        self.session = None;
                        self.model_path = new_path;
                        if let Err(err) = self.ensure_session() {
                            result = Err(err);
                        }
                    }
                    self.publish_caps();
                    let caps = self.capabilities.lock().unwrap_or_else(|e| e.into_inner()).clone();
                    let _ = reply.send(result.map(|_| caps));
                }
                Command::FeedStream { .. } | Command::CancelStream { .. } => {}
                Command::FinishStream { reply, .. } => {
                    let _ = reply.send(Err(anyhow!("stream is no longer active")));
                }
            }
        }
    }

    fn ensure_session(&mut self) -> Result<&mut GgufSession> {
        if self.session.is_none() {
            let loaded = GgufSession::load(&self.model_path, self.faster_live_text)?;
            self.session = Some(loaded);
        }
        Ok(self.session.as_mut().expect("session just loaded"))
    }

    fn publish_caps(&self) {
        let cfg = EngineConfig {
            model_path: self.model_path.clone(),
            vad_path: None,
            faster_live_text: self.faster_live_text,
            idle_unload_secs: self.idle_unload_secs,
        };
        let next = snapshot_caps(self.session.as_ref(), &cfg, self.vad.has_silero());
        *self.capabilities.lock().unwrap_or_else(|e| e.into_inner()) = next;
    }

    fn run_offline(
        &mut self,
        samples: &[f32],
        language: Option<&str>,
        vocabulary: &[String],
        screen_speech: bool,
    ) -> Result<TranscriptionResult> {
        let duration_ms = (samples.len() as u64) * 1_000 / (SAMPLE_RATE as u64);
        if screen_speech && !self.vad.contains_speech(samples) {
            return Ok(TranscriptionResult {
                text: String::new(),
                inference_ms: 0,
                duration_ms,
                source: "vad-silence".into(),
            });
        }
        let started = Instant::now();
        let session = self.ensure_session()?;
        let text = session.transcribe_offline(samples, language, vocabulary, None)?;
        Ok(TranscriptionResult {
            text,
            inference_ms: started.elapsed().as_millis() as u64,
            duration_ms,
            source: "offline".into(),
        })
    }

    fn handle_stream(
        &mut self,
        id: u64,
        config: StreamConfig,
        cancel: CancelToken,
        events: broadcast::Sender<StreamEvent>,
        rx: &Receiver<Command>,
    ) {
        let (native_stream_opts, model_name) = match self.ensure_session() {
            Ok(session) => (
                session.stream_options().cloned(),
                session.info().variant.clone(),
            ),
            Err(err) => {
                let _ = events.send(StreamEvent::Error {
                    message: format!("{err:#}"),
                });
                return;
            }
        };

        let _ = events.send(StreamEvent::Ready {
            id,
            partials: config.partials,
            native_streaming: native_stream_opts.is_some(),
            model: model_name,
        });

        if let Some(stream_opts) = native_stream_opts {
            self.drive_native_stream(id, config, stream_opts, cancel, events, rx);
        } else {
            self.drive_rolling_stream(id, config, cancel, events, rx);
        }
    }

    /// Native continuous GPU streaming (Parakeet Unified English / Nemotron 3.5).
    /// Feeds audio chunks directly as you speak and emits `Partial { stable, tentative }`.
    fn drive_native_stream(
        &mut self,
        id: u64,
        config: StreamConfig,
        stream_opts: transcribe_cpp::StreamOptions,
        cancel: CancelToken,
        events: broadcast::Sender<StreamEvent>,
        rx: &Receiver<Command>,
    ) {
        let mut all_samples: Vec<f32> = Vec::new();
        let mut last_partial = Partial::default();
        let mut finish_reply: Option<Sender<Result<TranscriptionResult>>> = None;

        let session = match self.session.as_mut() {
            Some(s) => s,
            None => return,
        };

        let mut finish_started = Instant::now();
        let stream_outcome = session.with_stream(
            config.language.as_deref(),
            &config.vocabulary,
            &stream_opts,
            &cancel,
            |stream| -> Result<Option<String>> {
                loop {
                    match rx.recv() {
                        Ok(Command::FeedStream {
                            id: cmd_id,
                            samples,
                        }) if cmd_id == id => {
                            all_samples.extend_from_slice(&samples);
                            stream.feed(&samples)?;
                            if config.partials {
                                let text = stream.text();
                                let next = Partial {
                                    stable: text.committed.trim_start().to_string(),
                                    tentative: text.tentative,
                                };
                                if next != last_partial {
                                    last_partial = next.clone();
                                    let _ = events.send(StreamEvent::Partial {
                                        stable: next.stable,
                                        tentative: next.tentative,
                                    });
                                }
                            }
                        }
                        Ok(Command::FinishStream {
                            id: cmd_id,
                            reply,
                        }) if cmd_id == id => {
                            finish_started = Instant::now();
                            finish_reply = Some(reply);
                            // Trailing 250 ms silence helps FastConformer emit closing punctuation.
                            let _ = stream.feed(&vec![0.0f32; SAMPLE_RATE / 4]);
                            stream.finalize()?;
                            let text = stream.text().full.trim().to_string();
                            return Ok(Some(text));
                        }
                        Ok(Command::CancelStream { id: cmd_id }) if cmd_id == id => {
                            let _ = events.send(StreamEvent::Cancelled);
                            return Ok(None);
                        }
                        Ok(Command::Shutdown) | Err(_) => {
                            let _ = events.send(StreamEvent::Cancelled);
                            return Ok(None);
                        }
                        Ok(_) => {
                            // Ignore unrelated/stale stream commands while this dictation owns the GPU.
                        }
                    }
                }
            },
        );

        let Some(reply) = finish_reply else {
            return;
        };

        let duration_ms = (all_samples.len() as u64) * 1_000 / (SAMPLE_RATE as u64);
        if config.screen_speech && !self.vad.contains_speech(&all_samples) {
            let res = TranscriptionResult {
                text: String::new(),
                inference_ms: 0,
                duration_ms,
                source: "vad-silence".into(),
            };
            let _ = events.send(StreamEvent::Final {
                text: res.text.clone(),
                inference_ms: res.inference_ms,
                duration_ms: res.duration_ms,
                source: res.source.clone(),
            });
            let _ = reply.send(Ok(res));
            return;
        }

        match stream_outcome {
            Ok(Ok(Some(text))) if !text.is_empty() => {
                let res = TranscriptionResult {
                    text,
                    inference_ms: finish_started.elapsed().as_millis() as u64,
                    duration_ms,
                    source: "stream".into(),
                };
                let _ = events.send(StreamEvent::Final {
                    text: res.text.clone(),
                    inference_ms: res.inference_ms,
                    duration_ms: res.duration_ms,
                    source: res.source.clone(),
                });
                let _ = reply.send(Ok(res));
            }
            _ => {
                // Fallback to whole-clip offline decode so nothing is ever lost.
                let fallback = self.run_offline(
                    &all_samples,
                    config.language.as_deref(),
                    &config.vocabulary,
                    false,
                );
                match fallback {
                    Ok(mut res) => {
                        res.source = "fallback".into();
                        let _ = events.send(StreamEvent::Final {
                            text: res.text.clone(),
                            inference_ms: res.inference_ms,
                            duration_ms: res.duration_ms,
                            source: res.source.clone(),
                        });
                        let _ = reply.send(Ok(res));
                    }
                    Err(err) => {
                        let msg = format!("{err:#}");
                        let _ = events.send(StreamEvent::Error {
                            message: msg.clone(),
                        });
                        let _ = reply.send(Err(anyhow!(msg)));
                    }
                }
            }
        }
    }

    /// Pause-aware rolling live preview for non-streaming models (Rumble-2-Flash / Qwen3-ASR / Whisper).
    /// When `config.partials` is ON, commits pause-delimited clauses into `stable`
    /// and decodes the uncommitted tail into `tentative`, avoiding full re-decoding
    /// of 30–60 s dictations on release. When `config.partials` is OFF, does ZERO
    /// preview decodes during speech and transcribes the full clip cleanly on finish.
    fn drive_rolling_stream(
        &mut self,
        id: u64,
        config: StreamConfig,
        cancel: CancelToken,
        events: broadcast::Sender<StreamEvent>,
        rx: &Receiver<Command>,
    ) {
        let mut all_samples: Vec<f32> = Vec::new();
        let mut segmenter = PauseSegmenter::default();
        let mut committed_through: usize = 0;
        let mut last_preview_len: usize = 0;
        let mut committed_parts: Vec<String> = Vec::new();

        loop {
            match rx.recv() {
                Ok(Command::FeedStream {
                    id: cmd_id,
                    samples,
                }) if cmd_id == id => {
                    all_samples.extend_from_slice(&samples);
                    if !config.partials {
                        continue;
                    }

                    // Check if a natural pause closed a stable clause.
                    let pause_cut = segmenter.push(&samples);
                    if let Some(cut) = pause_cut
                        && cut > committed_through + SAMPLE_RATE
                        && cut <= all_samples.len()
                    {
                        let clause = &all_samples[committed_through..cut];
                        if self.vad.contains_speech(clause)
                            && let Some(session) = self.session.as_mut()
                            && let Ok(text) = session.transcribe_offline(
                                clause,
                                config.language.as_deref(),
                                &config.vocabulary,
                                Some(&cancel),
                            )
                        {
                            let trimmed = text.trim();
                            if !trimmed.is_empty() {
                                committed_parts.push(trimmed.to_string());
                            }
                            committed_through = cut;
                            last_preview_len = cut;
                            let _ = events.send(StreamEvent::Partial {
                                stable: committed_parts.join(" "),
                                tentative: String::new(),
                            });
                            continue;
                        }
                    }

                    // Otherwise decode uncommitted tail periodically for tentative live words.
                    let uncommitted_len = all_samples.len().saturating_sub(committed_through);
                    if all_samples.len().saturating_sub(last_preview_len)
                        >= ROLLING_MIN_DELTA_SAMPLES
                        && uncommitted_len <= ROLLING_MAX_UNCOMMITTED_SAMPLES
                    {
                        let tail = &all_samples[committed_through..];
                        if self.vad.contains_speech(tail)
                            && let Some(session) = self.session.as_mut()
                            && let Ok(tentative) = session.transcribe_offline(
                                tail,
                                config.language.as_deref(),
                                &config.vocabulary,
                                Some(&cancel),
                            )
                        {
                            last_preview_len = all_samples.len();
                            let stable = committed_parts.join(" ");
                            let tentative = if stable.is_empty() || tentative.is_empty() {
                                tentative
                            } else {
                                format!(" {tentative}")
                            };
                            let _ = events.send(StreamEvent::Partial { stable, tentative });
                        }
                    }
                }
                Ok(Command::FinishStream {
                    id: cmd_id,
                    reply,
                }) if cmd_id == id => {
                    let started = Instant::now();
                    let duration_ms = (all_samples.len() as u64) * 1_000 / (SAMPLE_RATE as u64);

                    let result = if config.screen_speech && !self.vad.contains_speech(&all_samples)
                    {
                        Ok(TranscriptionResult {
                            text: String::new(),
                            inference_ms: 0,
                            duration_ms,
                            source: "vad-silence".into(),
                        })
                    } else if config.partials && committed_through > 0 {
                        // Only decode the final uncommitted tail for long dictations!
                        let tail = &all_samples[committed_through..];
                        let tail_text = if tail.len() >= SAMPLE_RATE / 5
                            && self.vad.contains_speech(tail)
                        {
                            self.ensure_session().and_then(|s| {
                                s.transcribe_offline(
                                    tail,
                                    config.language.as_deref(),
                                    &config.vocabulary,
                                    None,
                                )
                            })
                        } else {
                            Ok(String::new())
                        };
                        tail_text.map(|t| {
                            if !t.trim().is_empty() {
                                committed_parts.push(t.trim().to_string());
                            }
                            TranscriptionResult {
                                text: committed_parts.join(" "),
                                inference_ms: started.elapsed().as_millis() as u64,
                                duration_ms,
                                source: "rolling-tail".into(),
                            }
                        })
                    } else {
                        self.run_offline(
                            &all_samples,
                            config.language.as_deref(),
                            &config.vocabulary,
                            false,
                        )
                    };

                    match result {
                        Ok(res) => {
                            let _ = events.send(StreamEvent::Final {
                                text: res.text.clone(),
                                inference_ms: res.inference_ms,
                                duration_ms: res.duration_ms,
                                source: res.source.clone(),
                            });
                            let _ = reply.send(Ok(res));
                        }
                        Err(err) => {
                            let msg = format!("{err:#}");
                            let _ = events.send(StreamEvent::Error {
                                message: msg.clone(),
                            });
                            let _ = reply.send(Err(anyhow!(msg)));
                        }
                    }
                    return;
                }
                Ok(Command::CancelStream { id: cmd_id }) if cmd_id == id => {
                    let _ = events.send(StreamEvent::Cancelled);
                    return;
                }
                Ok(Command::Shutdown) | Err(_) => {
                    let _ = events.send(StreamEvent::Cancelled);
                    return;
                }
                Ok(_) => {}
            }
        }
    }
}

fn snapshot_caps(
    session: Option<&GgufSession>,
    config: &EngineConfig,
    silero_vad: bool,
) -> EngineCapabilities {
    if let Some(session) = session {
        let ModelInfo {
            path,
            architecture,
            variant,
            device_kind,
            device_name,
            weights_bytes,
            native_streaming,
            faster_live_text,
            ..
        } = session.info();
        EngineCapabilities {
            loaded: true,
            warm: session.is_warm(),
            model_path: path.display().to_string(),
            architecture: architecture.clone(),
            variant: variant.clone(),
            device_kind: device_kind.clone(),
            device_name: device_name.clone(),
            weights_bytes: *weights_bytes,
            native_streaming: *native_streaming,
            faster_live_text: *faster_live_text,
            idle_unload_secs: config.idle_unload_secs,
            silero_vad,
        }
    } else {
        EngineCapabilities {
            loaded: false,
            warm: false,
            model_path: config.model_path.display().to_string(),
            architecture: String::new(),
            variant: String::new(),
            device_kind: "parked".into(),
            device_name: String::new(),
            weights_bytes: 0,
            native_streaming: false,
            faster_live_text: config.faster_live_text,
            idle_unload_secs: config.idle_unload_secs,
            silero_vad,
        }
    }
}
