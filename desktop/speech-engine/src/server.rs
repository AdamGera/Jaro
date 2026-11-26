//! Local HTTP + SSE server for `jaro-speech`, compatible with Jaro's
//! `X-Jard-Token` / `Authorization: Bearer` authentication and `/streams`
//! real-time streaming model.

use std::collections::HashMap;
use std::convert::Infallible;
use std::io::Cursor;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::Result;
use axum::body::Bytes;
use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::Json;
use axum::routing::{get, post};
use axum::Router;
use futures_util::stream::{self, Stream};
use serde::{Deserialize, Serialize};
use subtle::ConstantTimeEq;
use tokio::sync::broadcast;
use tokio_stream::StreamExt;
use tokio_stream::wrappers::BroadcastStream;
use transcribe_cpp::CancelToken;

use crate::engine::{EngineCapabilities, SpeechEngine, StreamConfig, StreamEvent};
use crate::pause_segmenter::{SAMPLE_RATE, resample_to_16k};

const STREAM_LEASE: Duration = Duration::from_secs(15);

struct ActiveStream {
    owner_token: String,
    sample_rate: u32,
    sample_format: SampleFormat,
    cancel: CancelToken,
    events: broadcast::Sender<StreamEvent>,
    last_activity: Instant,
    finished: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum SampleFormat {
    F32le,
    S16le,
}

impl SampleFormat {
    fn decode(self, bytes: &[u8]) -> Option<Vec<f32>> {
        match self {
            Self::F32le => {
                if !bytes.len().is_multiple_of(4) {
                    return None;
                }
                let mut out = Vec::with_capacity(bytes.len() / 4);
                for chunk in bytes.chunks_exact(4) {
                    let v = f32::from_le_bytes(chunk.try_into().ok()?);
                    if !v.is_finite() {
                        return None;
                    }
                    out.push(v.clamp(-1.0, 1.0));
                }
                Some(out)
            }
            Self::S16le => {
                if !bytes.len().is_multiple_of(2) {
                    return None;
                }
                let mut out = Vec::with_capacity(bytes.len() / 2);
                for chunk in bytes.chunks_exact(2) {
                    let v = i16::from_le_bytes(chunk.try_into().ok()?);
                    out.push(f32::from(v) / 32_768.0);
                }
                Some(out)
            }
        }
    }
}

pub struct AppState {
    engine: Arc<SpeechEngine>,
    token: Option<String>,
    streams: Mutex<HashMap<u64, ActiveStream>>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct CreateStreamRequest {
    #[serde(default = "default_sample_rate")]
    sample_rate: u32,
    #[serde(default = "default_sample_format")]
    sample_format: SampleFormat,
    language: Option<String>,
    #[serde(default)]
    vocabulary: Vec<String>,
    #[serde(default)]
    partials: bool,
    #[serde(default = "default_true")]
    screen_speech: bool,
}

fn default_sample_rate() -> u32 {
    SAMPLE_RATE as u32
}
fn default_sample_format() -> SampleFormat {
    SampleFormat::F32le
}
fn default_true() -> bool {
    true
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct CreateStreamResponse {
    id: u64,
    owner_token: String,
    partials: bool,
    native_streaming: bool,
    lease_ms: u64,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct TranscribeQuery {
    #[serde(default = "default_sample_rate")]
    sample_rate: u32,
    format: Option<SampleFormat>,
    language: Option<String>,
    #[serde(default = "default_true")]
    screen_speech: bool,
    vocabulary: Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct UpdateConfigRequest {
    faster_live_text: Option<bool>,
    idle_unload_secs: Option<Option<u64>>,
    model_path: Option<PathBuf>,
}

pub async fn serve(
    addr: SocketAddr,
    engine: Arc<SpeechEngine>,
    token: Option<String>,
    ready_file: Option<PathBuf>,
) -> Result<()> {
    let state = Arc::new(AppState {
        engine,
        token,
        streams: Mutex::new(HashMap::new()),
    });

    // Lease reaper for abandoned streams.
    let reaper_state = state.clone();
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(3));
        loop {
            interval.tick().await;
            let expired: Vec<(u64, CancelToken)> = {
                let mut streams = reaper_state.streams.lock().unwrap_or_else(|e| e.into_inner());
                let mut dead = Vec::new();
                streams.retain(|&id, stream| {
                    if !stream.finished && stream.last_activity.elapsed() > STREAM_LEASE {
                        dead.push((id, stream.cancel.clone()));
                        false
                    } else {
                        true
                    }
                });
                dead
            };
            for (id, cancel) in expired {
                tracing::warn!(stream_id = id, "cancelling stream after lease expiry");
                reaper_state.engine.cancel_stream(id, &cancel);
            }
        }
    });

    let router = Router::new()
        .route("/health", get(health))
        .route("/capabilities", get(capabilities))
        .route("/v1/prewarm", post(prewarm))
        .route("/v1/config", post(update_config))
        .route("/v1/transcribe", post(transcribe_offline))
        .route("/v1/streams", post(create_stream))
        .route("/v1/streams/{id}/audio", post(push_stream_audio))
        .route("/v1/streams/{id}/events", get(stream_events))
        .route("/v1/streams/{id}/finish", post(finish_stream))
        .route("/v1/streams/{id}/cancel", post(cancel_stream))
        .route("/v1/streams/{id}/heartbeat", post(heartbeat_stream))
        .with_state(state);

    let listener = tokio::net::TcpListener::bind(addr).await?;
    let addr = listener.local_addr()?;
    if let Some(path) = ready_file {
        let pending = path.with_extension("pending");
        std::fs::write(&pending, addr.port().to_string())?;
        std::fs::rename(pending, path)?;
    }
    tracing::info!(%addr, "jaro-speech listening");
    axum::serve(listener, router)
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
}

fn check_auth(headers: &HeaderMap, expected: Option<&str>) -> Result<(), StatusCode> {
    let Some(expected) = expected else {
        return Ok(());
    };
    let supplied = headers
        .get("x-jard-token")
        .and_then(|v| v.to_str().ok())
        .or_else(|| {
            headers
                .get("authorization")
                .and_then(|v| v.to_str().ok())
                .and_then(|s| s.strip_prefix("Bearer "))
        })
        .unwrap_or("");
    if supplied.as_bytes().ct_eq(expected.as_bytes()).into() {
        Ok(())
    } else {
        Err(StatusCode::UNAUTHORIZED)
    }
}

fn check_stream_owner(headers: &HeaderMap, stream: &ActiveStream) -> Result<(), StatusCode> {
    let supplied = headers
        .get("x-jaro-stream-token")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if supplied
        .as_bytes()
        .ct_eq(stream.owner_token.as_bytes())
        .into()
    {
        Ok(())
    } else {
        Err(StatusCode::NOT_FOUND)
    }
}

async fn health(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<Json<serde_json::Value>, StatusCode> {
    check_auth(&headers, state.token.as_deref())?;
    let caps = state.engine.capabilities();
    Ok(Json(serde_json::json!({
        "status": "ok",
        "loaded": caps.loaded,
        "warm": caps.warm,
        "nativeStreaming": caps.native_streaming,
    })))
}

async fn capabilities(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<Json<EngineCapabilities>, StatusCode> {
    check_auth(&headers, state.token.as_deref())?;
    Ok(Json(state.engine.capabilities()))
}

async fn prewarm(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Result<(StatusCode, Json<serde_json::Value>), StatusCode> {
    check_auth(&headers, state.token.as_deref())?;
    let queued = state.engine.prewarm_async(false);
    Ok((
        StatusCode::ACCEPTED,
        Json(serde_json::json!({ "warming": queued })),
    ))
}

async fn update_config(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(body): Json<UpdateConfigRequest>,
) -> Result<Json<EngineCapabilities>, (StatusCode, Json<serde_json::Value>)> {
    check_auth(&headers, state.token.as_deref())
        .map_err(|s| (s, Json(serde_json::json!({ "error": "unauthorized" }))))?;
    let engine = state.engine.clone();
    let result = tokio::task::spawn_blocking(move || {
        engine.update_config(body.faster_live_text, body.idle_unload_secs, body.model_path)
    })
    .await
    .map_err(|e| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({ "error": e.to_string() })),
        )
    })?;
    match result {
        Ok(caps) => Ok(Json(caps)),
        Err(err) => Err((
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({ "error": format!("{err:#}") })),
        )),
    }
}

async fn transcribe_offline(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Query(query): Query<TranscribeQuery>,
    body: Bytes,
) -> Result<Json<crate::engine::TranscriptionResult>, (StatusCode, Json<serde_json::Value>)> {
    check_auth(&headers, state.token.as_deref())
        .map_err(|s| (s, Json(serde_json::json!({ "error": "unauthorized" }))))?;

    let samples_16k = decode_upload(&headers, &query, &body).map_err(|msg| {
        (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({ "error": msg })),
        )
    })?;

