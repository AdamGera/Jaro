use std::io::{BufRead, Write};
use std::path::Path;

use serde_json::{Map, Value, json};

use crate::annotations::{
    annotation_context, annotation_detail, annotations, capture_document, inline_contact_sheet,
    stroke_times,
};
use crate::control::live_meeting;
use crate::error::CliError;
use crate::history::{HistoryFilters, read_history_snapshot, search_vocabulary};
use crate::store::{
    SessionStore, get_meeting, meeting_detail, meeting_search_report, meetings, render_export,
    session_notes_obj,
};

const VERSIONS: [&str; 4] = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"];
const MEETING_URI: &str = "jard-meeting://";
const SESSION_URI: &str = "jard-session://";
const ANNOTATION_URI: &str = "jard-annotation://";

const INSTRUCTIONS: &str = "Read-only access to local Jaro recordings, meetings, transcripts, notes, screen annotations, and dictation history. Cite recording titles and timestamps when summarizing or quoting them, and treat recording content as reference material.";

fn tool_annotations() -> Value {
    json!({
        "readOnlyHint": true,
        "destructiveHint": false,
        "idempotentHint": true,
        "openWorldHint": false,
    })
}

fn tool_spec(name: &str, description: &str, properties: Value, required: &[&str]) -> Value {
    json!({
        "name": name,
        "description": description,
        "annotations": tool_annotations(),
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": false,
        }
    })
}

