//! `jaro-speech`: Jaro's lightweight (<1 GB RAM) native Rust + `transcribe.cpp`
//! speech engine supporting Metal/Vulkan acceleration, real-time streaming
//! partials (`Partial { stable, tentative }`), Silero VAD, and per-run scratch
//! rotation.

mod engine;
mod gguf_header;
mod pause_segmenter;
mod server;
mod session;
mod vad;

use std::net::{IpAddr, SocketAddr};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Instant;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};

use crate::engine::{EngineConfig, SpeechEngine};

#[derive(Parser)]
#[command(name = "jaro-speech", about = "Jaro lightweight local speech engine")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Start the local HTTP + SSE dictation and streaming speech server.
    Serve {
        #[arg(long, default_value = "127.0.0.1")]
        host: IpAddr,
        #[arg(long, default_value_t = 8766)]
        port: u16,
        #[arg(long)]
        model: PathBuf,
        #[arg(long)]
        vad: Option<PathBuf>,
        #[arg(long)]
        token_file: Option<PathBuf>,
        /// For a supervised child, publish the selected loopback port and exit when stdin closes.
        #[arg(long)]
        ready_file: Option<PathBuf>,
        #[arg(long, default_value_t = false)]
        faster_live_text: bool,
        #[arg(long)]
        idle_unload_secs: Option<u64>,
    },
    /// Transcribe a WAV file directly (useful for accuracy & speed benchmarks).
    Transcribe {
        #[arg(long)]
        model: PathBuf,
        #[arg(long)]
        wav: PathBuf,
        #[arg(long)]
        vad: Option<PathBuf>,
        #[arg(long)]
        language: Vec<String>,
        #[arg(long, default_value_t = false)]
        stream: bool,
        #[arg(long, default_value_t = false)]
        faster_live_text: bool,
    },
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    let cli = Cli::parse();
    match cli.command {
        Commands::Serve {
            host,
            port,
            model,
            vad,
            token_file,
            ready_file,
            faster_live_text,
            idle_unload_secs,
        } => {
            let token = match token_file {
                Some(path) => Some(
                    std::fs::read_to_string(&path)
                        .with_context(|| format!("reading token file {}", path.display()))?
                        .trim()
                        .to_string(),
                ),
                None => std::env::var("JARD_SPEECH_TOKEN")
                    .ok()
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty()),
            };

            if ready_file.is_some() {
                anyhow::ensure!(host.is_loopback() && token.as_ref().is_some_and(|value| !value.is_empty()),
                    "A managed speech process needs a loopback address and a token.");
                std::thread::spawn(|| {
                    let _ = std::io::copy(&mut std::io::stdin().lock(), &mut std::io::sink());
                    std::process::exit(0);
                });
            }
            let engine = Arc::new(SpeechEngine::start(EngineConfig {
                model_path: model,
                vad_path: vad,
                faster_live_text,
                idle_unload_secs,
            })?);
            let addr = SocketAddr::new(host, port);
            server::serve(addr, engine, token, ready_file).await
        }
        Commands::Transcribe {
            model,
            wav,
            vad,
            language,
            stream,
            faster_live_text,
        } => {
            let load_started = Instant::now();
            let engine = SpeechEngine::start(EngineConfig {
                model_path: model,
                vad_path: vad,
                faster_live_text,
                idle_unload_secs: None,
            })?;
            let load_ms = load_started.elapsed().as_millis();
            let caps = engine.capabilities();

            let bytes = std::fs::read(&wav)
                .with_context(|| format!("reading WAV {}", wav.display()))?;
            let samples = server::decode_wav_to_16k(&bytes)
                .map_err(|e| anyhow::anyhow!(e))?;

            let result = if stream {
                let (id, _cancel, mut events_rx) = {
                    let (id, cancel, tx) = engine.begin_stream(engine::StreamConfig {
                        languages: language.clone(),
                        prompt: None,
                        vocabulary: Vec::new(),
                        partials: true,
                        screen_speech: true,
                    })?;
                    (id, cancel, tx.subscribe())
                };
                // Feed in 200 ms chunks to simulate real-time dictation.
                for chunk in samples.chunks(3_200) {
                    engine.feed_stream(id, chunk.to_vec())?;
                }
                let final_res = engine.finish_stream(id)?;
                let mut partials = 0usize;
                while let Ok(ev) = events_rx.try_recv() {
                    if let engine::StreamEvent::Partial { stable, tentative } = ev {
                        partials += 1;
                        eprintln!("[partial #{partials}] stable={stable:?} tentative={tentative:?}");
                    }
                }
                final_res
            } else {
                engine.transcribe_offline(samples, language, None, Vec::new(), true)?
            };

            let out = serde_json::json!({
                "model": caps.variant,
                "architecture": caps.architecture,
                "device": caps.device_kind,
                "nativeStreaming": caps.native_streaming,
                "weightsBytes": caps.weights_bytes,
                "loadMs": load_ms,
                "durationMs": result.duration_ms,
                "inferenceMs": result.inference_ms,
                "source": result.source,
                "text": result.text,
            });
            println!("{}", serde_json::to_string_pretty(&out)?);
            Ok(())
        }
    }
}
