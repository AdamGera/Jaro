use std::collections::{HashMap, HashSet};
use std::ffi::CString;
use std::fs::File;
use std::io::{BufRead, BufReader, Read};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;
use std::os::unix::io::{AsRawFd, FromRawFd};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use chrono::{DateTime, Duration, Utc};
use regex::Regex;
use rusqlite::functions::FunctionFlags;
use rusqlite::{Connection, OpenFlags, OptionalExtension, params};
use serde_json::{Map, Value, json};
use uuid::Uuid;

use crate::error::CliError;
use crate::paths::{casefold, open_private_regular};
use crate::search::{rank_passages, words};
use crate::time_util::{
    date_bound, format_local_iso_millis, meeting_date_line, parse_iso_utc, parse_offset,
    py_round_i64, range_clock, timestamp,
};

pub const RECORDING_RATE: f64 = 48_000.0;
pub const PORTABLE_RECORDING_RATE: f64 = 16_000.0;
pub const MIXED_SPEAKERS: &str = "mixed";
const MAX_ROLLING_LINE: usize = 1024 * 1024;
const ROOT_MARKER: &str = ".jaro-annotation-folder";
const CAPTURE_MARKER: &str = ".jaro-annotation-id";

static RE_UUID_PREFIX: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[0-9a-fA-F-]{6,36}$").unwrap());
static RE_PARAGRAPH_SPLIT: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\n\s*\n").unwrap());

pub fn source_labels() -> Value {
    json!({"1": "You", "2": "Them"})
}

pub struct SessionStore {
    pub root: PathBuf,
    pub audio: PathBuf,
    pub db_path: PathBuf,
}

impl SessionStore {
    pub fn new(root: PathBuf) -> Self {
        let audio = root.join("audio");
        let db_path = root.join("sessions.sqlite3");
        Self { root, audio, db_path }
    }

    pub fn has_db(&self) -> bool {
        self.db_path.is_file()
    }

    pub fn connect(&self) -> Result<Connection, CliError> {
        let conn = Connection::open_with_flags(
            &self.db_path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )?;
        conn.busy_timeout(std::time::Duration::from_secs(15))?;
        conn.execute_batch("PRAGMA query_only=ON;")?;
        conn.create_scalar_function(
            "fold",
            1,
            FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC,
            |ctx| {
                let s: String = ctx.get(0)?;
                Ok(casefold(&s))
            },
        )?;
        Ok(conn)
    }

