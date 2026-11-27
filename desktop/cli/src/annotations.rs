use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use base64::Engine;
use base64::engine::general_purpose::STANDARD as BASE64;
use regex::Regex;
use rusqlite::{OptionalExtension, params};
use serde_json::{Map, Value, json};

use crate::error::CliError;
use crate::store::{SessionStore, f64_to_json};
use crate::time_util::frame_time;

const MAX_INLINE_SHEET: usize = 9 * 1024 * 1024;
const MAX_MANIFEST_BYTES: u64 = 24 * 1024 * 1024;
const MAX_TIMELINE_SECONDS: f64 = 3.0 * 60.0 * 60.0 + 2.0;

const KEYFRAME_KINDS: [&str; 6] = [
    "frame-01", "frame-02", "frame-03", "frame-04", "frame-05", "frame-06",
];

static RE_ANN_PREFIX: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[0-9a-f][0-9a-f-]{5,35}$").unwrap());
static RE_APP_ID: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$").unwrap());

pub fn is_package_reference(value: &str) -> bool {
    let p = Path::new(value);
    let ext_match = p
        .extension()
        .and_then(|e| e.to_str())
        .map(|e| e.eq_ignore_ascii_case("jaroannotation"))
        .unwrap_or(false);
    let name_match = p.file_name().and_then(|n| n.to_str()) == Some("annotation.json");
    ext_match || name_match
}

fn missing_video_reason(key: &str) -> Option<&'static str> {
    match key {
        "noFrames" => Some("The screen recording produced no video frames."),
        "permissionDenied" => Some("macOS declined Screen Recording access."),
        "screenInterrupted" => Some("The video source stopped before it saved a usable picture."),
        _ => None,
    }
}

fn asset_filename(kind: &str) -> Option<&'static str> {
    match kind {
        "manifest" => Some("annotation.json"),
        "contact-sheet" => Some("contact-sheet.png"),
        "frame-01" => Some("frame-01.png"),
        "frame-02" => Some("frame-02.png"),
        "frame-03" => Some("frame-03.png"),
        "frame-04" => Some("frame-04.png"),
        "frame-05" => Some("frame-05.png"),
        "frame-06" => Some("frame-06.png"),
        _ => None,
    }
}

pub fn annotations(library: &SessionStore, limit: usize, prefix: &str) -> Result<Vec<Value>, CliError> {
    if !library.has_db() {
        return Ok(Vec::new());
    }
    let conn = library.connect()?;
    let source = SessionStore::readable_source(&conn)?;
    let sql = format!(
        "SELECT id, name, created, status, duration AS durationSeconds,
            json_extract(options,'$.annotation.source') AS source,
            json_extract(options,'$.annotation.strokeCount') AS strokeCount,
            json_extract(options,'$.annotation.frames') AS frames
         FROM {source}
         WHERE json_type(options,'$.annotation')='object' AND id LIKE ?
         ORDER BY created DESC, id LIMIT ?"
    );
    let pattern = format!("{prefix}%");
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(params![pattern, limit as i64], |r| {
        let frames_str: Option<String> = r.get(7)?;
        let frames: Value = frames_str
            .as_deref()
            .and_then(|s| serde_json::from_str(s).ok())
            .unwrap_or_else(|| json!([]));
        let dur: Option<f64> = r.get(4)?;
        let mut m = Map::new();
        m.insert("id".into(), Value::String(r.get(0)?));
        m.insert("name".into(), Value::String(r.get(1)?));
        m.insert("created".into(), Value::String(r.get(2)?));
        m.insert("status".into(), Value::String(r.get(3)?));
        m.insert("durationSeconds".into(), dur.map(f64_to_json).unwrap_or(Value::Null));
        m.insert(
            "source".into(),
            r.get::<_, Option<String>>(5)?.map(Value::String).unwrap_or(Value::Null),
        );
        m.insert(
            "strokeCount".into(),
            r.get::<_, Option<i64>>(6)?.map(|n| json!(n)).unwrap_or(Value::Null),
        );
        m.insert("frames".into(), frames);
        Ok(Value::Object(m))
    })?;
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}