    let vocabulary: Vec<String> = query
        .vocabulary
        .as_deref()
        .map(|raw| {
            raw.split(',')
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(ToOwned::to_owned)
                .collect()
        })
        .unwrap_or_default();

    let engine = state.engine.clone();
    let language = query.language;
    let screen_speech = query.screen_speech;
    let result = tokio::task::spawn_blocking(move || {
        engine.transcribe_offline(samples_16k, language, vocabulary, screen_speech)
    })
    .await
    .map_err(|e| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({ "error": e.to_string() })),
        )
    })?;

    match result {
        Ok(res) => Ok(Json(res)),
        Err(err) => Err((
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({ "error": format!("{err:#}") })),
        )),
    }
}

async fn create_stream(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(body): Json<CreateStreamRequest>,
) -> Result<(StatusCode, Json<CreateStreamResponse>), (StatusCode, Json<serde_json::Value>)> {
    check_auth(&headers, state.token.as_deref())
        .map_err(|s| (s, Json(serde_json::json!({ "error": "unauthorized" }))))?;
    if !matches!(body.sample_rate, 8_000..=96_000) {
        return Err((
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({ "error": "unsupported sampleRate" })),
        ));
    }

    // Cancel any stale active stream so hotkey dictation always wins.
    let stale: Vec<(u64, CancelToken)> = {
        let mut streams = state.streams.lock().unwrap_or_else(|e| e.into_inner());
        streams
            .drain()
            .filter(|(_, s)| !s.finished)
            .map(|(id, s)| (id, s.cancel))
            .collect()
    };
    for (id, cancel) in stale {
        state.engine.cancel_stream(id, &cancel);
    }

    let caps = state.engine.capabilities();
    let config = StreamConfig {
        language: body.language,
        vocabulary: body.vocabulary,
        partials: body.partials,
        screen_speech: body.screen_speech,
    };
    let (id, cancel, events) = state.engine.begin_stream(config).map_err(|err| {
        (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({ "error": format!("{err:#}") })),
        )
    })?;
    let owner_token = generate_owner_token();

    {
        let mut streams = state.streams.lock().unwrap_or_else(|e| e.into_inner());
        streams.insert(
            id,
            ActiveStream {
                owner_token: owner_token.clone(),
                sample_rate: body.sample_rate,
                sample_format: body.sample_format,
                cancel,
                events,
                last_activity: Instant::now(),
                finished: false,
            },
        );
    }

    Ok((
        StatusCode::CREATED,
        Json(CreateStreamResponse {
            id,
            owner_token,
            partials: body.partials,
            native_streaming: caps.native_streaming,
            lease_ms: STREAM_LEASE.as_millis() as u64,
        }),
    ))
}