    pub fn readable_source(conn: &Connection) -> Result<&'static str, CliError> {
        let has_view: Option<i64> = conn
            .query_row(
                "SELECT 1 FROM sqlite_master WHERE type='view' AND name='readable_sessions'",
                [],
                |r| r.get(0),
            )
            .optional()?;
        Ok(if has_view.is_some() {
            "readable_sessions"
        } else {
            "sessions"
        })
    }

    pub fn table_exists(conn: &Connection, name: &str) -> Result<bool, CliError> {
        let found: Option<i64> = conn
            .query_row(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                [name],
                |r| r.get(0),
            )
            .optional()?;
        Ok(found.is_some())
    }

    pub fn use_24_hour_clock(&self) -> bool {
        if !self.has_db() {
            return false;
        }
        let Ok(conn) = self.connect() else {
            return false;
        };
        if !Self::table_exists(&conn, "meeting_clock_style").unwrap_or(false) {
            return false;
        }
        conn.query_row(
            "SELECT 1 FROM meeting_clock_style WHERE id=1 AND use_24_hour",
            [],
            |r| r.get::<_, i64>(0),
        )
        .optional()
        .ok()
        .flatten()
        .is_some()
    }

    pub fn exists(&self, key: &str) -> Result<bool, CliError> {
        if !self.has_db() {
            return Ok(false);
        }
        let conn = self.connect()?;
        if conn
            .query_row("SELECT 1 FROM sessions WHERE id=?", [key], |r| r.get::<_, i64>(0))
            .optional()?
            .is_some()
        {
            return Ok(true);
        }
        if Self::table_exists(&conn, "meeting_trash")? {
            if conn
                .query_row("SELECT 1 FROM meeting_trash WHERE id=?", [key], |r| r.get::<_, i64>(0))
                .optional()?
                .is_some()
            {
                return Ok(true);
            }
        }
        Ok(false)
    }

    pub fn list(&self, query: &str, meetings_only: bool) -> Result<Vec<Value>, CliError> {
        if !self.has_db() {
            return Ok(Vec::new());
        }
        let conn = self.connect()?;
        let has_fin = Self::table_exists(&conn, "meeting_finalization")?;
        let pending = if has_fin {
            "EXISTS(SELECT 1 FROM meeting_finalization f WHERE f.session_id=s.id)"
        } else {
            "0"
        };
        let source = Self::readable_source(&conn)?;
        let retention = if source == "readable_sessions" {
            "coalesce((SELECT days FROM meeting_retention WHERE id=1),json_extract(s.options,'$.retentionApplies'))"
        } else {
            "NULL"
        };
        let meeting = "coalesce(json_extract(s.options,'$.meeting'),json_extract(s.options,'$.sourceSpeakerTurns'),0)";
        let briefs = Self::table_exists(&conn, "meeting_briefs")?;
        let title = if briefs { "b.title" } else { "NULL" };
        let join_brief = if briefs {
            "LEFT JOIN meeting_briefs b ON b.id=s.id"
        } else {
            ""
        };
        let pending_brief = if briefs { "coalesce(b.status='queued',0)" } else { "0" };
        let provisional = "json_extract(s.options,'$.provisionalTitle')";
        let renamed = "json_extract(s.options,'$.meetingTitle')";
        let has_personal = meetings_only
            && !query.is_empty()
            && Self::table_exists(&conn, "personal_notes")?;
        let personal_search = if has_personal {
            "OR EXISTS(SELECT 1 FROM personal_notes p, json_tree(p.document) j WHERE p.id=s.id AND j.key='text' AND j.type='text' AND instr(fold(CAST(j.atom AS TEXT)),:folded))"
        } else {
            ""
        };
        let brief_search = if meetings_only && briefs {
            "OR instr(fold(coalesce(b.summary,'')),:folded)"
        } else {
            ""
        };
        let has_notes = Self::table_exists(&conn, "notes")?;
        let has_overviews = Self::table_exists(&conn, "overviews")?;
        let (join_notes, o_title, pending_ov) = if has_notes && has_overviews {
            (
                "LEFT JOIN notes n ON n.id=s.id LEFT JOIN overviews o ON o.id=s.id AND o.lang=n.displayed",
                "o.title",
                "(SELECT count(*) FROM overviews v WHERE v.id=s.id AND v.status='queued')",
            )
        } else {
            ("", "NULL", "0")
        };

        let sql = format!(
            "SELECT s.id,
                coalesce({renamed},json_extract(s.options,'$.meetingCalendar.title'),{title},{provisional},{o_title},s.name) AS name,
                s.created, s.duration, s.processed, s.status,
                {meeting} AS meeting,
                json_extract(s.options,'$.meetingProvider') AS meetingProvider,
                json_extract(s.options,'$.meetingStartedAt') AS meetingStartedAt,
                json_extract(s.options,'$.meetingEndedAt') AS meetingEndedAt,
                json_extract(s.options,'$.audioRemovedAt') AS audioRemovedAt,
                json_type(s.options,'$.annotation')='object' AS annotation,
                CASE WHEN {meeting} AND coalesce(json_extract(s.options,'$.retentionApplies'),1)<>0
                    THEN NULLIF({retention},0) END AS meetingRetentionDays,
                {pending} AS finalizationPending,
                {title} AS summaryTitle,
                {provisional} AS provisionalTitle,
                {pending_ov}+{pending_brief} AS pendingOverview
             FROM {source} s {join_notes} {join_brief}
             WHERE (NOT :meetings_only OR meeting) AND (:query='' OR instr(fold(s.name),:folded) OR instr(fold(coalesce({o_title},'')),:folded)
                OR instr(fold(s.search_text),:folded) OR instr(fold(coalesce(meetingProvider,'')),:folded)
                OR instr(fold(coalesce(json_extract(s.options,'$.meetingCalendar.title'),'')),:folded)
                OR instr(fold(coalesce({title},'')),:folded) OR instr(fold(coalesce({provisional},'')),:folded)
                OR instr(fold(coalesce({renamed},'')),:folded) {personal_search} {brief_search})
             ORDER BY s.created DESC, s.id"
        );
        let folded = casefold(query);
        let mut stmt = conn.prepare(&sql)?;
        let rows = stmt.query_map(
            rusqlite::named_params! {
                ":meetings_only": i64::from(meetings_only),
                ":query": query,
                ":folded": folded,
            },
            |row| {
                let mut m = Map::new();
                m.insert("id".into(), Value::String(row.get(0)?));
                m.insert("name".into(), Value::String(row.get(1)?));
                m.insert("created".into(), Value::String(row.get(2)?));
                m.insert("duration".into(), opt_f64_to_json(row.get(3)?));
                m.insert("processed".into(), f64_to_json(row.get(4)?));
                m.insert("status".into(), Value::String(row.get(5)?));
                let is_meeting: Option<i64> = row.get(6)?;
                m.insert("meeting".into(), Value::Bool(is_meeting.unwrap_or(0) != 0));
                m.insert("meetingProvider".into(), opt_str_to_json(row.get(7)?));
                m.insert("meetingStartedAt".into(), opt_str_to_json(row.get(8)?));
                m.insert("meetingEndedAt".into(), opt_str_to_json(row.get(9)?));
                m.insert("audioRemovedAt".into(), opt_str_to_json(row.get(10)?));
                let is_ann: Option<i64> = row.get(11)?;
                m.insert("annotation".into(), Value::Bool(is_ann.unwrap_or(0) != 0));
                m.insert("meetingRetentionDays".into(), opt_i64_to_json(row.get(12)?));
                let fin_pending: Option<i64> = row.get(13)?;
                m.insert("finalizationPending".into(), Value::Bool(fin_pending.unwrap_or(0) != 0));
                m.insert("summaryTitle".into(), opt_str_to_json(row.get(14)?));
                m.insert("provisionalTitle".into(), opt_str_to_json(row.get(15)?));
                let pend_ov: Option<i64> = row.get(16)?;
                m.insert("pendingOverview".into(), json!(pend_ov.unwrap_or(0)));
                Ok(Value::Object(m))
            },
        )?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row?);
        }
        Ok(out)
    }

    pub fn get(&self, key: &str, include_turns: bool) -> Result<Value, CliError> {
        if !self.has_db() {
            return Err(CliError::missing(key));
        }
        let conn = self.connect()?;
        let source = Self::readable_source(&conn)?;
        let sql = format!(
            "SELECT id, name, created, filename, mime, options, duration, processed, status, error, transcript
             FROM {source} WHERE id=?"
        );
        let row = conn
            .query_row(&sql, [key], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                    r.get::<_, String>(4)?,
                    r.get::<_, String>(5)?,
                    r.get::<_, Option<f64>>(6)?,
                    r.get::<_, f64>(7)?,
                    r.get::<_, String>(8)?,
                    r.get::<_, Option<String>>(9)?,
                    r.get::<_, Option<String>>(10)?,
                ))
            })
            .optional()?;
        let Some((id, raw_name, created, filename, _mime, options_str, duration, processed, status, error, transcript_str)) = row else {
            return Err(CliError::missing(key));
        };

        let mut options: Map<String, Value> =
            serde_json::from_str(&options_str).map_err(|e| CliError::value(e.to_string()))?;
        let custom_location = options
            .remove("annotationLocation")
            .and_then(|v| v.as_str().map(String::from));
        let mut has_audio = options
            .get("audioRemovedAt")
            .map(|v| v.is_null() || v.as_str() == Some(""))
            .unwrap_or(true);
        if let Some(ref loc) = custom_location {
            options.insert("annotationCustomStorage".into(), Value::Bool(true));
            if self.annotation_folder_for(&conn, &id, Some(loc)).is_err() {
                has_audio = false;
                options.insert("annotationStorageUnavailable".into(), Value::Bool(true));
                if let Some(Value::Object(ann)) = options.get_mut("annotation") {
                    ann.insert("manifestUrl".into(), Value::Null);
                    ann.insert("contactSheetUrl".into(), Value::Null);
                    ann.insert("keyframes".into(), Value::Array(Vec::new()));
                }
            }
        }

        let is_meeting = options
            .get("meeting")
            .and_then(Value::as_bool)
            .unwrap_or_else(|| {
                options
                    .get("sourceSpeakerTurns")
                    .and_then(Value::as_bool)
                    .unwrap_or(false)
            });

        let overview = self.read_overview_selection(&conn, &id)?;
        let personal = self.read_personal_notes(&conn, &id)?;
        let brief = if is_meeting {
            self.read_meeting_brief(&conn, &id)?
        } else {
            Value::Null
        };
        let export = if is_meeting {
            self.read_meeting_export(&conn, &id)?
        } else {
            Value::Null
        };
        let pending = Self::table_exists(&conn, "meeting_finalization")?
            && conn
                .query_row("SELECT 1 FROM meeting_finalization WHERE session_id=?", [&id], |r| r.get::<_, i64>(0))
                .optional()?
                .is_some();

        let retained_as_meeting = is_meeting
            && options
                .get("retentionApplies")
                .and_then(Value::as_i64)
                .map(|v| v != 0)
                .unwrap_or(true);
        let configured: Option<i64> = if retained_as_meeting && source == "readable_sessions" {
            conn.query_row("SELECT days FROM meeting_retention WHERE id=1", [], |r| r.get(0))
                .optional()?
        } else {
            None
        };
        let retention_days = configured
            .or_else(|| options.get("retentionApplies").and_then(Value::as_i64))
            .filter(|&v| v != 0);

        let display_name = options
            .get("meetingTitle")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .map(String::from)
            .or_else(|| {
                options
                    .get("meetingCalendar")
                    .and_then(|c| c.get("title"))
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .map(String::from)
            })
            .or_else(|| {
                brief
                    .get("title")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .map(String::from)
            })
            .or_else(|| {
                options
                    .get("provisionalTitle")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .map(String::from)
            })
            .or_else(|| {
                overview
                    .get("version")
                    .and_then(|v| v.get("title"))
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .map(String::from)
            })
            .unwrap_or_else(|| raw_name.clone());

        let is_recording = options
            .get("recording")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let suffix = Path::new(&filename)
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| format!(".{e}"))
            .unwrap_or_default();
        let audio_name = if is_recording {
            format!("{raw_name}{suffix}")
        } else {
            raw_name.clone()
        };

        let mut result = Map::new();
        result.insert("id".into(), Value::String(id.clone()));
        result.insert("name".into(), Value::String(raw_name));
        result.insert("created".into(), Value::String(created));
        result.insert("duration".into(), opt_f64_to_json(duration));
        result.insert("processed".into(), f64_to_json(processed));
        result.insert("status".into(), Value::String(status.clone()));
        for (k, v) in options.clone() {
            result.insert(k, v);
        }
        result.insert("meeting".into(), Value::Bool(is_meeting));
        result.insert("meetingRetentionDays".into(), opt_i64_to_json(retention_days));
        result.insert("finalizationPending".into(), Value::Bool(pending));
        result.insert("overview".into(), overview);
        result.insert("personalNotes".into(), personal);
        result.insert("meetingBrief".into(), brief);
        result.insert("meetingExport".into(), export);
        result.insert("name".into(), Value::String(display_name));
        result.insert("audioName".into(), Value::String(audio_name));
        result.insert(
            "audioPath".into(),
            if has_audio && custom_location.is_none() {
                Value::String(format!("audio/{id}/{filename}"))
            } else {
                Value::Null
            },
        );
        result.insert(
            "audioUrl".into(),
            if has_audio {
                Value::String(format!("/api/sessions/{id}/audio"))
            } else {
                Value::Null
            },
        );

        let want_turns = include_turns && is_meeting;
        let (transcript, turns) = self.build_transcript(
            &conn,
            &id,
            &options,
            duration,
            processed,
            &status,
            error.as_deref(),
            transcript_str.as_deref(),
            want_turns,
        )?;
        result.insert("transcript".into(), transcript);
        if want_turns {
            result.insert("meetingTurns".into(), Value::Array(turns));
        }
        Ok(Value::Object(result))
    }

    #[allow(clippy::too_many_arguments)]
    fn build_transcript(
        &self,
        conn: &Connection,
        key: &str,
        options: &Map<String, Value>,
        duration: Option<f64>,
        processed: f64,
        status: &str,
        error: Option<&str>,
        cached_str: Option<&str>,
        include_turns: bool,
    ) -> Result<(Value, Vec<Value>), CliError> {
        let cached = cached_str.filter(|s| !s.is_empty());
        if let Some(json_str) = cached {
            if !include_turns {
                let parsed: Value =
                    serde_json::from_str(json_str).map_err(|e| CliError::value(e.to_string()))?;
                return Ok((parsed, Vec::new()));
            }
        }
        let mut stmt = conn.prepare("SELECT payload FROM chunks WHERE session_id=? ORDER BY seconds")?;
        let mut chunks: Vec<Value> = stmt
            .query_map([key], |r| r.get::<_, String>(0))?
            .filter_map(|r| r.ok())
            .filter_map(|s| serde_json::from_str(&s).ok())
            .collect();

        let source_turns = options
            .get("sourceSpeakerTurns")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let is_meeting = options
            .get("meeting")
            .and_then(Value::as_bool)
            .unwrap_or(source_turns);
        let mixed_speakers = !is_meeting
            || options.get("meetingSpeakerMode").and_then(Value::as_str) == Some(MIXED_SPEAKERS);

        let rolling_rows: Vec<(i64, Value)> = if Self::table_exists(conn, "meeting_rolling")? {
            let mut rstmt = conn.prepare(
                "SELECT end_frame, payload FROM meeting_rolling WHERE session_id=? ORDER BY start_frame",
            )?;
            rstmt
                .query_map([key], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))?
                .filter_map(|r| r.ok())
                .filter_map(|(ef, s)| serde_json::from_str(&s).ok().map(|v| (ef, v)))
                .collect()
        } else {
            Vec::new()
        };

        let has_rolling = !rolling_rows.is_empty();
        let last_end_frame = rolling_rows.last().map(|(ef, _)| *ef).unwrap_or(0);
        if has_rolling {
            let provisional: Vec<Value> = rolling_rows.into_iter().map(|(_, v)| v).collect();
            chunks = if mixed_speakers {
                mixed_speaker_timeline(chunks, provisional, processed)
            } else {
                timeline_chunks(chunks, provisional, !source_turns)
            };
        }
        let mut seconds = if is_meeting || has_rolling {
            duration.filter(|&d| d != 0.0).unwrap_or(processed)
        } else {
            processed
        };
        if has_rolling {
            seconds = seconds.max(last_end_frame as f64 / RECORDING_RATE);
        }

        let mut transcript = if let Some(json_str) = cached {
            serde_json::from_str(json_str).map_err(|e| CliError::value(e.to_string()))?
        } else {
            let diarization = options
                .get("diarization")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let mut t = transcript_from_chunks(
                &chunks,
                seconds,
                diarization || (has_rolling && !mixed_speakers),
                error,
                source_turns || (has_rolling && !mixed_speakers),
                has_rolling,
            );
            if is_meeting || has_rolling {
                let src = if status == "complete" {
                    "final"
                } else if has_rolling || status == "processing" {
                    "rolling"
                } else {
                    "draft"
                };
                if let Value::Object(ref mut m) = t {
                    m.insert("transcriptSource".into(), Value::String(src.into()));
                }
            }
            t
        };

        let mut turns: Vec<Value> = if include_turns {
            chunks
                .iter()
                .flat_map(|c| {
                    c.get("segments")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default()
                })
                .collect()
        } else {
            Vec::new()
        };
        if has_rolling {
            turns.sort_by(|a, b| {
                let sa = a.get("start").and_then(Value::as_f64).unwrap_or(0.0);
                let sb = b.get("start").and_then(Value::as_f64).unwrap_or(0.0);
                sa.total_cmp(&sb)
            });
        }
        // Ensure transcript is object
        if !transcript.is_object() {
            transcript = json!({});
        }
        Ok((transcript, turns))
    }

    fn read_overview_selection(&self, conn: &Connection, key: &str) -> Result<Value, CliError> {
        if !Self::table_exists(conn, "notes")? || !Self::table_exists(conn, "overviews")? {
            return Ok(Value::Null);
        }
        let row = conn
            .query_row(
                "SELECT base, preference, displayed FROM notes WHERE id=?",
                [key],
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, Option<String>>(2)?,
                    ))
                },
            )
            .optional()?;
        let Some((base, preference, displayed)) = row else {
            return Ok(Value::Null);
        };
        let target = if preference == "auto" {
            base.clone()
        } else {
            preference.clone()
        };
        let item = conn
            .query_row(
                "SELECT status, error, scope FROM overviews WHERE id=? AND lang=?",
                params![key, target],
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, Option<String>>(1)?,
                        r.get::<_, Option<String>>(2)?,
                    ))
                },
            )
            .optional()?;
        let display_body: Option<String> = if let Some(ref disp) = displayed {
            conn.query_row(
                "SELECT body FROM overviews WHERE id=? AND lang=?",
                params![key, disp],
                |r| r.get::<_, Option<String>>(0),
            )
            .optional()?
            .flatten()
        } else {
            None
        };
        let version: Value = display_body
            .and_then(|b| serde_json::from_str(&b).ok())
            .unwrap_or(Value::Null);
        let (status, error, scope) = item.unwrap_or_else(|| ("empty".to_string(), None, None));
        Ok(json!({
            "preference": preference,
            "autoLanguage": base,
            "target": target,
            "status": status,
            "error": error,
            "scope": scope,
            "version": version,
        }))
    }

    fn read_personal_notes(&self, conn: &Connection, key: &str) -> Result<Value, CliError> {
        let empty = json!({"document": {"type": "doc", "content": [{"type": "paragraph"}]}, "revision": 0});
        if !Self::table_exists(conn, "personal_notes")? {
            return Ok(empty);
        }
        let row = conn
            .query_row(
                "SELECT document, revision FROM personal_notes WHERE id=?",
                [key],
                |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)),
            )
            .optional()?;
        if let Some((doc_str, rev)) = row {
            if let Ok(doc) = serde_json::from_str::<Value>(&doc_str) {
                return Ok(json!({"document": doc, "revision": rev}));
            }
        }
        Ok(empty)
    }

    fn read_meeting_brief(&self, conn: &Connection, key: &str) -> Result<Value, CliError> {
        if !Self::table_exists(conn, "meeting_briefs")? {
            return Ok(Value::Null);
        }
        let row = conn
            .query_row(
                "SELECT status, title, summary, error FROM meeting_briefs WHERE id=?",
                [key],
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, Option<String>>(1)?,
                        r.get::<_, Option<String>>(2)?,
                        r.get::<_, Option<String>>(3)?,
                    ))
                },
            )
            .optional()?;
        if let Some((status, title, summary, error)) = row {
            if status == "queued" || title.as_deref().is_some_and(|s| !s.is_empty()) || error.is_some() {
                return Ok(json!({
                    "status": status,
                    "title": title,
                    "summary": summary,
                    "error": error,
                }));
            }
        }
        Ok(Value::Null)
    }

    fn read_meeting_export(&self, conn: &Connection, key: &str) -> Result<Value, CliError> {
        if !Self::table_exists(conn, "meeting_exports")? {
            return Ok(Value::Null);
        }
        let row = conn
            .query_row(
                "SELECT status, filename, error FROM meeting_exports WHERE id=?",
                [key],
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, Option<String>>(1)?,
                        r.get::<_, Option<String>>(2)?,
                    ))
                },
            )
            .optional()?;
        let Some((status, filename, error)) = row else {
            return Ok(Value::Null);
        };
        let notes_filename: Option<String> = conn
            .query_row("SELECT notes_filename FROM meeting_exports WHERE id=?", [key], |r| r.get(0))
            .optional()
            .ok()
            .flatten()
            .flatten();
        let refresh: i64 = conn
            .query_row("SELECT refresh FROM meeting_exports WHERE id=?", [key], |r| r.get(0))
            .optional()
            .ok()
            .flatten()
            .unwrap_or(0);
        Ok(json!({
            "status": status,
            "filename": if status == "ready" { filename } else { None },
            "notesFilename": notes_filename,
            "error": error,
            "refreshing": refresh != 0 && status == "ready",
        }))
    }

    pub fn annotation_folder_for(
        &self,
        conn: &Connection,
        key: &str,
        location: Option<&str>,
    ) -> Result<PathBuf, CliError> {
        let Some(loc) = location else {
            return Ok(self.audio.join(key));
        };
        if !Self::table_exists(conn, "annotation_locations")? {
            return Err(CliError::runtime("The Annotations folder is unavailable."));
        }
        let folder: Option<String> = conn
            .query_row("SELECT folder FROM annotation_locations WHERE id=?", [loc], |r| r.get(0))
            .optional()?;
        let Some(folder_str) = folder else {
            return Err(CliError::runtime("The Annotations folder is unavailable."));
        };
        let root = check_annotation_root(Path::new(&folder_str), loc)?;
        let bundle = root.join(format!("Jaro Annotation {key}"));
        check_annotation_bundle(&bundle, key)?;
        Ok(bundle)
    }

    pub fn media_path(&self, key: &str) -> Result<PathBuf, CliError> {
        if !self.has_db() {
            return Err(CliError::missing(key));
        }
        let conn = self.connect()?;
        let source = Self::readable_source(&conn)?;
        let sql = format!("SELECT filename, options FROM {source} WHERE id=?");
        let row = conn
            .query_row(&sql, [key], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))
            .optional()?;
        let Some((filename, opt_str)) = row else {
            return Err(CliError::missing(key));
        };
        let options: Value = serde_json::from_str(&opt_str).unwrap_or(Value::Null);
        if options.get("audioRemovedAt").and_then(Value::as_str).is_some_and(|s| !s.is_empty()) {
            return Err(CliError::missing(key));
        }
        let loc = if options.get("annotation").is_some_and(|v| v.is_object()) {
            options.get("annotationLocation").and_then(Value::as_str)
        } else {
            None
        };
        let folder = self.annotation_folder_for(&conn, key, loc)?;
        let path = folder.join(&filename);
        if path.parent() != Some(folder.as_path()) || path.is_symlink() || !path.is_file() {
            return Err(CliError::missing(key));
        }
        Ok(path)
    }
}