pub fn annotation_asset_path(library: &SessionStore, key: &str, kind: &str) -> Result<PathBuf, CliError> {
    if !library.has_db() {
        return Err(CliError::missing(key));
    }
    let Some(filename) = asset_filename(kind) else {
        return Err(CliError::missing(key));
    };
    let conn = library.connect()?;
    let source = SessionStore::readable_source(&conn)?;
    let sql = format!("SELECT options FROM {source} WHERE id=?");
    let opt_str: Option<String> = conn.query_row(&sql, [key], |r| r.get(0)).optional()?;
    let Some(opt_str) = opt_str else {
        return Err(CliError::missing(key));
    };
    let options: Value = serde_json::from_str(&opt_str).unwrap_or(Value::Null);
    let Some(ann) = options.get("annotation").and_then(Value::as_object) else {
        return Err(CliError::missing(key));
    };
    if let Some(pos) = KEYFRAME_KINDS.iter().position(|&k| k == kind) {
        let idx = (pos + 1) as i64;
        let has_kf = ann
            .get("keyframes")
            .and_then(Value::as_array)
            .map(|a| a.iter().any(|v| v.as_i64() == Some(idx)))
            .unwrap_or(false);
        if !has_kf {
            return Err(CliError::missing(key));
        }
    }
    let loc = options.get("annotationLocation").and_then(Value::as_str);
    let folder = library.annotation_folder_for(&conn, key, loc)?;
    let path = folder.join(filename);
    if path.is_symlink() || !path.is_file() {
        return Err(CliError::missing(key));
    }
    Ok(path)
}

fn safe_path_str(res: Result<PathBuf, CliError>) -> Value {
    match res {
        Ok(p) => {
            let parent_sym = p.parent().map(|par| par.is_symlink()).unwrap_or(false);
            if !p.is_symlink() && !parent_sym {
                Value::String(p.to_string_lossy().into_owned())
            } else {
                Value::Null
            }
        }
        Err(_) => Value::Null,
    }
}

fn clean_origin(value: Option<&Value>) -> Option<Value> {
    let obj = value?.as_object()?;
    let app = obj.get("app")?.as_str()?;
    if app.len() > 255 || !RE_APP_ID.is_match(app) {
        return None;
    }
    let name = obj
        .get("name")
        .and_then(Value::as_str)
        .map(|s| s.split_whitespace().collect::<Vec<_>>().join(" "))
        .map(|s| s.chars().take(160).collect::<String>())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| app.to_string());
    // Stored annotation origins were already normalized at capture time; preserve validated fields.
    let mut out = Map::new();
    out.insert("app".into(), Value::String(app.to_ascii_lowercase()));
    out.insert("name".into(), Value::String(name));
    if let Some(term) = obj.get("terminal").filter(|v| v.is_object()) {
        out.insert("terminal".into(), term.clone());
    }
    if let Some(site) = obj.get("site").filter(|v| v.is_string()) {
        out.insert("site".into(), site.clone());
    }
    if let Some(site_url) = obj.get("siteUrl").filter(|v| v.is_string()) {
        out.insert("siteUrl".into(), site_url.clone());
    }
    Some(Value::Object(out))
}