async fn push_stream_audio(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<u64>,
    body: Bytes,
) -> Result<StatusCode, (StatusCode, Json<serde_json::Value>)> {
    check_auth(&headers, state.token.as_deref())
        .map_err(|s| (s, Json(serde_json::json!({ "error": "unauthorized" }))))?;

    let (sample_rate, sample_format) = {
        let mut streams = state.streams.lock().unwrap_or_else(|e| e.into_inner());
        let stream = streams.get_mut(&id).ok_or_else(|| {
            (
                StatusCode::NOT_FOUND,
                Json(serde_json::json!({ "error": "stream-not-found" })),
            )
        })?;
        check_stream_owner(&headers, stream)
            .map_err(|s| (s, Json(serde_json::json!({ "error": "stream-not-found" }))))?;
        if stream.finished {
            return Err((
                StatusCode::CONFLICT,
                Json(serde_json::json!({ "error": "stream-ended" })),
            ));
        }
        stream.last_activity = Instant::now();
        (stream.sample_rate, stream.sample_format)
    };

    let decoded = sample_format.decode(&body).ok_or_else(|| {
        (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({ "error": "invalid-audio" })),
        )
    })?;
    let samples_16k = resample_to_16k(&decoded, sample_rate);
    state.engine.feed_stream(id, samples_16k).map_err(|err| {
        (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({ "error": format!("{err:#}") })),
        )
    })?;
    Ok(StatusCode::NO_CONTENT)
}