fn read_marker(path: &Path) -> Result<String, CliError> {
    let meta = std::fs::symlink_metadata(path)
        .map_err(|_| CliError::runtime("The Annotations folder is unavailable."))?;
    if !meta.file_type().is_file() {
        return Err(CliError::runtime("The Annotations folder is unavailable."));
    }
    let Some(mut f) = open_private_regular(path)
        .map_err(|_| CliError::runtime("The Annotations folder is unavailable."))? else {
        return Err(CliError::runtime("The Annotations folder is unavailable."));
    };
    let mut buf = [0u8; 37];
    let n = f
        .read(&mut buf)
        .map_err(|_| CliError::runtime("The Annotations folder is unavailable."))?;
    let key = std::str::from_utf8(&buf[..n])
        .map_err(|_| CliError::runtime("The Annotations folder is unavailable."))?;
    if is_canonical_uuid(key) {
        Ok(key.to_string())
    } else {
        Err(CliError::runtime("The Annotations folder is unavailable."))
    }
}

fn check_annotation_root(path: &Path, location: &str) -> Result<PathBuf, CliError> {
    let meta = std::fs::symlink_metadata(path)
        .map_err(|_| CliError::runtime("The Annotations folder is unavailable."))?;
    if !meta.file_type().is_dir() {
        return Err(CliError::runtime("The Annotations folder is unavailable."));
    }
    let resolved = path
        .canonicalize()
        .map_err(|_| CliError::runtime("The Annotations folder is unavailable."))?;
    if resolved != path {
        return Err(CliError::runtime("The Annotations folder is unavailable."));
    }
    if read_marker(&path.join(ROOT_MARKER))? != location {
        return Err(CliError::runtime("The Annotations folder is unavailable."));
    }
    Ok(path.to_path_buf())
}

fn check_annotation_bundle(folder: &Path, key: &str) -> Result<(), CliError> {
    let meta = std::fs::symlink_metadata(folder)
        .map_err(|_| CliError::runtime("The Annotations folder is unavailable."))?;
    if !meta.file_type().is_dir() {
        return Err(CliError::runtime("The Annotations folder is unavailable."));
    }
    if read_marker(&folder.join(CAPTURE_MARKER))? != key {
        return Err(CliError::runtime("The Annotations folder is unavailable."));
    }
    Ok(())
}

pub fn is_canonical_uuid(s: &str) -> bool {
    s.len() == 36
        && Uuid::parse_str(s)
            .map(|u| u.hyphenated().to_string() == s)
            .unwrap_or(false)
}

fn opt_f64_to_json(v: Option<f64>) -> Value {
    match v {
        Some(n) => f64_to_json(n),
        None => Value::Null,
    }
}

pub fn f64_to_json(n: f64) -> Value {
    if n.is_finite() {
        json!(n)
    } else {
        Value::Null
    }
}

fn opt_str_to_json(v: Option<String>) -> Value {
    match v {
        Some(s) => Value::String(s),
        None => Value::Null,
    }
}

fn opt_i64_to_json(v: Option<i64>) -> Value {
    match v {
        Some(n) => json!(n),
        None => Value::Null,
    }
}

// ---------------------------------------------------------------------------
// Rolling transcript & chunk reconciliation (1:1 port of meeting_rolling.py & session_store.py)
// ---------------------------------------------------------------------------

fn midpoint(turn: &Value) -> Option<f64> {
    let s = turn.get("start").and_then(Value::as_f64)?;
    let e = turn.get("end").and_then(Value::as_f64)?;
    if s.is_finite() && e.is_finite() {
        Some((s + e) / 2.0)
    } else {
        None
    }
}

fn merge_intervals(mut spans: Vec<(f64, f64)>) -> Vec<(f64, f64)> {
    spans.sort_by(|a, b| a.0.total_cmp(&b.0).then_with(|| a.1.total_cmp(&b.1)));
    let mut combined: Vec<(f64, f64)> = Vec::new();
    for (s, e) in spans {
        if let Some(last) = combined.last_mut() {
            if s <= last.1 {
                last.1 = last.1.max(e);
                continue;
            }
        }
        combined.push((s, e));
    }
    combined
}

fn covers(spans: &[(f64, f64)], t: f64) -> bool {
    spans.iter().any(|&(s, e)| s <= t && t < e)
}

fn word_turn(turn: &Value, group: &[Value]) -> Value {
    let mut out = turn.as_object().cloned().unwrap_or_default();
    if let Some(first) = group.first() {
        out.insert("start".into(), first.get("start").cloned().unwrap_or(Value::Null));
    }
    if let Some(last) = group.last() {
        out.insert("end".into(), last.get("end").cloned().unwrap_or(Value::Null));
    }
    let mut text = String::new();
    for w in group {
        if let Some(t) = w.get("text").and_then(Value::as_str) {
            text.push_str(t);
        }
        let sep = w.get("separator").and_then(Value::as_str).unwrap_or(" ");
        text.push_str(sep);
    }
    out.insert("text".into(), Value::String(text.trim().to_string()));
    Value::Object(out)
}

fn trim_chunks<F>(chunks: Vec<Value>, keep: F) -> Vec<Value>
where
    F: Fn(Option<i64>, Option<f64>) -> bool,
{
    let mut result = Vec::new();
    for chunk in chunks {
        let segments = chunk
            .get("segments")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let words_arr = chunk
            .get("words")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let mut kept_turns: Vec<Value> = Vec::new();
        let mut kept_words: Vec<Value> = Vec::new();
        for turn in &segments {
            let speaker = turn.get("speaker").and_then(Value::as_i64);
            let start = turn.get("start").and_then(Value::as_f64);
            let end = turn.get("end").and_then(Value::as_f64);
            let center = midpoint(turn);
            if center.is_none() {
                if keep(speaker, None) {
                    kept_turns.push(turn.clone());
                }
                continue;
            }
            let (s_val, e_val) = (start.unwrap(), end.unwrap());
            let aligned: Vec<(&Value, f64)> = words_arr
                .iter()
                .filter_map(|w| {
                    if w.get("speaker").and_then(Value::as_i64) == speaker {
                        let p = midpoint(w)?;
                        if s_val <= p && p <= e_val {
                            return Some((w, p));
                        }
                    }
                    None
                })
                .collect();
            if aligned.is_empty() {
                if keep(speaker, center) {
                    kept_turns.push(turn.clone());
                }
                continue;
            }
            let chosen: Vec<bool> = aligned.iter().map(|(_, p)| keep(speaker, Some(*p))).collect();
            if chosen.iter().all(|&c| c) {
                kept_turns.push(turn.clone());
                kept_words.extend(aligned.iter().map(|(w, _)| (*w).clone()));
            } else if chosen.iter().any(|&c| c) {
                let mut group: Vec<Value> = Vec::new();
                for ((w, _), &use_it) in aligned.iter().zip(chosen.iter()) {
                    if use_it {
                        group.push((*w).clone());
                    } else if !group.is_empty() {
                        kept_turns.push(word_turn(turn, &group));
                        kept_words.extend(group.drain(..));
                    }
                }
                if !group.is_empty() {
                    kept_turns.push(word_turn(turn, &group));
                    kept_words.extend(group);
                }
            }
        }
        let mut activity = Vec::new();
        if let Some(acts) = chunk.get("activity").and_then(Value::as_array) {
            for row in acts {
                let r_sp = row.get("speaker").and_then(Value::as_i64);
                let r_s = row.get("start").and_then(Value::as_f64).unwrap_or(0.0);
                let r_e = row.get("end").and_then(Value::as_f64).unwrap_or(0.0);
                for turn in &kept_turns {
                    if r_sp == turn.get("speaker").and_then(Value::as_i64) {
                        if let (Some(ts), Some(te)) = (
                            turn.get("start").and_then(Value::as_f64),
                            turn.get("end").and_then(Value::as_f64),
                        ) {
                            let s = r_s.max(ts);
                            let e = r_e.min(te);
                            if s < e {
                                let mut m = row.as_object().cloned().unwrap_or_default();
                                m.insert("start".into(), json!(s));
                                m.insert("end".into(), json!(e));
                                activity.push(Value::Object(m));
                            }
                        }
                    }
                }
            }
        }
        if !kept_turns.is_empty() {
            if kept_turns == segments {
                result.push(chunk);
            } else {
                let mut m = chunk.as_object().cloned().unwrap_or_default();
                let text = kept_turns
                    .iter()
                    .filter_map(|t| t.get("text").and_then(Value::as_str))
                    .collect::<Vec<_>>()
                    .join(" ");
                m.insert("text".into(), Value::String(text));
                m.insert("segments".into(), Value::Array(kept_turns));
                m.insert("words".into(), Value::Array(kept_words));
                m.insert("activity".into(), Value::Array(activity));
                result.push(Value::Object(m));
            }
        }
    }
    result
}

fn clear_speakers(mut chunk: Value) -> Value {
    if let Value::Object(ref mut m) = chunk {
        m.insert("activity".into(), Value::Array(Vec::new()));
        for key in ["segments", "words"] {
            if let Some(Value::Array(arr)) = m.get_mut(key) {
                for item in arr.iter_mut() {
                    if let Value::Object(im) = item {
                        im.insert("speaker".into(), Value::Null);
                    }
                }
            }
        }
    }
    chunk
}