pub fn annotation_detail(
    library: &SessionStore,
    identifier: &str,
    include_capture: bool,
) -> Result<Value, CliError> {
    let lower = identifier.to_ascii_lowercase();
    let matches = if lower == "latest" {
        annotations(library, 1, "")?
    } else {
        if !RE_ANN_PREFIX.is_match(&lower) {
            return Err(CliError::missing(identifier));
        }
        annotations(library, 2, &lower)?
    };
    if matches.len() > 1 {
        return Err(CliError::value(
            "That annotation ID prefix matches more than one Session.",
        ));
    }
    let Some(row) = matches.into_iter().next() else {
        return Err(CliError::missing(identifier));
    };
    let key = row.get("id").and_then(Value::as_str).unwrap_or("").to_string();
    let session = library.get(&key, false)?;
    let transcript = session.get("transcript").cloned().unwrap_or_else(|| json!({}));
    let ann = session.get("annotation").and_then(Value::as_object);
    let origin = clean_origin(ann.and_then(|a| a.get("origin")));
    let missing_video = ann
        .and_then(|a| a.get("missingVideo"))
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(String::from);
    let media_path = safe_path_str(library.media_path(&key));

    let frames = row
        .get("frames")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut keyframes = Vec::new();
    if let Some(kfs) = ann.and_then(|a| a.get("keyframes")).and_then(Value::as_array) {
        for idx_val in kfs {
            if let Some(idx) = idx_val.as_u64().map(|u| u as usize) {
                if idx >= 1 && idx <= KEYFRAME_KINDS.len() && idx <= frames.len() {
                    let p = safe_path_str(annotation_asset_path(library, &key, KEYFRAME_KINDS[idx - 1]));
                    if p.is_string() {
                        keyframes.push(json!({
                            "index": idx,
                            "time": frames[idx - 1],
                            "path": p,
                        }));
                    }
                }
            }
        }
    }

    let mut out = Map::new();
    out.insert("version".into(), json!(1));
    if let Some(robj) = row.as_object() {
        for (k, v) in robj {
            out.insert(k.clone(), v.clone());
        }
    }
    if let Some(orig) = origin {
        out.insert("origin".into(), orig);
    }
    out.insert("status".into(), session.get("status").cloned().unwrap_or(Value::Null));
    out.insert("durationSeconds".into(), session.get("duration").cloned().unwrap_or(Value::Null));
    out.insert("route".into(), Value::String(format!("/?session={key}&tab=overview")));
    out.insert(
        "transcript".into(),
        transcript.get("text").cloned().unwrap_or_else(|| Value::String(String::new())),
    );
    out.insert(
        "interruption".into(),
        transcript.get("interruption").cloned().unwrap_or(Value::Null),
    );
    if include_capture {
        out.insert("captureTitle".into(), session.get("name").cloned().unwrap_or(Value::Null));
        out.insert("timedTranscript".into(), transcript);
    }
    if let Some(ref mv) = missing_video {
        out.insert("missingVideo".into(), Value::String(mv.clone()));
        out.insert("audioPath".into(), media_path.clone());
    }
    if !keyframes.is_empty() {
        out.insert("keyframes".into(), Value::Array(keyframes));
    }
    out.insert(
        "videoPath".into(),
        if missing_video.is_some() { Value::Null } else { media_path },
    );
    out.insert("manifestPath".into(), safe_path_str(annotation_asset_path(library, &key, "manifest")));
    out.insert(
        "contactSheetPath".into(),
        safe_path_str(annotation_asset_path(library, &key, "contact-sheet")),
    );
    Ok(Value::Object(out))
}

pub fn annotation_context(
    detail: &Value,
    cursor: usize,
    max_chars: Option<usize>,
) -> Result<Value, CliError> {
    let transcript = detail.get("transcript").and_then(Value::as_str).unwrap_or("");
    let chars: Vec<char> = transcript.chars().collect();
    if cursor > chars.len() {
        return Err(CliError::value("The cursor is beyond the annotation narration."));
    }
    let end = match max_chars {
        None => chars.len(),
        Some(mc) => chars.len().min(cursor.saturating_add(mc)),
    };
    let id = detail.get("id").and_then(Value::as_str).unwrap_or("");
    let source = detail.get("source").and_then(Value::as_str).unwrap_or("");
    let created = detail.get("created").and_then(Value::as_str).unwrap_or("");
    let status = detail.get("status").and_then(Value::as_str).unwrap_or("");
    let strokes = detail.get("strokeCount").and_then(Value::as_i64).unwrap_or(0);

    let mut lines = vec![
        "# Screen annotation".to_string(),
        String::new(),
        format!("- Capture ID: {id}"),
        format!("- Source: {source}"),
        format!("- Recorded: {created}"),
        format!("- Narration status: {status}"),
        format!("- Strokes: {strokes}"),
    ];
    if let Some(dur) = detail.get("durationSeconds").and_then(Value::as_f64) {
        if dur != 0.0 {
            lines.push(format!("- Duration: {}", frame_time(dur)));
        }
    }
    if let Some(frames) = detail.get("frames").and_then(Value::as_array) {
        if !frames.is_empty() {
            let formatted = frames
                .iter()
                .filter_map(Value::as_f64)
                .map(frame_time)
                .collect::<Vec<_>>()
                .join(", ");
            lines.push(String::new());
            lines.push("## Contact sheet times".to_string());
            lines.push(String::new());
            lines.push(formatted);
        }
    }
    if let Some(mv) = detail.get("missingVideo").and_then(Value::as_str) {
        if let Some(reason) = missing_video_reason(mv) {
            lines.push(String::new());
            lines.push(reason.to_string());
        }
    }
    if let Some(interr) = detail.get("interruption").and_then(Value::as_str) {
        if !interr.is_empty() {
            lines.push(String::new());
            lines.push(interr.to_string());
        }
    }
    lines.push(String::new());
    lines.push("## Narration".to_string());
    lines.push(String::new());
    if chars.is_empty() {
        lines.push("No narration is available.".to_string());
    } else {
        let slice: String = chars[cursor..end].iter().collect();
        lines.push(slice);
    }
    if end < chars.len() {
        lines.push(String::new());
        lines.push("The narration continues on the next page.".to_string());
    }

    Ok(json!({
        "id": id,
        "route": detail.get("route").cloned().unwrap_or(Value::Null),
        "markdown": lines.join("\n") + "\n",
        "next_cursor": if end < chars.len() { json!(end) } else { Value::Null },
    }))
}