async fn stream_events(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<u64>,
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, StatusCode> {
    check_auth(&headers, state.token.as_deref())?;
    let rx = {
        let streams = state.streams.lock().unwrap_or_else(|e| e.into_inner());
        let stream = streams.get(&id).ok_or(StatusCode::NOT_FOUND)?;
        check_stream_owner(&headers, stream)?;
        stream.events.subscribe()
    };

    let sse_stream = BroadcastStream::new(rx).filter_map(|item| match item {
        Ok(event) => {
            let payload = serde_json::to_string(&event).unwrap_or_default();
            Some(Ok(Event::default().data(payload)))
        }
        Err(_) => None,
    });
    let boxed = stream::select(stream::empty(), sse_stream);
    Ok(Sse::new(boxed).keep_alive(KeepAlive::new().interval(Duration::from_secs(15))))
}

async fn finish_stream(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<u64>,
) -> Result<Json<crate::engine::TranscriptionResult>, (StatusCode, Json<serde_json::Value>)> {
    check_auth(&headers, state.token.as_deref())
        .map_err(|s| (s, Json(serde_json::json!({ "error": "unauthorized" }))))?;
    {
        let mut streams = state.streams.lock().unwrap_or_else(|e| e.into_inner());
        let stream = streams.get_mut(&id).ok_or_else(|| {
            (
                StatusCode::NOT_FOUND,
                Json(serde_json::json!({ "error": "stream-not-found" })),
            )
        })?;
        check_stream_owner(&headers, stream)
            .map_err(|s| (s, Json(serde_json::json!({ "error": "stream-not-found" }))))?;
        if stream.finished {
            return Err((
                StatusCode::CONFLICT,
                Json(serde_json::json!({ "error": "stream-ended" })),
            ));
        }
        stream.finished = true;
    }

    let engine = state.engine.clone();
    let result = tokio::task::spawn_blocking(move || engine.finish_stream(id))
        .await
        .map_err(|e| {
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(serde_json::json!({ "error": e.to_string() })),
            )
        })?;

    {
        let mut streams = state.streams.lock().unwrap_or_else(|e| e.into_inner());
        streams.remove(&id);
    }

    match result {
        Ok(res) => Ok(Json(res)),
        Err(err) => Err((
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({ "error": format!("{err:#}") })),
        )),
    }
}