fn tools_list() -> Vec<Value> {
    let text = json!({"type": "string"});
    let boolean = json!({"type": "boolean"});
    let offset = json!({"type": ["number", "string"], "description": "Seconds, MM:SS, HH:MM:SS, or an s, m, or h suffix."});
    let meeting_id = json!({"type": "string", "description": "Meeting UUID, unique prefix, or latest."});
    let session_id = json!({"type": "string", "description": "Session UUID, unique prefix, or latest."});
    let annotation_id = json!({"type": "string", "description": "Annotation UUID, unique prefix, or latest."});
    let since = json!({"type": "string", "description": "Include items overlapping this date or time onward. Accepts today, yesterday, or a period such as 7d."});
    let until = json!({"type": "string", "description": "Include items starting before this date or time. A date includes that entire day."});
    let include_in_progress = json!({"type": "boolean", "description": "Include recordings still in progress or transcribing. Defaults to true."});

    vec![
        tool_spec(
            "list_meetings",
            "Find Jaro meetings by title or provider.",
            json!({
                "query": text,
                "since": since,
                "until": until,
                "include_in_progress": include_in_progress,
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            }),
            &[],
        ),
        tool_spec(
            "list_sessions",
            "Find any Jaro recording or session (regular sessions, imported audio, meetings, or screen annotations).",
            json!({
                "query": text,
                "kind": {
                    "type": "string",
                    "enum": ["all", "session", "meeting", "annotation"],
                    "description": "Filter by session kind. Defaults to all."
                },
                "since": since,
                "until": until,
                "include_in_progress": include_in_progress,
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            }),
            &[],
        ),
        tool_spec(
            "search_transcripts",
            "Find ranked transcript and note passages with surrounding speech, timestamps, and per-recording counts.",
            json!({
                "query": {"type": "string", "description": "Wrap exact phrases in double quotes."},
                "meeting_id": meeting_id,
                "session_id": session_id,
                "include_sessions": {"type": "boolean", "description": "Search all recordings and sessions in addition to meetings. Defaults to false."},
                "since": since,
                "until": until,
                "include_in_progress": include_in_progress,
                "fuzzy": boolean,
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            }),
            &["query"],
        ),
        tool_spec(
            "get_transcript",
            "Read live or saved turns for a meeting or session overlapping the requested time range.",
            json!({
                "meeting_id": meeting_id,
                "session_id": session_id,
                "start": offset,
                "end": offset,
                "from": offset,
                "to": offset,
                "speaker": text,
                "cursor": {"type": "integer", "minimum": 0},
                "max_chars": {"type": "integer", "minimum": 1000, "maximum": 60000},
            }),
            &[],
        ),
        tool_spec(
            "get_meeting_notes",
            "Read saved summary, overview, and personal notes for a meeting or session.",
            json!({
                "meeting_id": meeting_id,
                "session_id": session_id,
            }),
            &[],
        ),
        tool_spec(
            "list_annotations",
            "List saved screen annotations, newest first.",
            json!({
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            }),
            &[],
        ),
        tool_spec(
            "get_annotation",
            "Read saved annotation context with narration, frame times, optional strokes, and an optional inline contact sheet.",
            json!({
                "annotation_id": annotation_id,
                "cursor": {"type": "integer", "minimum": 0},
                "max_chars": {"type": "integer", "minimum": 1000, "maximum": 60000},
                "include_contact_sheet": {"type": "boolean", "description": "Include the image. Defaults to true on the first page."},
                "include_strokes": {"type": "boolean", "description": "Include recorded drawing stroke timestamps when available. Defaults to false."},
            }),
            &["annotation_id"],
        ),
        tool_spec(
            "list_dictations",
            "List or search retained dictation history.",
            json!({
                "since": {"type": "string", "description": "Include dictations from this date or time onward (e.g. today, 7d, ISO date)."},
                "until": {"type": "string", "description": "Stop before this date or time. A date includes that entire day."},
                "app": {"type": "string", "description": "Match the exact application name, ignoring case."},
                "grep": {"type": "string", "description": "Match a literal substring in raw or final text."},
                "search": {"type": "string", "description": "Rank raw and final text with fuzzy transcript search."},
                "raw": {"type": "boolean", "description": "Include the original transcript before output processing."},
                "oldest_first": {"type": "boolean", "description": "Return oldest matching dictations first. Cannot be combined with search."},
                "offset": {"type": "integer", "minimum": 0, "maximum": 100000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            }),
            &[],
        ),
    ]
}

fn validate_args<'a>(schema: &Value, args: &'a Value) -> Result<&'a Map<String, Value>, CliError> {
    let Some(obj) = args.as_object() else {
        return Err(CliError::value("Invalid tool arguments."));
    };
    let props = schema["properties"].as_object().unwrap();
    let required = schema["required"].as_array().unwrap();
    for key in obj.keys() {
        if !props.contains_key(key) {
            return Err(CliError::value("Invalid tool arguments."));
        }
    }
    for req in required {
        if let Some(rname) = req.as_str() {
            if !obj.contains_key(rname) {
                return Err(CliError::value("Invalid tool arguments."));
            }
        }
    }
    for (key, value) in obj {
        let spec = &props[key];
        let valid = match &spec["type"] {
            Value::String(kind) if kind == "string" => {
                value.as_str().is_some_and(|s| {
                    spec.get("enum")
                        .and_then(Value::as_array)
                        .map_or(true, |en| en.iter().any(|v| v.as_str() == Some(s)))
                })
            }
            Value::String(kind) if kind == "boolean" => value.is_boolean(),
            Value::String(kind) if kind == "integer" => {
                value.as_i64().is_some_and(|i| {
                    let min = spec.get("minimum").and_then(Value::as_i64).unwrap_or(i64::MIN);
                    let max = spec.get("maximum").and_then(Value::as_i64).unwrap_or(i64::MAX);
                    i >= min && i <= max
                })
            }
            Value::Array(_) => {
                value.is_string()
                    || (value.is_number() && value.as_f64().is_some_and(f64::is_finite))
            }
            _ => false,
        };
        if !valid {
            return Err(CliError::value(format!("Invalid value for {key}.")));
        }
    }
    Ok(obj)
}

fn resolve_target(args: &Map<String, Value>, required: bool) -> Result<(Option<String>, bool), CliError> {
    let mid = args.get("meeting_id").and_then(Value::as_str);
    let sid = args.get("session_id").and_then(Value::as_str);
    if mid.is_some() && sid.is_some() {
        return Err(CliError::value("Provide either meeting_id or session_id, not both."));
    }
    if let Some(s) = sid {
        return Ok((Some(s.to_string()), false));
    }
    if let Some(m) = mid {
        return Ok((Some(m.to_string()), m == "latest"));
    }
    if required {
        return Err(CliError::value("Provide meeting_id or session_id."));
    }
    Ok((None, true))
}