fn mixed_speaker_timeline(final_chunks: Vec<Value>, rolling: Vec<Value>, processed: f64) -> Vec<Value> {
    if final_chunks.is_empty() {
        return rolling;
    }
    let tail = trim_chunks(rolling, |_, pt| pt.is_some_and(|p| p >= processed));
    let mut out = final_chunks;
    out.extend(tail.into_iter().map(clear_speakers));
    out
}

fn timeline_chunks(final_chunks: Vec<Value>, rolling: Vec<Value>, mixed: bool) -> Vec<Value> {
    if rolling.is_empty() {
        return final_chunks;
    }
    let mut covering: HashMap<i64, Vec<(f64, f64)>> = HashMap::new();
    for sp in [1i64, 2i64] {
        let mut spans = Vec::new();
        let key = sp.to_string();
        for chunk in &final_chunks {
            if let Some(arr) = chunk
                .get("meetingCoverage")
                .and_then(|mc| mc.get(&key))
                .and_then(Value::as_array)
            {
                for span in arr {
                    if let Some(pair) = span.as_array() {
                        if pair.len() == 2 {
                            if let (Some(a), Some(b)) = (pair[0].as_f64(), pair[1].as_f64()) {
                                spans.push((a, b));
                            }
                        }
                    }
                }
            }
        }
        covering.insert(sp, merge_intervals(spans));
    }
    if mixed {
        let mut attr_spans = Vec::new();
        for chunk in &rolling {
            if let Some(segs) = chunk.get("segments").and_then(Value::as_array) {
                for turn in segs {
                    if midpoint(turn).is_some() {
                        if let (Some(s), Some(e)) = (
                            turn.get("start").and_then(Value::as_f64),
                            turn.get("end").and_then(Value::as_f64),
                        ) {
                            attr_spans.push((s, e));
                        }
                    }
                }
            }
        }
        let attributed = merge_intervals(attr_spans);
        let cleared: Vec<Value> = final_chunks.into_iter().map(clear_speakers).collect();
        let trimmed = trim_chunks(cleared, |_, pt| pt.map_or(true, |p| !covers(&attributed, p)));
        let mut out = rolling;
        out.extend(trimmed);
        out
    } else {
        let trimmed = trim_chunks(rolling, |sp, pt| {
            let Some(p) = pt else { return true };
            let Some(s) = sp else { return true };
            covering.get(&s).map_or(true, |spans| !covers(spans, p))
        });
        let mut out = final_chunks;
        out.extend(trimmed);
        out
    }
}

fn transcript_from_chunks(
    chunks: &[Value],
    seconds: f64,
    speakers: bool,
    interruption: Option<&str>,
    meeting: bool,
    reconcile: bool,
) -> Value {
    let mut text_parts: Vec<String> = Vec::new();
    let mut languages: Vec<String> = Vec::new();
    let mut turns: Vec<Value> = Vec::new();
    let mut words_vec: Vec<Value> = Vec::new();
    let mut intervals: HashMap<i64, Vec<(f64, f64)>> = HashMap::new();
    let mut suppressed: i64 = 0;

    for chunk in chunks {
        if let Some(t) = chunk.get("text").and_then(Value::as_str) {
            if !t.is_empty() {
                text_parts.push(t.to_string());
                if let Some(langs) = chunk.get("languages").and_then(Value::as_array) {
                    for l in langs {
                        if let Some(ls) = l.as_str() {
                            if !ls.is_empty() && !languages.iter().any(|x| x == ls) {
                                languages.push(ls.to_string());
                            }
                        }
                    }
                } else if let Some(ls) = chunk.get("language").and_then(Value::as_str) {
                    if !ls.is_empty() && !languages.iter().any(|x| x == ls) {
                        languages.push(ls.to_string());
                    }
                }
            }
        }
        suppressed += chunk
            .get("echoSuppressedUtterances")
            .and_then(Value::as_i64)
            .unwrap_or(0);
        if let Some(ws) = chunk.get("words").and_then(Value::as_array) {
            words_vec.extend(ws.iter().cloned());
        }
        if let Some(segs) = chunk.get("segments").and_then(Value::as_array) {
            turns.extend(segs.iter().cloned());
        }
        if let Some(acts) = chunk.get("activity").and_then(Value::as_array) {
            for row in acts {
                if let (Some(sp), Some(s), Some(e)) = (
                    row.get("speaker").and_then(Value::as_i64),
                    row.get("start").and_then(Value::as_f64),
                    row.get("end").and_then(Value::as_f64),
                ) {
                    intervals.entry(sp).or_default().push((s, e));
                }
            }
        }
    }

    if reconcile {
        turns.sort_by(|a, b| {
            let sa = a.get("start").and_then(Value::as_f64).unwrap_or(0.0);
            let sb = b.get("start").and_then(Value::as_f64).unwrap_or(0.0);
            sa.total_cmp(&sb)
        });
        words_vec.sort_by(|a, b| {
            let sa = a.get("start").and_then(Value::as_f64).unwrap_or(f64::INFINITY);
            let sb = b.get("start").and_then(Value::as_f64).unwrap_or(f64::INFINITY);
            sa.total_cmp(&sb)
        });
        if !turns.is_empty() {
            text_parts = turns
                .iter()
                .filter_map(|t| t.get("text").and_then(Value::as_str).map(String::from))
                .collect();
        }
    }

    let mut segments: Vec<Value> = Vec::new();
    for turn in turns {
        let sp = turn.get("speaker").cloned().unwrap_or(Value::Null);
        let t_text = turn.get("text").and_then(Value::as_str).unwrap_or("");
        let t_end = turn.get("end").and_then(Value::as_f64);
        if let Some(Value::Object(prev)) = segments.last_mut() {
            if prev.get("speaker").unwrap_or(&Value::Null) == &sp {
                let prev_text = prev.get("text").and_then(Value::as_str).unwrap_or("");
                let combined = format!("{prev_text} {t_text}");
                prev.insert("text".into(), Value::String(combined));
                if let Some(e) = t_end {
                    let prev_end = prev.get("end").and_then(Value::as_f64).unwrap_or(0.0);
                    prev.insert("end".into(), json!(e.max(prev_end)));
                }
                continue;
            }
        }
        segments.push(turn);
    }

    let mut sp_keys: Vec<i64> = intervals.keys().copied().collect();
    sp_keys.sort();
    let mut rows: Vec<(i64, Vec<Value>, f64)> = Vec::new();
    for sp in sp_keys {
        let mut spans = intervals.remove(&sp).unwrap_or_default();
        spans.sort_by(|a, b| a.0.total_cmp(&b.0).then_with(|| a.1.total_cmp(&b.1)));
        let mut merged: Vec<(f64, f64)> = Vec::new();
        for (s, e) in spans {
            let s = s.max(0.0);
            let e = e.min(seconds);
            if e <= s {
                continue;
            }
            if let Some(last) = merged.last_mut() {
                if s <= last.1 {
                    last.1 = last.1.max(e);
                    continue;
                }
            }
            merged.push((s, e));
        }
        if !merged.is_empty() {
            let secs: f64 = merged.iter().map(|(s, e)| e - s).sum();
            let ivs: Vec<Value> = merged.into_iter().map(|(s, e)| json!({"start": s, "end": e})).collect();
            rows.push((sp, ivs, secs));
        }
    }
    let total_secs: f64 = rows.iter().map(|(_, _, s)| *s).sum();
    let activity_rows: Vec<Value> = rows
        .into_iter()
        .map(|(sp, ivs, secs)| {
            json!({
                "speaker": sp,
                "intervals": ivs,
                "seconds": secs,
                "share": if total_secs > 0.0 { secs / total_secs } else { 0.0 },
            })
        })
        .collect();

    let mut m = Map::new();
    m.insert("text".into(), Value::String(text_parts.join(" ")));
    m.insert("durationSeconds".into(), json!(seconds));
    m.insert(
        "detectedLanguages".into(),
        Value::Array(languages.into_iter().map(Value::String).collect()),
    );
    m.insert("segments".into(), Value::Array(segments));
    m.insert("words".into(), Value::Array(words_vec));
    m.insert(
        "speakerActivity".into(),
        if speakers { Value::Array(activity_rows) } else { Value::Null },
    );
    if meeting {
        m.insert("speakerNames".into(), source_labels());
        m.insert("echoSuppressedUtterances".into(), json!(suppressed));
    }
    if let Some(err) = interruption {
        if !err.is_empty() {
            m.insert("interruption".into(), Value::String(err.to_string()));
        }
    }
    Value::Object(m)
}

fn valid_rolling_record(row: &Value, text: bool) -> bool {
    let Some(obj) = row.as_object() else { return false };
    if !obj.contains_key("start") || !obj.contains_key("end") || !obj.contains_key("speaker") {
        return false;
    }
    match obj.get("speaker") {
        Some(Value::Null) => {
            if !text {
                return false;
            }
        }
        Some(Value::Number(n)) => {
            let Some(i) = n.as_i64() else { return false };
            if !(1..=8).contains(&i) {
                return false;
            }
        }
        _ => return false,
    }
    if text {
        if !obj.get("text").is_some_and(Value::is_string) {
            return false;
        }
        for k in ["start", "end"] {
            match obj.get(k) {
                Some(Value::Null) => {}
                Some(Value::Number(n)) if n.as_f64().is_some_and(f64::is_finite) => {}
                _ => return false,
            }
        }
        true
    } else {
        midpoint(row).is_some()
    }
}

pub fn read_rolling(source: &Path, frames: i64) -> Vec<(i64, i64, Value)> {
    let path = source.with_extension("rolling.ndjson");
    if path.is_symlink() {
        return Vec::new();
    }
    let cpath = match CString::new(path.as_os_str().as_bytes()) {
        Ok(c) => c,
        Err(_) => return Vec::new(),
    };
    let fd = unsafe { libc::open(cpath.as_ptr(), libc::O_RDONLY | libc::O_NOFOLLOW) };
    if fd < 0 {
        return Vec::new();
    }
    let file = unsafe { File::from_raw_fd(fd) };
    let mut reader = BufReader::new(file);
    let mut last: i64 = 0;
    let mut out = Vec::new();
    loop {
        let mut line = Vec::new();
        let n = match reader.by_ref().take((MAX_ROLLING_LINE + 1) as u64).read_until(b'\n', &mut line) {
            Ok(n) => n,
            Err(_) => break,
        };
        if n == 0 || line.len() > MAX_ROLLING_LINE || !line.ends_with(b"\n") {
            break;
        }
        let Ok(Value::Array(arr)) = serde_json::from_slice::<Value>(&line) else {
            continue;
        };
        if arr.len() != 3 {
            continue;
        }
        let (Some(start), Some(mut end)) = (arr[0].as_i64(), arr[1].as_i64()) else {
            continue;
        };
        if !(last <= start && start < end) || start >= frames {
            continue;
        }
        let mut result = arr[2].clone();
        let Some(obj) = result.as_object() else { continue };
        if !obj.get("text").is_some_and(Value::is_string) {
            continue;
        }
        let (Some(segs), Some(ws), Some(acts)) = (
            obj.get("segments").and_then(Value::as_array),
            obj.get("words").and_then(Value::as_array),
            obj.get("activity").and_then(Value::as_array),
        ) else {
            continue;
        };
        if !segs.iter().all(|r| valid_rolling_record(r, true))
            || !ws.iter().all(|r| valid_rolling_record(r, true))
            || !acts.iter().all(|r| valid_rolling_record(r, false))
        {
            continue;
        }
        last = end;
        end = end.min(frames);
        if !segs.is_empty() && end < last {
            let limit = end as f64 / RECORDING_RATE;
            let trimmed = trim_chunks(vec![result], |_, off| off.is_some_and(|o| o < limit));
            if let Some(first) = trimmed.into_iter().next() {
                result = first;
            } else {
                continue;
            }
        }
        out.push((start, end, result));
    }
    out
}