async fn cancel_stream(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<u64>,
) -> Result<StatusCode, StatusCode> {
    check_auth(&headers, state.token.as_deref())?;
    let cancel = {
        let mut streams = state.streams.lock().unwrap_or_else(|e| e.into_inner());
        let stream = streams.get(&id).ok_or(StatusCode::NOT_FOUND)?;
        check_stream_owner(&headers, stream)?;
        streams.remove(&id).map(|s| s.cancel)
    };
    if let Some(cancel) = cancel {
        state.engine.cancel_stream(id, &cancel);
    }
    Ok(StatusCode::NO_CONTENT)
}

async fn heartbeat_stream(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(id): Path<u64>,
) -> Result<StatusCode, StatusCode> {
    check_auth(&headers, state.token.as_deref())?;
    let mut streams = state.streams.lock().unwrap_or_else(|e| e.into_inner());
    let stream = streams.get_mut(&id).ok_or(StatusCode::NOT_FOUND)?;
    check_stream_owner(&headers, stream)?;
    if stream.finished {
        return Err(StatusCode::CONFLICT);
    }
    stream.last_activity = Instant::now();
    Ok(StatusCode::NO_CONTENT)
}

fn decode_upload(
    headers: &HeaderMap,
    query: &TranscribeQuery,
    body: &[u8],
) -> Result<Vec<f32>, String> {
    let content_type = headers
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");

    if content_type.starts_with("audio/wav") || body.starts_with(b"RIFF") {
        return decode_wav_to_16k(body);
    }
    let fmt = query.format.unwrap_or(SampleFormat::F32le);
    let samples = fmt
        .decode(body)
        .ok_or_else(|| "invalid PCM audio payload".to_string())?;
    Ok(resample_to_16k(&samples, query.sample_rate))
}

pub fn decode_wav_to_16k(bytes: &[u8]) -> Result<Vec<f32>, String> {
    let mut reader = hound::WavReader::new(Cursor::new(bytes))
        .map_err(|e| format!("invalid WAV header: {e}"))?;
    let spec = reader.spec();
    let channels = spec.channels.max(1) as usize;
    let interleaved: Vec<f32> = match spec.sample_format {
        hound::SampleFormat::Float => reader
            .samples::<f32>()
            .map(|s| s.map_err(|e| e.to_string()))
            .collect::<Result<Vec<_>, _>>()?,
        hound::SampleFormat::Int => {
            let max_val = (1i64 << (spec.bits_per_sample.saturating_sub(1))) as f32;
            reader
                .samples::<i32>()
                .map(|s| s.map(|v| (v as f32) / max_val).map_err(|e| e.to_string()))
                .collect::<Result<Vec<_>, _>>()?
        }
    };
    let mono: Vec<f32> = if channels == 1 {
        interleaved
    } else {
        interleaved
            .chunks(channels)
            .map(|frame| frame.iter().sum::<f32>() / (frame.len() as f32))
            .collect()
    };
    Ok(resample_to_16k(&mono, spec.sample_rate))
}

fn generate_owner_token() -> String {
    let mut bytes = [0u8; 24];
    let _ = getrandom::fill(&mut bytes);
    let mut out = String::with_capacity(5 + bytes.len() * 2);
    out.push_str("jaro_");
    for b in bytes {
        use std::fmt::Write;
        let _ = write!(&mut out, "{b:02x}");
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sample_format_decodes_s16le_and_f32le() {
        let s16 = [0i16, 16384, -16384]
            .into_iter()
            .flat_map(i16::to_le_bytes)
            .collect::<Vec<_>>();
        let out = SampleFormat::S16le.decode(&s16).unwrap();
        assert_eq!(out.len(), 3);
        assert!((out[1] - 0.5).abs() < 1e-4);
        assert!((out[2] + 0.5).abs() < 1e-4);
    }

    #[test]
    fn owner_token_is_unique_and_prefixed() {
        let a = generate_owner_token();
        let b = generate_owner_token();
        assert!(a.starts_with("jaro_"));
        assert_ne!(a, b);
    }
}