pub fn inline_contact_sheet(library: &SessionStore, key: &str) -> Option<String> {
    let path = annotation_asset_path(library, key, "contact-sheet").ok()?;
    if path.parent().is_some_and(|p| p.is_symlink()) {
        return None;
    }
    let mut f = File::open(&path).ok()?;
    let mut data = Vec::new();
    f.by_ref()
        .take((MAX_INLINE_SHEET + 1) as u64)
        .read_to_end(&mut data)
        .ok()?;
    if data.len() > MAX_INLINE_SHEET || !data.starts_with(b"\x89PNG\r\n\x1a\n") {
        return None;
    }
    Some(BASE64.encode(&data))
}

fn is_valid_number(v: Option<&Value>, lower: f64, upper: f64) -> bool {
    let Some(Value::Number(n)) = v else { return false };
    n.as_f64()
        .is_some_and(|f| f.is_finite() && f >= lower && f <= upper)
}

fn is_valid_point(v: Option<&Value>) -> bool {
    let Some(Value::Array(arr)) = v else { return false };
    arr.len() == 2 && is_valid_number(arr.first(), 0.0, 1.0) && is_valid_number(arr.get(1), 0.0, 1.0)
}

fn validate_annotation_timeline(manifest: &Value) -> Result<(), CliError> {
    let Some(obj) = manifest.as_object() else {
        return Err(CliError::value("Invalid annotation manifest."));
    };
    let space_ok = obj.get("coordinateSpace").and_then(Value::as_str) == Some("normalized-bottom-left");
    let source_ok = matches!(
        obj.get("source").and_then(Value::as_str),
        Some("display" | "window" | "area" | "phone")
    );
    if !space_ok || !source_ok {
        return Err(CliError::value("Invalid annotation manifest."));
    }
    let (Some(events), Some(frames)) = (
        obj.get("events").and_then(Value::as_array),
        obj.get("frames").and_then(Value::as_array),
    ) else {
        return Err(CliError::value("Invalid annotation timeline."));
    };
    if events.len() > 100_000 || frames.len() > KEYFRAME_KINDS.len() {
        return Err(CliError::value("Invalid annotation timeline."));
    }
    if let Some(mv) = obj.get("missingVideo") {
        let valid_mv = mv
            .as_str()
            .is_some_and(|s| missing_video_reason(s).is_some())
            && frames.is_empty();
        if !valid_mv {
            return Err(CliError::value("Invalid missing video state."));
        }
    }
    let mut last = 0.0f64;
    let mut _strokes: HashSet<u64> = HashSet::new();
    for event in events {
        let Some(eobj) = event.as_object() else {
            return Err(CliError::value("Annotation event times must be in order."));
        };
        if !is_valid_number(eobj.get("time"), last, MAX_TIMELINE_SECONDS) {
            return Err(CliError::value("Annotation event times must be in order."));
        }
        let t = eobj["time"].as_f64().unwrap();
        last = t;
        if eobj.get("clear") == Some(&Value::Bool(true)) {
            continue;
        }
        let stroke = eobj.get("stroke").and_then(Value::as_u64);
        let stroke_ok = stroke.is_some_and(|s| s <= (1u64 << 53) - 1)
            && eobj.get("stroke").is_some_and(|v| v.is_u64() || v.is_i64());
        if !is_valid_point(eobj.get("from")) || !is_valid_point(eobj.get("to")) || !stroke_ok {
            return Err(CliError::value("Invalid annotation stroke."));
        }
        if let Some(lifted) = eobj.get("liftedAt") {
            if !is_valid_number(Some(lifted), t, MAX_TIMELINE_SECONDS) {
                return Err(CliError::value("Invalid annotation stroke release time."));
            }
        }
        _strokes.insert(stroke.unwrap());
    }
    let mut prev_frame: Option<f64> = None;
    for f in frames {
        if !is_valid_number(Some(f), 0.0, MAX_TIMELINE_SECONDS) {
            return Err(CliError::value("Invalid annotation frame times."));
        }
        let fv = f.as_f64().unwrap();
        if prev_frame.is_some_and(|p| p >= fv) {
            return Err(CliError::value("Invalid annotation frame times."));
        }
        prev_frame = Some(fv);
    }
    Ok(())
}