// ---------------------------------------------------------------------------
// Interrupted drafts (staging/*.recording & microphone-drafts/*.recording)
// ---------------------------------------------------------------------------

fn read_meeting_title_file(path: &Path) -> Option<String> {
    let title_path = path.with_extension("meeting-title");
    let cpath = CString::new(title_path.as_os_str().as_bytes()).ok()?;
    let fd = unsafe { libc::open(cpath.as_ptr(), libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK) };
    if fd < 0 {
        return None;
    }
    let mut f = unsafe { File::from_raw_fd(fd) };
    let mut buf = [0u8; 481];
    let n = f.read(&mut buf).ok()?;
    if n > 480 {
        return None;
    }
    let raw = std::str::from_utf8(&buf[..n]).ok()?;
    clean_meeting_title(raw)
}

fn clean_meeting_title(value: &str) -> Option<String> {
    let title = value.split_whitespace().collect::<Vec<_>>().join(" ");
    if !title.chars().any(char::is_alphanumeric)
        || title.split_whitespace().count() > 8
        || title.chars().count() > 120
    {
        return None;
    }
    if title.chars().any(|c| c.is_control() && c != '\u{200c}' && c != '\u{200d}') {
        return None;
    }
    Some(title)
}

pub fn normalize_meeting_title(title: &str) -> Result<String, CliError> {
    let name = title.split_whitespace().collect::<Vec<_>>().join(" ");
    if name.is_empty()
        || name.chars().count() > 200
        || name.chars().any(|c| c.is_control() && c != '\u{200c}' && c != '\u{200d}')
    {
        return Err(CliError::value(
            "Enter a meeting title of 1 to 200 printable characters.",
        ));
    }
    Ok(name)
}

fn read_recording_metadata(file: &mut File) -> Map<String, Value> {
    let mut buf = vec![0u8; 32768];
    let n = file.read(&mut buf).unwrap_or(0);
    let text = String::from_utf8_lossy(&buf[..n]);
    if text.starts_with('{') {
        if let Ok(Value::Object(mut obj)) = serde_json::from_str::<Value>(&text) {
            if let Some(mt) = obj.get("meetingTitle").and_then(Value::as_str).map(String::from) {
                match normalize_meeting_title(&mt) {
                    Ok(cleaned) => {
                        obj.insert("meetingTitle".into(), Value::String(cleaned));
                    }
                    Err(_) => {
                        obj.remove("meetingTitle");
                    }
                }
            }
            return obj;
        }
    } else if text.len() >= 36 {
        if let Ok(u) = Uuid::parse_str(&text[..36]) {
            let mut m = Map::new();
            m.insert("notesDraft".into(), Value::String(u.hyphenated().to_string()));
            return m;
        }
    }
    Map::new()
}

pub fn interrupted_meetings(library: &SessionStore) -> Vec<(Value, PathBuf)> {
    let mut out = Vec::new();
    // 1. staging/*.recording
    let staging = library.root.join("staging");
    if let Ok(entries) = std::fs::read_dir(&staging) {
        let mut drafts: Vec<(f64, Value, PathBuf)> = Vec::new();
        for entry in entries.flatten() {
            let marker = entry.path();
            if marker.extension().and_then(|e| e.to_str()) != Some("recording") {
                continue;
            }
            let Some(stem) = marker.file_stem().and_then(|s| s.to_str()) else { continue };
            if !is_canonical_uuid(stem) {
                continue;
            }
            let path = staging.join(stem);
            if path.is_symlink() {
                continue;
            }
            let Ok(mut claim) = File::open(&marker) else { continue };
            if unsafe { libc::flock(claim.as_raw_fd(), libc::LOCK_SH | libc::LOCK_NB) } != 0 {
                continue;
            }
            let Ok(info) = std::fs::metadata(&path) else { continue };
            if info.len() <= 44 {
                continue;
            }
            let meta = read_recording_metadata(&mut claim);
            let provider = meta
                .get("meetingProvider")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty());
            let has_tracks = path.with_extension("mic.wav").is_file()
                || path.with_extension("system.wav").is_file();
            let is_meeting = provider.is_some() || has_tracks;
            if !is_meeting || library.exists(stem).unwrap_or(false) {
                continue;
            }
            let duration = ((info.len() - 44) / 2) as f64 / RECORDING_RATE;
            let modified = info.mtime() as f64 + (info.mtime_nsec() as f64) / 1e9;
            let provisional = read_meeting_title_file(&path);
            let row = build_interrupted_row(stem, duration, modified, &meta, provider, provisional);
            drafts.push((modified, row, path));
        }
        drafts.sort_by(|a, b| b.0.total_cmp(&a.0));
        out.extend(drafts.into_iter().map(|(_, r, p)| (r, p)));
    }
    // 2. microphone-drafts/*.recording
    let mic_dir = library.root.join("microphone-drafts");
    if let Ok(entries) = std::fs::read_dir(&mic_dir) {
        for entry in entries.flatten() {
            let marker = entry.path();
            if marker.extension().and_then(|e| e.to_str()) != Some("recording") || marker.is_symlink() {
                continue;
            }
            let Some(stem) = marker.file_stem().and_then(|s| s.to_str()) else { continue };
            if !is_canonical_uuid(stem) || library.exists(stem).unwrap_or(false) {
                continue;
            }
            let path = mic_dir.join(format!("{stem}.wav"));
            if !path.is_file() || path.is_symlink() {
                continue;
            }
            let Ok(mut claim) = File::open(&marker) else { continue };
            if unsafe { libc::flock(claim.as_raw_fd(), libc::LOCK_SH | libc::LOCK_NB) } != 0 {
                continue;
            }
            let meta = read_recording_metadata(&mut claim);
            let Some(provider) = meta
                .get("meetingProvider")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty()) else {
                continue;
            };
            let frames = portable_wav_frames(&path);
            if frames == 0 {
                continue;
            }
            let Ok(info) = std::fs::metadata(&path) else { continue };
            let duration = frames as f64 / PORTABLE_RECORDING_RATE;
            let modified = info.mtime() as f64 + (info.mtime_nsec() as f64) / 1e9;
            let row = build_interrupted_row(stem, duration, modified, &meta, Some(provider), None);
            out.push((row, path));
        }
    }
    out
}

fn portable_wav_frames(path: &Path) -> u64 {
    let Ok(mut f) = File::open(path) else { return 0 };
    let Ok(meta) = f.metadata() else { return 0 };
    let mut hdr = [0u8; 44];
    if f.read_exact(&mut hdr).is_err() {
        return 0;
    }
    if &hdr[0..4] != b"RIFF" || &hdr[8..16] != b"WAVEfmt " || &hdr[36..40] != b"data" {
        return 0;
    }
    let channels = u16::from_le_bytes([hdr[22], hdr[23]]);
    let rate = u32::from_le_bytes([hdr[24], hdr[25], hdr[26], hdr[27]]);
    let bits = u16::from_le_bytes([hdr[34], hdr[35]]);
    let data_bytes = u32::from_le_bytes([hdr[40], hdr[41], hdr[42], hdr[43]]) as u64;
    if channels != 1 || rate != 16_000 || bits != 16 {
        return 0;
    }
    let file_frames = meta.len().saturating_sub(44) / 2;
    (data_bytes / 2).min(file_frames)
}

fn build_interrupted_row(
    stem: &str,
    duration: f64,
    modified: f64,
    meta: &Map<String, Value>,
    provider_opt: Option<&str>,
    provisional: Option<String>,
) -> Value {
    let end_secs = modified.floor() as i64;
    let end_nanos = ((modified - modified.floor()) * 1e9).clamp(0.0, 999_999_999.0) as u32;
    let mut end = DateTime::<Utc>::from_timestamp(end_secs, end_nanos).unwrap_or_else(Utc::now);
    let dur = Duration::nanoseconds((duration * 1e9) as i64);
    let mut start = end - dur;
    if let Some(started_str) = meta.get("meetingStartedAt").and_then(Value::as_str) {
        if let Ok(dt) = DateTime::parse_from_rfc3339(started_str) {
            if dt.offset().local_minus_utc() == 0 {
                let utc = dt.with_timezone(&Utc);
                let yr = chrono::Datelike::year(&utc);
                if (2000..9999).contains(&yr) {
                    start = utc;
                    end = utc + dur;
                }
            }
        }
    }
    let provider = provider_opt.unwrap_or("Meeting");
    let calendar = meta.get("meetingCalendar").filter(|v| v.is_object()).cloned();
    let cal_title = calendar
        .as_ref()
        .and_then(|c| c.get("title"))
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty());
    let meeting_title = meta
        .get("meetingTitle")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty());
    let name = meeting_title
        .or(cal_title)
        .or(provisional.as_deref())
        .map(String::from)
        .unwrap_or_else(|| {
            if provider != "Meeting" {
                format!("{provider} meeting")
            } else {
                provider.to_string()
            }
        });
    let start_iso = start.to_rfc3339_opts(chrono::SecondsFormat::AutoSi, false);
    let end_iso = end.to_rfc3339_opts(chrono::SecondsFormat::AutoSi, false);
    let mut row = Map::new();
    row.insert("id".into(), Value::String(stem.to_string()));
    row.insert("name".into(), Value::String(name));
    row.insert("meetingProvider".into(), Value::String(provider.to_string()));
    row.insert("meeting".into(), Value::Bool(true));
    row.insert("meetingCalendar".into(), calendar.unwrap_or(Value::Null));
    row.insert("created".into(), Value::String(start_iso.clone()));
    if meta.get("meetingMicrophoneWarning").and_then(Value::as_str) == Some("digitalSilence") {
        row.insert("meetingMicrophoneWarning".into(), Value::String("digitalSilence".into()));
    }
    if let Some(pt) = provisional {
        row.insert("provisionalTitle".into(), Value::String(pt));
    }
    row.insert("meetingStartedAt".into(), Value::String(start_iso));
    row.insert("meetingEndedAt".into(), Value::String(end_iso));
    row.insert("duration".into(), json!(duration));
    row.insert("processed".into(), json!(0));
    row.insert("status".into(), Value::String("interrupted".into()));
    Value::Object(row)
}

// ---------------------------------------------------------------------------
// Saved meetings: list, get, detail, search, exports (saved_meetings.py)
// ---------------------------------------------------------------------------

fn meeting_span(row: &Value) -> (DateTime<Utc>, DateTime<Utc>) {
    let saved = row
        .get("created")
        .and_then(Value::as_str)
        .and_then(parse_iso_utc)
        .unwrap_or_else(Utc::now);
    let dur_secs = row
        .get("duration")
        .and_then(Value::as_f64)
        .filter(|&d| d != 0.0)
        .or_else(|| row.get("processed").and_then(Value::as_f64))
        .unwrap_or(0.0);
    let dur = Duration::nanoseconds((dur_secs * 1e9) as i64);
    let start = row
        .get("meetingStartedAt")
        .and_then(Value::as_str)
        .and_then(parse_iso_utc)
        .unwrap_or(saved - dur);
    let end = row
        .get("meetingEndedAt")
        .and_then(Value::as_str)
        .and_then(parse_iso_utc)
        .unwrap_or(start + dur);
    (start, end)
}