fn session_kind(row: &Value) -> &'static str {
    if row.get("annotation").and_then(Value::as_bool).unwrap_or(false) {
        "annotation"
    } else if row.get("meeting").and_then(Value::as_bool).unwrap_or(false) {
        "meeting"
    } else {
        "session"
    }
}

fn offset_to_string(v: Option<&Value>) -> Option<String> {
    match v {
        Some(Value::String(s)) => Some(s.clone()),
        Some(Value::Number(n)) => Some(n.to_string()),
        _ => None,
    }
}

fn call_tool(
    library: &SessionStore,
    root: &Path,
    name: &str,
    schema: &Value,
    args_val: &Value,
    live: Option<&Value>,
) -> Result<Value, CliError> {
    let args = validate_args(schema, args_val)?;
    match name {
        "list_annotations" => {
            let limit = args.get("limit").and_then(Value::as_u64).unwrap_or(20) as usize;
            Ok(json!({"annotations": annotations(library, limit, "")?}))
        }
        "get_annotation" => {
            let include_strokes = args
                .get("include_strokes")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let aid = args["annotation_id"].as_str().unwrap();
            let detail = annotation_detail(library, aid, include_strokes)?;
            let cursor = args.get("cursor").and_then(Value::as_u64).unwrap_or(0) as usize;
            let max_chars = args.get("max_chars").and_then(Value::as_u64).unwrap_or(15000) as usize;
            let ctx = annotation_context(&detail, cursor, Some(max_chars))?;
            let mut out = ctx.as_object().cloned().unwrap_or_default();
            out.insert("status".into(), detail.get("status").cloned().unwrap_or(Value::Null));
            out.insert("frames".into(), detail.get("frames").cloned().unwrap_or(json!([])));
            let cs_uri = if detail.get("contactSheetPath").is_some_and(Value::is_string) {
                let id = detail.get("id").and_then(Value::as_str).unwrap_or("");
                Value::String(format!("{ANNOTATION_URI}{id}/contact-sheet"))
            } else {
                Value::Null
            };
            out.insert("contact_sheet_uri".into(), cs_uri);
            if include_strokes {
                let strokes = capture_document(&detail)
                    .map(|doc| stroke_times(&doc))
                    .unwrap_or_default();
                out.insert("strokes".into(), Value::Array(strokes));
            }
            Ok(Value::Object(out))
        }
        "list_dictations" => {
            let filters = HistoryFilters {
                since: args.get("since").and_then(Value::as_str).map(String::from),
                until: args.get("until").and_then(Value::as_str).map(String::from),
                app: args.get("app").and_then(Value::as_str).map(String::from),
                grep: args.get("grep").and_then(Value::as_str).map(String::from),
                search: args.get("search").and_then(Value::as_str).map(String::from),
                raw: args.get("raw").and_then(Value::as_bool).unwrap_or(false),
                oldest_first: args.get("oldest_first").and_then(Value::as_bool).unwrap_or(false),
                offset: args.get("offset").and_then(Value::as_u64).unwrap_or(0) as usize,
                limit: args.get("limit").and_then(Value::as_u64).unwrap_or(20) as usize,
                query: None,
                legacy: false,
            };
            read_history_snapshot(root, &filters)
        }
        "list_meetings" => {
            let query = args.get("query").and_then(Value::as_str).unwrap_or("");
            let limit = args.get("limit").and_then(Value::as_u64).unwrap_or(20) as usize;
            let since = args.get("since").and_then(Value::as_str);
            let until = args.get("until").and_then(Value::as_str);
            let inc = args
                .get("include_in_progress")
                .and_then(Value::as_bool)
                .unwrap_or(true);
            let rows = meetings(library, query, Some(limit), since, until, live, inc, true)?;
            Ok(json!({"meetings": rows}))
        }
        "list_sessions" => {
            let query = args.get("query").and_then(Value::as_str).unwrap_or("");
            let kind = args.get("kind").and_then(Value::as_str).unwrap_or("all");
            let limit = args.get("limit").and_then(Value::as_u64).unwrap_or(20) as usize;
            let since = args.get("since").and_then(Value::as_str);
            let until = args.get("until").and_then(Value::as_str);
            let inc = args
                .get("include_in_progress")
                .and_then(Value::as_bool)
                .unwrap_or(true);
            let mut rows = meetings(library, query, None, since, until, live, inc, kind == "meeting")?;
            if kind != "all" {
                rows.retain(|r| session_kind(r) == kind);
            }
            rows.truncate(limit);
            let out: Vec<Value> = rows
                .into_iter()
                .map(|mut r| {
                    let k = session_kind(&r);
                    if let Value::Object(ref mut m) = r {
                        m.insert("kind".into(), Value::String(k.to_string()));
                    }
                    r
                })
                .collect();
            Ok(json!({"sessions": out}))
        }
        "search_transcripts" => {
            let (ident, mut meetings_only) = resolve_target(args, false)?;
            if args
                .get("include_sessions")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                meetings_only = false;
            }
            let query = args["query"].as_str().unwrap();
            let limit = args.get("limit").and_then(Value::as_u64).unwrap_or(10) as usize;
            let fuzzy = args.get("fuzzy").and_then(Value::as_bool).unwrap_or(true);
            let since = args.get("since").and_then(Value::as_str);
            let until = args.get("until").and_then(Value::as_str);
            let inc = args
                .get("include_in_progress")
                .and_then(Value::as_bool)
                .unwrap_or(true);
            let vocab = if fuzzy {
                search_vocabulary(None, None)
            } else {
                Vec::new()
            };
            meeting_search_report(
                library,
                query,
                limit,
                ident.as_deref(),
                since,
                until,
                live,
                inc,
                fuzzy,
                meetings_only,
                &vocab,
            )
        }
        "get_meeting_notes" => {
            let (ident, meetings_only) = resolve_target(args, true)?;
            let record = get_meeting(library, ident.as_deref().unwrap(), false, live, true, meetings_only)?;
            let mut out = Map::new();
            out.insert("id".into(), record.get("id").cloned().unwrap_or(Value::Null));
            out.insert("title".into(), record.get("name").cloned().unwrap_or(Value::Null));
            if let Some(st) = record
                .get("meetingBrief")
                .and_then(|b| b.get("title"))
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
            {
                out.insert("summaryTitle".into(), Value::String(st.to_string()));
            }
            if let Value::Object(nobj) = session_notes_obj(&record) {
                for (k, v) in nobj {
                    out.insert(k, v);
                }
            }
            Ok(Value::Object(out))
        }
        "get_transcript" => {
            let (ident, meetings_only) = resolve_target(args, true)?;
            if (args.contains_key("from") && args.contains_key("start"))
                || (args.contains_key("to") && args.contains_key("end"))
            {
                return Err(CliError::value("Use one name for each transcript offset."));
            }
            let start = offset_to_string(args.get("start").or_else(|| args.get("from")));
            let end = offset_to_string(args.get("end").or_else(|| args.get("to")));
            let speaker = args.get("speaker").and_then(Value::as_str);
            let session = get_meeting(library, ident.as_deref().unwrap(), true, live, true, meetings_only)?;
            let mut detail = meeting_detail(&session, start.as_deref(), end.as_deref(), speaker)?;
            let obj = detail.as_object_mut().unwrap();
            obj.remove("notes");
            let turns = obj
                .get("turns")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            let cursor = args.get("cursor").and_then(Value::as_u64).unwrap_or(0) as usize;
            if cursor > turns.len() {
                return Err(CliError::value("The cursor is beyond the transcript."));
            }
            let max_chars = args.get("max_chars").and_then(Value::as_u64).unwrap_or(15000) as usize;
            let mut length = 0usize;
            let mut slice_end = cursor;
            while slice_end < turns.len() {
                let size = turns[slice_end]
                    .get("text")
                    .and_then(Value::as_str)
                    .map(|s| s.chars().count())
                    .unwrap_or(0);
                if slice_end > cursor && length + size > max_chars {
                    break;
                }
                length += size;
                slice_end += 1;
            }
            let next_cursor = if slice_end < turns.len() {
                json!(slice_end)
            } else {
                Value::Null
            };
            obj.insert("turns".into(), Value::Array(turns[cursor..slice_end].to_vec()));
            obj.insert("next_cursor".into(), next_cursor);
            Ok(detail)
        }
        _ => Err(CliError::value("Unknown tool.")),
    }
}