pub fn capture_document(detail: &Value) -> Result<Value, CliError> {
    let Some(mp) = detail.get("manifestPath").and_then(Value::as_str) else {
        return Err(CliError::value(
            "The saved annotation is unavailable. Reconnect the Annotations folder if needed.",
        ));
    };
    let path = Path::new(mp);
    let meta = std::fs::symlink_metadata(path)
        .map_err(|_| CliError::value("An annotation asset is missing or exceeds its size limit."))?;
    if meta.file_type().is_symlink() || !meta.file_type().is_file() || meta.len() == 0 || meta.len() > MAX_MANIFEST_BYTES {
        return Err(CliError::value(
            "An annotation asset is missing or exceeds its size limit.",
        ));
    }
    let raw = std::fs::read(path)
        .map_err(|_| CliError::value("An annotation asset is missing or exceeds its size limit."))?;
    let manifest: Value = serde_json::from_slice(&raw)
        .map_err(|_| CliError::value("The saved annotation has an unreadable stroke timeline."))?;
    let version_ok = manifest
        .get("version")
        .and_then(Value::as_i64)
        .is_some_and(|v| v == 1);
    if !manifest.is_object() || !version_ok {
        return Err(CliError::value(
            "The saved annotation has an unreadable stroke timeline.",
        ));
    }
    let mut capture = Map::new();
    capture.insert("source".into(), detail.get("source").cloned().unwrap_or(Value::Null));
    capture.insert(
        "coordinateSpace".into(),
        manifest.get("coordinateSpace").cloned().unwrap_or(Value::Null),
    );
    capture.insert("events".into(), manifest.get("events").cloned().unwrap_or(Value::Null));
    capture.insert("frames".into(), detail.get("frames").cloned().unwrap_or(Value::Null));
    if let Some(mv) = detail.get("missingVideo").filter(|v| v.is_string()) {
        capture.insert("missingVideo".into(), mv.clone());
    }
    let capture_val = Value::Object(capture);
    validate_annotation_timeline(&capture_val)?;
    let mut capture = match capture_val {
        Value::Object(m) => m,
        _ => unreachable!(),
    };
    capture.insert("id".into(), detail.get("id").cloned().unwrap_or(Value::Null));
    capture.insert("title".into(), detail.get("captureTitle").cloned().unwrap_or(Value::Null));
    capture.insert("created".into(), detail.get("created").cloned().unwrap_or(Value::Null));
    capture.insert("status".into(), detail.get("status").cloned().unwrap_or(Value::Null));
    capture.insert(
        "durationSeconds".into(),
        detail.get("durationSeconds").cloned().unwrap_or(Value::Null),
    );
    capture.insert(
        "transcript".into(),
        detail.get("timedTranscript").cloned().unwrap_or(Value::Null),
    );
    capture.insert("images".into(), Value::Array(Vec::new()));
    Ok(json!({
        "formatVersion": 1,
        "annotation": Value::Object(capture),
    }))
}

pub fn stroke_times(document: &Value) -> Vec<Value> {
    let mut order: Vec<i64> = Vec::new();
    let mut strokes: HashMap<i64, (f64, f64, i64)> = HashMap::new();
    if let Some(events) = document
        .get("annotation")
        .and_then(|a| a.get("events"))
        .and_then(Value::as_array)
    {
        for event in events {
            if event.get("clear") == Some(&Value::Bool(true)) {
                continue;
            }
            let (Some(key), Some(t)) = (
                event.get("stroke").and_then(Value::as_i64),
                event.get("time").and_then(Value::as_f64),
            ) else {
                continue;
            };
            let lifted = event.get("liftedAt").and_then(Value::as_f64).unwrap_or(t);
            let entry = strokes.entry(key).or_insert_with(|| {
                order.push(key);
                (t, t, 0)
            });
            entry.1 = entry.1.max(lifted);
            entry.2 += 1;
        }
    }
    order
        .into_iter()
        .map(|k| {
            let (start, end, segs) = strokes[&k];
            json!({
                "stroke": k,
                "start": start,
                "end": end,
                "segments": segs,
            })
        })
        .collect()
}