#[allow(clippy::too_many_arguments)]
pub fn meetings(
    library: &SessionStore,
    query: &str,
    limit: Option<usize>,
    since: Option<&str>,
    until: Option<&str>,
    live: Option<&Value>,
    include_in_progress: bool,
    meetings_only: bool,
) -> Result<Vec<Value>, CliError> {
    let now = Utc::now();
    let first = date_bound(since, now, false)?;
    let last = date_bound(until, now, true)?;
    if let (Some(f), Some(l)) = (first, last) {
        if f >= l {
            return Err(CliError::value("The until time must be after the since time."));
        }
    }
    let mut rows = library.list("", meetings_only)?;
    let saved: HashSet<String> = rows
        .iter()
        .filter_map(|r| r.get("id").and_then(Value::as_str).map(String::from))
        .collect();
    let live_id = live.and_then(|l| l.get("id")).and_then(Value::as_str);
    for (draft, _) in interrupted_meetings(library) {
        let did = draft.get("id").and_then(Value::as_str).unwrap_or("");
        if !saved.contains(did) && live_id != Some(did) {
            rows.push(draft);
        }
    }
    if let Some(live_obj) = live {
        if let Some(lid) = live_id {
            let already_done = rows.iter().any(|r| {
                r.get("id").and_then(Value::as_str) == Some(lid)
                    && r.get("status").and_then(Value::as_str) != Some("processing")
            });
            if !already_done {
                let mut clean_live = live_obj.as_object().cloned().unwrap_or_default();
                clean_live.remove("meetingTurns");
                let mut merged = Vec::with_capacity(rows.len() + 1);
                merged.push(Value::Object(clean_live));
                for r in rows {
                    if r.get("id").and_then(Value::as_str) != Some(lid) {
                        merged.push(r);
                    }
                }
                rows = merged;
            }
        }
    }
    let terms: HashSet<String> = words(query).into_iter().collect();
    let mut matches: Vec<(DateTime<Utc>, String, Value)> = Vec::new();
    for row in rows {
        let status = row.get("status").and_then(Value::as_str).unwrap_or("");
        if !include_in_progress && (status == "recording" || status == "processing") {
            continue;
        }
        let (start, end) = meeting_span(&row);
        if first.is_some_and(|f| end < f) || last.is_some_and(|l| start >= l) {
            continue;
        }
        let searchable = [
            row.get("name").and_then(Value::as_str),
            row.get("summaryTitle").and_then(Value::as_str),
            row.get("provisionalTitle").and_then(Value::as_str),
            row.get("meetingProvider").and_then(Value::as_str),
        ]
        .into_iter()
        .flatten()
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
        let row_words: HashSet<String> = words(&searchable).into_iter().collect();
        if terms.is_subset(&row_words) {
            let id = row.get("id").and_then(Value::as_str).unwrap_or("").to_string();
            matches.push((start, id, row));
        }
    }
    matches.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| b.1.cmp(&a.1)));
    if let Some(lim) = limit {
        matches.truncate(lim);
    }
    Ok(matches.into_iter().map(|(_, _, r)| r).collect())
}

pub fn get_meeting(
    library: &SessionStore,
    identifier: &str,
    turns: bool,
    live: Option<&Value>,
    staged: bool,
    meetings_only: bool,
) -> Result<Value, CliError> {
    let resolved_id: String = if identifier == "latest" {
        let newest = meetings(library, "", Some(1), None, None, live, true, meetings_only)?;
        let Some(first) = newest.first() else {
            return Err(CliError::missing(identifier));
        };
        first.get("id").and_then(Value::as_str).unwrap_or("").to_string()
    } else if let Ok(u) = Uuid::parse_str(identifier) {
        u.hyphenated().to_string()
    } else {
        if !RE_UUID_PREFIX.is_match(identifier) {
            return Err(CliError::missing(identifier));
        }
        let prefix = identifier.to_ascii_lowercase();
        let all = meetings(library, "", None, None, None, live, true, meetings_only)?;
        let found: Vec<String> = all
            .into_iter()
            .filter_map(|r| r.get("id").and_then(Value::as_str).map(String::from))
            .filter(|id| id.starts_with(&prefix))
            .collect();
        if found.len() > 1 {
            let subject = if meetings_only { "meeting" } else { "session" };
            return Err(CliError::value(format!(
                "That {subject} ID prefix matches more than one {subject}."
            )));
        }
        let Some(one) = found.into_iter().next() else {
            return Err(CliError::missing(identifier));
        };
        one
    };

    let mut session = match library.get(&resolved_id, turns) {
        Ok(s) => Some(s),
        Err(CliError::Missing(_)) => None,
        Err(e) => return Err(e),
    };

    if session.is_none() {
        if let Some(live_obj) = live {
            if live_obj.get("id").and_then(Value::as_str) == Some(resolved_id.as_str()) {
                let segs = live_obj
                    .get("meetingTurns")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default();
                let labels = if live_obj.get("meetingSpeakerMode").and_then(Value::as_str) == Some(MIXED_SPEAKERS) {
                    json!({})
                } else {
                    source_labels()
                };
                let mut m = live_obj.as_object().cloned().unwrap_or_default();
                let dur = live_obj.get("duration").cloned().unwrap_or(json!(0));
                m.insert(
                    "transcript".into(),
                    json!({
                        "durationSeconds": dur,
                        "segments": segs,
                        "speakerNames": labels,
                        "text": "",
                        "transcriptSource": "rolling",
                    }),
                );
                return Ok(Value::Object(m));
            }
        }
    }

    if session.is_none() {
        if !staged {
            return Err(CliError::missing(&resolved_id));
        }
        let found = interrupted_meetings(library)
            .into_iter()
            .find(|(d, _)| d.get("id").and_then(Value::as_str) == Some(resolved_id.as_str()));
        let Some((draft, source)) = found else {
            return library.get(&resolved_id, turns);
        };
        let mut mixed = false;
        let mut segments = Vec::new();
        if turns {
            let dur = draft.get("duration").and_then(Value::as_f64).unwrap_or(0.0);
            let frames = py_round_i64(dur * RECORDING_RATE);
            for (_, _, res) in read_rolling(&source, frames) {
                if res.get("meetingSpeakerMode").and_then(Value::as_str) == Some(MIXED_SPEAKERS) {
                    mixed = true;
                }
                if let Some(segs) = res.get("segments").and_then(Value::as_array) {
                    segments.extend(segs.iter().cloned());
                }
            }
        }
        session = match library.get(&resolved_id, turns) {
            Ok(s) => Some(s),
            Err(CliError::Missing(_)) => {
                let mut m = draft.as_object().cloned().unwrap_or_default();
                let dur = draft.get("duration").cloned().unwrap_or(json!(0));
                let src = if segments.is_empty() { "draft" } else { "rolling" };
                m.insert("staged".into(), Value::Bool(true));
                m.insert(
                    "transcript".into(),
                    json!({
                        "durationSeconds": dur,
                        "segments": segments,
                        "speakerNames": if mixed { json!({}) } else { source_labels() },
                        "text": "",
                        "transcriptSource": src,
                    }),
                );
                return Ok(Value::Object(m));
            }
            Err(e) => return Err(e),
        };
    }

    let session = session.unwrap();
    if meetings_only && !session.get("meeting").and_then(Value::as_bool).unwrap_or(false) {
        return Err(CliError::missing(&resolved_id));
    }
    Ok(session)
}

pub fn document_text(doc: &Value) -> String {
    fn render(node: &Value) -> String {
        let kind = node.get("type").and_then(Value::as_str).unwrap_or("");
        let children = node
            .get("content")
            .and_then(Value::as_array)
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        match kind {
            "text" => node.get("text").and_then(Value::as_str).unwrap_or("").to_string(),
            "hardBreak" => "\n".to_string(),
            "paragraph" | "heading" => {
                let text: String = children.iter().map(render).collect();
                if kind == "heading" && !text.trim().is_empty() {
                    let level = node
                        .get("attrs")
                        .and_then(|a| a.get("level"))
                        .and_then(Value::as_u64)
                        .unwrap_or(1) as usize;
                    format!("{} {text}", "#".repeat(level))
                } else {
                    text
                }
            }
            "bulletList" | "orderedList" => {
                let start = node
                    .get("attrs")
                    .and_then(|a| a.get("start"))
                    .and_then(Value::as_i64)
                    .unwrap_or(1);
                let mut out = Vec::new();
                for (idx, child) in children.iter().enumerate() {
                    let item = render(child);
                    if !item.trim().is_empty() {
                        let prefix = if kind == "bulletList" {
                            "- ".to_string()
                        } else {
                            format!("{}. ", start + idx as i64)
                        };
                        out.push(format!("{prefix}{}", item.replace('\n', "\n  ")));
                    }
                }
                out.join("\n")
            }
            "taskList" => {
                let mut out = Vec::new();
                for child in children {
                    let item = render(child);
                    if !item.trim().is_empty() {
                        let checked = child
                            .get("attrs")
                            .and_then(|a| a.get("checked"))
                            .and_then(Value::as_bool)
                            .unwrap_or(false);
                        let mark = if checked { "x" } else { " " };
                        out.push(format!("- [{mark}] {}", item.replace('\n', "\n  ")));
                    }
                }
                out.join("\n")
            }
            _ => {
                let text = children
                    .iter()
                    .map(render)
                    .filter(|s| !s.trim().is_empty())
                    .collect::<Vec<_>>()
                    .join("\n\n");
                if kind == "blockquote" {
                    text.lines()
                        .map(|l| format!("> {l}"))
                        .collect::<Vec<_>>()
                        .join("\n")
                } else {
                    text
                }
            }
        }
    }
    render(doc).trim().to_string()
}

pub fn session_notes_obj(session: &Value) -> Value {
    let overview_ver = session
        .get("overview")
        .and_then(|o| o.get("version"))
        .and_then(Value::as_object);
    let mut ov_parts = Vec::new();
    if let Some(ov) = overview_ver {
        if let Some(s) = ov.get("summary").and_then(Value::as_str).filter(|s| !s.is_empty()) {
            ov_parts.push(s);
        }
        if let Some(m) = ov.get("markdown").and_then(Value::as_str).filter(|s| !s.is_empty()) {
            ov_parts.push(m);
        }
    }
    let personal_doc = session
        .get("personalNotes")
        .and_then(|p| p.get("document"))
        .cloned()
        .unwrap_or_else(|| json!({"type": "doc", "content": []}));
    let mut m = Map::new();
    if let Some(summary) = session
        .get("meetingBrief")
        .and_then(|b| b.get("summary"))
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        m.insert("summary".into(), Value::String(summary.to_string()));
    }
    m.insert("overview".into(), Value::String(ov_parts.join("\n\n")));
    m.insert("personalNotes".into(), Value::String(document_text(&personal_doc)));
    Value::Object(m)
}

fn resolve_speaker_label(speaker: Option<&Value>, labels: Option<&Map<String, Value>>) -> Option<String> {
    let sp = speaker?;
    if sp.is_null() {
        return None;
    }
    let sp_str = match sp {
        Value::String(s) => s.clone(),
        Value::Number(n) => n.to_string(),
        _ => return None,
    };
    if let Some(map) = labels {
        if let Some(raw) = map.get(&sp_str).and_then(Value::as_str) {
            let cleaned = raw.split_whitespace().collect::<Vec<_>>().join(" ");
            if !cleaned.is_empty() {
                return Some(cleaned);
            }
        }
    }
    Some(format!("Speaker {sp_str}"))
}