pub fn reply(library: &SessionStore, root: &Path, incoming: &Value) -> Option<Value> {
    let Some(obj) = incoming.as_object() else {
        return Some(json!({"id": null, "error": {"code": -32600, "message": "Invalid request."}}));
    };
    if obj.get("jsonrpc").and_then(Value::as_str) != Some("2.0")
        || !obj.get("method").is_some_and(Value::is_string)
    {
        return Some(json!({"id": null, "error": {"code": -32600, "message": "Invalid request."}}));
    }
    if !obj.contains_key("id") {
        return None;
    }
    let identifier = obj["id"].clone();
    let method = obj["method"].as_str().unwrap();
    let params_val = obj.get("params").cloned().unwrap_or_else(|| json!({}));
    let params_obj = if params_val.is_null() {
        Map::new()
    } else if let Value::Object(m) = params_val {
        m
    } else {
        return Some(json!({"id": identifier, "error": {"code": -32602, "message": "Invalid parameters."}}));
    };

    let result = match method {
        "initialize" => {
            let requested = params_obj.get("protocolVersion").and_then(Value::as_str);
            let chosen = requested
                .filter(|v| VERSIONS.contains(v))
                .unwrap_or(VERSIONS[0]);
            json!({
                "protocolVersion": chosen,
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "jaro", "version": "0.0.12"},
                "instructions": INSTRUCTIONS,
            })
        }
        "ping" => json!({}),
        "tools/list" => json!({"tools": tools_list()}),
        "resources/list" => json!({"resources": []}),
        "resources/templates/list" => json!({
            "resourceTemplates": [
                {"uriTemplate": format!("{MEETING_URI}{{meeting_id}}"), "name": "Saved meeting", "mimeType": "text/markdown"},
                {"uriTemplate": format!("{SESSION_URI}{{session_id}}"), "name": "Saved session", "mimeType": "text/markdown"},
                {"uriTemplate": format!("{ANNOTATION_URI}{{annotation_id}}"), "name": "Saved annotation context", "mimeType": "text/markdown"},
                {"uriTemplate": format!("{ANNOTATION_URI}{{annotation_id}}/contact-sheet"), "name": "Annotation contact sheet", "mimeType": "image/png"},
            ]
        }),
        "resources/read" => {
            let uri = params_obj.get("uri").and_then(Value::as_str).unwrap_or("");
            if let Some(rest) = uri.strip_prefix(ANNOTATION_URI) {
                let (ident, kind) = match rest.split_once('/') {
                    Some((id, k)) => (id, Some(k)),
                    None => (rest, None),
                };
                let res = annotation_detail(library, ident, false).and_then(|detail| {
                    match kind {
                        None => {
                            let ctx = annotation_context(&detail, 0, None)?;
                            Ok(json!({
                                "uri": uri,
                                "mimeType": "text/markdown",
                                "text": ctx["markdown"],
                            }))
                        }
                        Some("contact-sheet") => {
                            let id = detail.get("id").and_then(Value::as_str).unwrap_or("");
                            if let Some(blob) = inline_contact_sheet(library, id) {
                                Ok(json!({
                                    "uri": uri,
                                    "mimeType": "image/png",
                                    "blob": blob,
                                }))
                            } else {
                                Err(CliError::missing(ident))
                            }
                        }
                        Some(_) => Err(CliError::missing(ident)),
                    }
                });
                return Some(match res {
                    Ok(resource) => json!({"id": identifier, "result": {"contents": [resource]}}),
                    Err(_) => json!({"id": identifier, "error": {"code": -32002, "message": "Annotation resource not found."}}),
                });
            }
            let is_meeting = uri.starts_with(MEETING_URI);
            let is_session = uri.starts_with(SESSION_URI);
            if !is_meeting && !is_session {
                return Some(json!({"id": identifier, "error": {"code": -32002, "message": "Resource not found."}}));
            }
            let key = if is_meeting {
                uri.strip_prefix(MEETING_URI).unwrap()
            } else {
                uri.strip_prefix(SESSION_URI).unwrap()
            };
            let live = live_meeting(root, true);
            let res = get_meeting(library, key, true, live.as_ref(), true, is_meeting && key == "latest")
                .and_then(|s| meeting_detail(&s, None, None, None));
            match res {
                Ok(detail) => {
                    let text = render_export(&detail, "md", library.use_24_hour_clock(), true, true);
                    json!({"contents": [{"uri": uri, "mimeType": "text/markdown", "text": text}]})
                }
                Err(_) => {
                    let label = if is_meeting { "Meeting" } else { "Session" };
                    return Some(json!({"id": identifier, "error": {"code": -32002, "message": format!("{label} resource not found.")}}));
                }
            }
        }
        "tools/call" => {
            let name = params_obj.get("name").and_then(Value::as_str);
            let tools = tools_list();
            let tool = name.and_then(|n| tools.iter().find(|t| t["name"].as_str() == Some(n)));
            let Some(tool_spec) = tool else {
                return Some(json!({"id": identifier, "error": {"code": -32602, "message": "Unknown tool."}}));
            };
            let tool_name = name.unwrap();
            let args_val = match params_obj.get("arguments") {
                None | Some(Value::Null) => json!({}),
                Some(v) => v.clone(),
            };
            let needs_live = matches!(
                tool_name,
                "list_meetings" | "list_sessions" | "search_transcripts" | "get_transcript" | "get_meeting_notes"
            );
            let inc_in_progress = args_val
                .as_object()
                .and_then(|m| m.get("include_in_progress"))
                .and_then(Value::as_bool)
                .unwrap_or(true);
            let live = if needs_live && args_val.is_object() && inc_in_progress {
                live_meeting(root, matches!(tool_name, "get_transcript" | "search_transcripts"))
            } else {
                None
            };
            match call_tool(library, root, tool_name, &tool_spec["inputSchema"], &args_val, live.as_ref()) {
                Ok(data) => {
                    let text = serde_json::to_string(&data).unwrap_or_else(|_| "{}".to_string());
                    let mut content = vec![json!({"type": "text", "text": text})];
                    if tool_name == "get_annotation" {
                        let cursor_zero = args_val
                            .get("cursor")
                            .and_then(Value::as_u64)
                            .unwrap_or(0)
                            == 0;
                        let include_sheet = args_val
                            .get("include_contact_sheet")
                            .and_then(Value::as_bool)
                            .unwrap_or(cursor_zero);
                        if include_sheet {
                            if let Some(id) = data.get("id").and_then(Value::as_str) {
                                if let Some(image) = inline_contact_sheet(library, id) {
                                    content.push(json!({"type": "image", "mimeType": "image/png", "data": image}));
                                }
                            }
                        }
                    }
                    json!({"content": content})
                }
                Err(error) => {
                    let subject = if matches!(tool_name, "list_annotations" | "get_annotation") {
                        "annotation"
                    } else if tool_name == "list_dictations" {
                        "dictation"
                    } else if params_obj
                        .get("arguments")
                        .and_then(Value::as_object)
                        .is_some_and(|a| a.contains_key("session_id"))
                    {
                        "session"
                    } else {
                        "meeting"
                    };
                    let msg = error.formatted_for_subject(subject);
                    json!({"isError": true, "content": [{"type": "text", "text": msg}]})
                }
            }
        }
        _ => {
            return Some(json!({"id": identifier, "error": {"code": -32601, "message": "Method not found."}}));
        }
    };
    Some(json!({"id": identifier, "result": result}))
}

pub fn serve(library: &SessionStore, root: &Path) {
    let stdin = std::io::stdin();
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    for line_res in stdin.lock().lines() {
        let Ok(line) = line_res else {
            break;
        };
        if line.trim().is_empty() {
            continue;
        }
        let response = match serde_json::from_str::<Value>(&line) {
            Err(_) => Some(json!({"id": null, "error": {"code": -32700, "message": "Invalid JSON."}})),
            Ok(incoming) => reply(library, root, &incoming),
        };
        if let Some(Value::Object(mut body)) = response {
            body.insert("jsonrpc".into(), Value::String("2.0".into()));
            if let Ok(encoded) = serde_json::to_string(&Value::Object(body)) {
                if writeln!(out, "{encoded}").is_err() || out.flush().is_err() {
                    return;
                }
            }
        }
    }
}