pub fn meeting_detail(
    session: &Value,
    start_arg: Option<&str>,
    end_arg: Option<&str>,
    speaker_filter: Option<&str>,
) -> Result<Value, CliError> {
    let start_ms = parse_offset(start_arg)?;
    let end_ms = parse_offset(end_arg)?;
    if let Some(e) = end_ms {
        if e <= start_ms.unwrap_or(0) {
            return Err(CliError::value("The end time must be after the start time."));
        }
    }
    let transcript = session
        .get("transcript")
        .and_then(Value::as_object)
        .ok_or_else(|| CliError::value("Missing transcript."))?;
    let duration = transcript.get("durationSeconds").and_then(Value::as_f64);
    let labels = transcript.get("speakerNames").and_then(Value::as_object);

    let mut available: Vec<Value> = session
        .get("meetingTurns")
        .and_then(Value::as_array)
        .filter(|a| !a.is_empty())
        .cloned()
        .unwrap_or_else(|| {
            transcript
                .get("segments")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default()
        });

    let status = session.get("status").and_then(Value::as_str).unwrap_or("");
    if let Some(s_ms) = start_ms {
        if s_ms > 0 {
            let max_turn = available
                .iter()
                .filter_map(|t| {
                    t.get("end")
                        .and_then(Value::as_f64)
                        .or_else(|| t.get("start").and_then(Value::as_f64))
                })
                .fold(0.0f64, f64::max);
            let mut extent = duration.unwrap_or(0.0).max(max_turn);
            let recording = status == "recording";
            if recording {
                if let Some(began) = session
                    .get("meetingStartedAt")
                    .and_then(Value::as_str)
                    .and_then(parse_iso_utc)
                {
                    let elapsed_secs = (Utc::now() - began).num_milliseconds() as f64 / 1000.0;
                    extent = extent.max(elapsed_secs);
                }
            }
            let elapsed = py_round_i64(extent * 1000.0).max(0) as u64;
            if s_ms >= elapsed {
                let progress = if recording { " so far" } else { "" };
                return Err(CliError::value(format!(
                    "The start time {} is at or beyond the meeting's end at {}{progress}.",
                    range_clock(s_ms),
                    range_clock(elapsed),
                )));
            }
        }
    }

    if available.is_empty()
        && start_ms.is_none()
        && end_ms.is_none()
        && speaker_filter.is_none()
    {
        if let Some(txt) = transcript.get("text").and_then(Value::as_str) {
            if !txt.is_empty() {
                available.push(json!({"speaker": null, "start": null, "end": null, "text": txt}));
            }
        }
    }

    available.sort_by(|a, b| {
        let sa = a.get("start").and_then(Value::as_f64).unwrap_or(0.0);
        let sb = b.get("start").and_then(Value::as_f64).unwrap_or(0.0);
        sa.total_cmp(&sb)
    });

    let speaker_folded = speaker_filter.map(casefold);
    let mut turns = Vec::new();
    for turn in available {
        let first = turn.get("start").and_then(Value::as_f64);
        let last = turn.get("end").and_then(Value::as_f64);
        if let Some(s_ms) = start_ms {
            let cmp_val = last.or(first).unwrap_or(0.0);
            if py_round_i64(cmp_val * 1000.0) <= s_ms as i64 {
                continue;
            }
        }
        if let Some(e_ms) = end_ms {
            match first {
                None => continue,
                Some(f) if py_round_i64(f * 1000.0) >= e_ms as i64 => continue,
                _ => {}
            }
        }
        let name = resolve_speaker_label(turn.get("speaker"), labels);
        if let Some(ref sf) = speaker_folded {
            if name.as_deref().map(casefold).as_deref() != Some(sf.as_str()) {
                continue;
            }
        }
        turns.push(json!({
            "start": first,
            "end": last,
            "speaker": name,
            "text": turn.get("text").and_then(Value::as_str).unwrap_or(""),
        }));
    }

    let t_source = transcript
        .get("transcriptSource")
        .and_then(Value::as_str)
        .unwrap_or(if status == "complete" {
            "final"
        } else if status == "recording" || status == "processing" {
            "rolling"
        } else {
            "draft"
        });

    let mut out = Map::new();
    out.insert("id".into(), session.get("id").cloned().unwrap_or(Value::Null));
    out.insert("title".into(), session.get("name").cloned().unwrap_or(Value::Null));
    out.insert("created".into(), session.get("created").cloned().unwrap_or(Value::Null));
    out.insert("provider".into(), session.get("meetingProvider").cloned().unwrap_or(Value::Null));
    out.insert("status".into(), Value::String(status.to_string()));
    out.insert("duration".into(), opt_f64_to_json(duration));
    out.insert("transcriptSource".into(), Value::String(t_source.to_string()));
    out.insert("startedAt".into(), session.get("meetingStartedAt").cloned().unwrap_or(Value::Null));
    out.insert("endedAt".into(), session.get("meetingEndedAt").cloned().unwrap_or(Value::Null));
    out.insert("audioRemovedAt".into(), session.get("audioRemovedAt").cloned().unwrap_or(Value::Null));
    out.insert("calendar".into(), session.get("meetingCalendar").cloned().unwrap_or(Value::Null));
    out.insert(
        "finalizationPending".into(),
        Value::Bool(session.get("finalizationPending").and_then(Value::as_bool).unwrap_or(false)),
    );
    if start_ms.is_some() || end_ms.is_some() {
        out.insert(
            "range".into(),
            json!({"from_ms": start_ms.unwrap_or(0), "to_ms": end_ms}),
        );
    }
    if session.get("meetingMicrophoneWarning").and_then(Value::as_str) == Some("digitalSilence") {
        out.insert("microphoneWarning".into(), Value::String("digitalSilence".into()));
    }
    if session.get("staged").and_then(Value::as_bool).unwrap_or(false) {
        out.insert("staged".into(), Value::Bool(true));
    }
    if let Some(st) = session
        .get("meetingBrief")
        .and_then(|b| b.get("title"))
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        out.insert("summaryTitle".into(), Value::String(st.to_string()));
    }
    if let Some(pt) = session
        .get("provisionalTitle")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        out.insert("provisionalTitle".into(), Value::String(pt.to_string()));
    }
    out.insert("notes".into(), session_notes_obj(session));
    out.insert("turns".into(), Value::Array(turns));
    Ok(Value::Object(out))
}

fn transcript_passages(turns: &[Value], gap: f64, word_limit: Option<usize>) -> Vec<Value> {
    let mut out = Vec::new();
    let mut previous: Option<Map<String, Value>> = None;
    let mut count = 0usize;
    for turn in turns {
        let t_text = turn.get("text").and_then(Value::as_str).unwrap_or("");
        let length = t_text.split_whitespace().count();
        let can_merge = if let Some(ref prev) = previous {
            let p_end = prev.get("end").and_then(Value::as_f64);
            let t_start = turn.get("start").and_then(Value::as_f64);
            let t_end = turn.get("end").and_then(Value::as_f64);
            match (p_end, t_start, t_end) {
                (Some(pe), Some(ts), Some(_)) => {
                    prev.get("speaker") == turn.get("speaker")
                        && (ts - pe) <= gap
                        && word_limit.map_or(true, |lim| count + length <= lim)
                }
                _ => false,
            }
        } else {
            false
        };
        if can_merge {
            let prev = previous.as_mut().unwrap();
            let pe = prev.get("end").and_then(Value::as_f64).unwrap();
            let te = turn.get("end").and_then(Value::as_f64).unwrap();
            prev.insert("end".into(), json!(pe.max(te)));
            let old = prev.get("text").and_then(Value::as_str).unwrap_or("");
            prev.insert("text".into(), Value::String(format!("{old} {t_text}")));
            count += length;
            continue;
        }
        if let Some(prev) = previous.take() {
            out.push(Value::Object(prev));
        }
        previous = Some(turn.as_object().cloned().unwrap_or_default());
        count = length;
    }
    if let Some(prev) = previous {
        out.push(Value::Object(prev));
    }
    out
}

fn speech_excerpt(turn: &Value, before: bool) -> Value {
    let joined = turn
        .get("text")
        .and_then(Value::as_str)
        .unwrap_or("")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    let chars: Vec<char> = joined.chars().collect();
    let text = if chars.len() > 120 {
        if before {
            let tail: String = chars[chars.len() - 119..].iter().collect();
            let after_space = tail.split_once(' ').map(|(_, r)| r).filter(|r| !r.is_empty());
            format!("…{}", after_space.unwrap_or(&tail))
        } else {
            let head: String = chars[..119].iter().collect();
            let before_space = head.rsplit_once(' ').map(|(l, _)| l).filter(|l| !l.is_empty());
            format!("{}…", before_space.unwrap_or(&head))
        }
    } else {
        joined
    };
    let mut m = turn.as_object().cloned().unwrap_or_default();
    m.insert("text".into(), Value::String(text));
    Value::Object(m)
}

#[allow(clippy::too_many_arguments)]
pub fn meeting_search_report(
    library: &SessionStore,
    query: &str,
    limit: usize,
    identifier: Option<&str>,
    since: Option<&str>,
    until: Option<&str>,
    live: Option<&Value>,
    include_in_progress: bool,
    fuzzy: bool,
    meetings_only: bool,
    vocabulary: &[(String, Vec<String>)],
) -> Result<Value, CliError> {
    let key = match identifier {
        Some(ident) => Some(
            get_meeting(library, ident, false, live, true, meetings_only)?
                .get("id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
        ),
        None => None,
    };
    let rows = meetings(
        library,
        "",
        None,
        since,
        until,
        live,
        include_in_progress,
        meetings_only,
    )?;

    struct PassageMeta {
        payload: Map<String, Value>,
        turn_index: Option<usize>,
    }

    let mut passage_inputs: Vec<(String, String)> = Vec::new();
    let mut passage_metas: Vec<PassageMeta> = Vec::new();
    let mut details: HashMap<String, Map<String, Value>> = HashMap::new();
    let mut speech: HashMap<String, (DateTime<Utc>, Vec<Value>)> = HashMap::new();

    for row in rows {
        let rid = row.get("id").and_then(Value::as_str).unwrap_or("");
        if let Some(ref want) = key {
            if rid != want {
                continue;
            }
        }
        let session = match get_meeting(library, rid, true, live, true, meetings_only) {
            Ok(s) => s,
            Err(CliError::Missing(_)) => continue,
            Err(e) => return Err(e),
        };
        let detail = meeting_detail(&session, None, None, None)?;
        let did = detail.get("id").and_then(Value::as_str).unwrap_or("").to_string();
        let dtitle = detail.get("title").and_then(Value::as_str).unwrap_or("").to_string();
        let dstatus = detail.get("status").and_then(Value::as_str).unwrap_or("").to_string();
        let dsource = detail
            .get("transcriptSource")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let staged = detail.get("staged").and_then(Value::as_bool).unwrap_or(false);
        let route = if staged {
            None
        } else {
            Some(format!("/?session={did}&tab=transcript"))
        };

        let mut dmeta = Map::new();
        dmeta.insert("id".into(), Value::String(did.clone()));
        dmeta.insert("title".into(), Value::String(dtitle.clone()));
        dmeta.insert("status".into(), Value::String(dstatus.clone()));
        dmeta.insert("transcriptSource".into(), Value::String(dsource.clone()));
        details.insert(did.clone(), dmeta);

        let raw_turns = detail
            .get("turns")
            .and_then(Value::as_array)
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        let turns = transcript_passages(raw_turns, 2.5, Some(120));
        let start_iso = detail
            .get("startedAt")
            .and_then(Value::as_str)
            .or_else(|| detail.get("created").and_then(Value::as_str))
            .unwrap_or("");
        let start_dt = parse_iso_utc(start_iso).unwrap_or_else(Utc::now);

        for (index, turn) in turns.iter().enumerate() {
            let mut m = Map::new();
            m.insert("id".into(), Value::String(did.clone()));
            m.insert("title".into(), Value::String(dtitle.clone()));
            m.insert("status".into(), Value::String(dstatus.clone()));
            if let Some(tobj) = turn.as_object() {
                for (k, v) in tobj {
                    m.insert(k.clone(), v.clone());
                }
            }
            m.insert("source".into(), Value::String("transcript".into()));
            m.insert("transcriptSource".into(), Value::String(dsource.clone()));
            let stamp = turn.get("start").and_then(Value::as_f64);
            let turn_route = match (&route, stamp) {
                (Some(r), Some(s)) => Value::String(format!("{r}&at={}", format_py_float(s))),
                (Some(r), None) => Value::String(r.clone()),
                (None, _) => Value::Null,
            };
            m.insert("route".into(), turn_route);
            let text = turn.get("text").and_then(Value::as_str).unwrap_or("").to_string();
            passage_inputs.push((did.clone(), text));
            passage_metas.push(PassageMeta {
                payload: m,
                turn_index: Some(index),
            });
        }
        speech.insert(did.clone(), (start_dt, turns));

        if let Some(notes_obj) = detail.get("notes").and_then(Value::as_object) {
            for src in ["summary", "overview", "personalNotes"] {
                let Some(note) = notes_obj.get(src).and_then(Value::as_str) else { continue };
                let tab = if src == "personalNotes" { "notes" } else { "overview" };
                for paragraph in RE_PARAGRAPH_SPLIT.split(note) {
                    if paragraph.trim().is_empty() {
                        continue;
                    }
                    let mut m = Map::new();
                    m.insert("id".into(), Value::String(did.clone()));
                    m.insert("title".into(), Value::String(dtitle.clone()));
                    m.insert("status".into(), Value::String(dstatus.clone()));
                    m.insert("source".into(), Value::String(src.to_string()));
                    m.insert("text".into(), Value::String(paragraph.to_string()));
                    m.insert("start".into(), Value::Null);
                    m.insert("end".into(), Value::Null);
                    m.insert("speaker".into(), Value::Null);
                    m.insert("route".into(), Value::String(format!("/?session={did}&tab={tab}")));
                    passage_inputs.push((did.clone(), paragraph.to_string()));
                    passage_metas.push(PassageMeta {
                        payload: m,
                        turn_index: None,
                    });
                }
            }
        }
    }

    let (hits, counts) = rank_passages(&passage_inputs, query, limit, fuzzy, false, vocabulary)?;
    let mut out_hits = Vec::with_capacity(hits.len());
    for hit in hits {
        let meta = &passage_metas[hit.index];
        let mut obj = meta.payload.clone();
        obj.insert("snippet".into(), Value::String(hit.snippet));
        obj.insert("score".into(), json!(hit.score));
        if let Some(tidx) = meta.turn_index {
            let hid = obj.get("id").and_then(Value::as_str).unwrap_or("");
            if let Some((start_dt, turns)) = speech.get(hid) {
                if let Some(s) = obj.get("start").and_then(Value::as_f64) {
                    let abs = *start_dt + Duration::nanoseconds((s * 1e9) as i64);
                    obj.insert("absoluteTime".into(), Value::String(format_local_iso_millis(abs)));
                }
                if tidx > 0 {
                    obj.insert("before".into(), speech_excerpt(&turns[tidx - 1], true));
                }
                if tidx + 1 < turns.len() {
                    obj.insert("after".into(), speech_excerpt(&turns[tidx + 1], false));
                }
            }
        }
        out_hits.push(Value::Object(obj));
    }

    let total_matches: usize = counts.iter().map(|(_, c)| *c).sum();
    let matching_meetings: Vec<Value> = counts
        .into_iter()
        .filter_map(|(k, count)| {
            let mut m = details.get(&k)?.clone();
            m.insert("matchCount".into(), json!(count));
            Some(Value::Object(m))
        })
        .collect();

    Ok(json!({
        "passages": out_hits,
        "totalMatches": total_matches,
        "searchedMeetings": details.len(),
        "matchingMeetings": matching_meetings,
    }))
}

fn format_py_float(v: f64) -> String {
    if v.fract() == 0.0 && v.abs() < 1e16 {
        format!("{:.1}", v)
    } else {
        format!("{v}")
    }
}

fn markdown_turns(turns: &[Value]) -> Vec<String> {
    turns
        .iter()
        .map(|turn| {
            let ts = turn.get("start").and_then(Value::as_f64).map(|s| timestamp(s, false, false));
            let sp = turn.get("speaker").and_then(Value::as_str).filter(|s| !s.is_empty());
            let mut parts = Vec::new();
            if let Some(t) = ts {
                parts.push(t);
            }
            if let Some(s) = sp {
                parts.push(s.to_string());
            }
            let text = turn.get("text").and_then(Value::as_str).unwrap_or("");
            if parts.is_empty() {
                text.to_string()
            } else {
                format!("**{}**  \n{text}", parts.join(" · "))
            }
        })
        .collect()
}

pub fn render_export(
    detail: &Value,
    kind: &str,
    use_24_hour: bool,
    include_date: bool,
    companion: bool,
) -> String {
    let title = detail
        .get("title")
        .and_then(Value::as_str)
        .unwrap_or("")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    let attendees: Vec<&str> = detail
        .get("calendar")
        .and_then(|c| c.get("attendees"))
        .and_then(Value::as_array)
        .map(|arr| arr.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default();
    let attendance = if attendees.is_empty() {
        None
    } else {
        Some(format!("**Attendees** · {}", attendees.join(", ")))
    };

    let notes_obj = detail.get("notes").and_then(Value::as_object);
    let summary = notes_obj
        .and_then(|n| n.get("summary"))
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty());
    let summary_title = detail
        .get("summaryTitle")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty());
    let summary_section = summary.map(|sum| {
        let mut parts = vec!["## Summary".to_string()];
        if let Some(st) = summary_title {
            if st != title {
                parts.push(format!("**{st}**"));
            }
        }
        parts.push(sum.to_string());
        parts.push("_Written on this Mac by Jard’s local Qwen model._".to_string());
        parts.join("\n\n")
    });

    let date_line = if include_date && (kind == "md" || kind == "notes") {
        let iso = detail
            .get("startedAt")
            .and_then(Value::as_str)
            .or_else(|| detail.get("created").and_then(Value::as_str))
            .unwrap_or("");
        Some(meeting_date_line(iso, use_24_hour))
    } else {
        None
    };

    let mut notes_sections = Vec::new();
    if kind == "notes" || companion {
        for (key, label) in [("overview", "Overview"), ("personalNotes", "My Notes")] {
            if let Some(body) = notes_obj
                .and_then(|n| n.get(key))
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
            {
                notes_sections.push(format!("## {label}\n\n{body}"));
            }
        }
    }

    if kind == "notes" {
        let mut rows = vec![format!("# {title}")];
        if let Some(dl) = date_line {
            rows.push(dl);
        }
        if let Some(att) = attendance {
            rows.push(att);
        }
        if let Some(ss) = summary_section {
            rows.push(ss);
        }
        rows.extend(notes_sections);
        return rows.join("\n\n") + "\n";
    }

    if kind == "md" {
        let mut rows = vec![format!("# {title}")];
        if let Some(dl) = date_line {
            rows.push(dl);
        }
        if let Some(att) = attendance {
            rows.push(att);
        }
        if companion {
            let status = detail.get("status").and_then(Value::as_str).unwrap_or("");
            let mut chars = status.chars();
            let cap = match chars.next() {
                None => String::new(),
                Some(f) => f.to_uppercase().collect::<String>() + chars.as_str(),
            };
            let tsrc = detail
                .get("transcriptSource")
                .and_then(Value::as_str)
                .unwrap_or("");
            rows.push(format!("- **Status:** {cap} · {tsrc} transcript"));
        }
        if let Some(bounds) = detail.get("range").and_then(Value::as_object) {
            let from_ms = bounds.get("from_ms").and_then(Value::as_u64).unwrap_or(0);
            let to = match bounds.get("to_ms").and_then(Value::as_u64) {
                None => "end".to_string(),
                Some(ms) => range_clock(ms),
            };
            rows.push(format!("- **Range:** {}–{to}", range_clock(from_ms)));
        }
        let has_summary = summary_section.is_some();
        if let Some(ss) = summary_section {
            rows.push(ss);
        }
        rows.extend(notes_sections);
        if has_summary || companion {
            rows.push("## Transcript".to_string());
        }
        let turns = detail
            .get("turns")
            .and_then(Value::as_array)
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        rows.extend(markdown_turns(turns));
        return rows.join("\n\n") + "\n";
    }

    // srt or vtt
    let is_srt = kind == "srt";
    let is_vtt = kind == "vtt";
    let turns = detail
        .get("turns")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let mut cues = Vec::new();
    for turn in turns {
        let (Some(start), Some(end)) = (
            turn.get("start").and_then(Value::as_f64),
            turn.get("end").and_then(Value::as_f64),
        ) else {
            continue;
        };
        if end <= start {
            continue;
        }
        let first = timestamp(start, true, is_srt);
        let last = timestamp(end, true, is_srt);
        if first == last {
            continue;
        }
        let mut body = turn
            .get("text")
            .and_then(Value::as_str)
            .unwrap_or("")
            .replace("-->", "->")
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ");
        if body.is_empty() {
            continue;
        }
        if let Some(sp) = turn.get("speaker").and_then(Value::as_str) {
            let cleaned = sp
                .replace("-->", "->")
                .split_whitespace()
                .collect::<Vec<_>>()
                .join(" ");
            if !cleaned.is_empty() {
                body = format!("{cleaned}: {body}");
            }
        }
        if is_vtt {
            body = body
                .replace('&', "&amp;")
                .replace('<', "&lt;")
                .replace('>', "&gt;");
        }
        let prefix = if is_srt {
            format!("{}\n", cues.len() + 1)
        } else {
            String::new()
        };
        cues.push(format!("{prefix}{first} --> {last}\n{body}"));
    }
    let header = if is_vtt { "WEBVTT\n\n" } else { "" };
    let body = if cues.is_empty() {
        String::new()
    } else {
        cues.join("\n\n") + "\n"
    };
    format!("{header}{body}")
}

pub fn render_meeting_search(report: &Value, meetings_only: bool) -> String {
    let offset = |turn: &Value| -> String {
        match turn.get("start").and_then(Value::as_f64) {
            Some(s) => timestamp(s, false, false),
            None => "—".to_string(),
        }
    };
    let rows = report
        .get("passages")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let total = report.get("totalMatches").and_then(Value::as_u64).unwrap_or(0) as usize;
    let matching = report
        .get("matchingMeetings")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let searched = report
        .get("searchedMeetings")
        .and_then(Value::as_u64)
        .unwrap_or(0) as usize;
    let passage_label = if total == 1 { "passage" } else { "passages" };
    let noun = if meetings_only { "meeting" } else { "session" };
    let meeting_label = if searched == 1 {
        noun.to_string()
    } else {
        format!("{noun}s")
    };
    let mut lines = vec![format!(
        "{total} matching {passage_label} across {} of {searched} {meeting_label}.",
        matching.len()
    )];
    if rows.len() < total {
        lines.push(format!("Showing the top {}.", rows.len()));
    }
    for m in matching {
        let id = m.get("id").and_then(Value::as_str).unwrap_or("");
        let title = m.get("title").and_then(Value::as_str).unwrap_or("");
        let count = m.get("matchCount").and_then(Value::as_u64).unwrap_or(0);
        lines.push(format!("{id}  {title}  {count} matches"));
    }
    for hit in rows {
        let when = match hit.get("absoluteTime").and_then(Value::as_str) {
            Some(abs) => format!("{} @ {abs}", offset(hit)),
            None => offset(hit),
        };
        let source = hit
            .get("transcriptSource")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .or_else(|| hit.get("source").and_then(Value::as_str))
            .unwrap_or("");
        let speaker = match hit.get("speaker").and_then(Value::as_str) {
            Some(s) if !s.is_empty() => format!("  {s}"),
            _ => String::new(),
        };
        let hid = hit.get("id").and_then(Value::as_str).unwrap_or("");
        let htitle = hit.get("title").and_then(Value::as_str).unwrap_or("");
        lines.push(String::new());
        lines.push(format!("{hid}  {when}  {htitle}  {source}{speaker}"));
        if let Some(before) = hit.get("before").filter(|v| v.is_object()) {
            let sp = match before.get("speaker").and_then(Value::as_str) {
                Some(s) if !s.is_empty() => format!(" {s}"),
                _ => String::new(),
            };
            let txt = before.get("text").and_then(Value::as_str).unwrap_or("");
            lines.push(format!("  Before ({}{sp}): {txt}", offset(before)));
        }
        let snip = hit.get("snippet").and_then(Value::as_str).unwrap_or("");
        lines.push(format!("  Hit: {snip}"));
        if let Some(after) = hit.get("after").filter(|v| v.is_object()) {
            let sp = match after.get("speaker").and_then(Value::as_str) {
                Some(s) if !s.is_empty() => format!(" {s}"),
                _ => String::new(),
            };
            let txt = after.get("text").and_then(Value::as_str).unwrap_or("");
            lines.push(format!("  After ({}{sp}): {txt}", offset(after)));
        }
    }
    lines.join("\n")
}
