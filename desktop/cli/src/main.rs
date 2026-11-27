mod annotations;
mod control;
mod error;
mod fallback;
mod history;
mod mcp;
mod paths;
mod search;
mod store;
mod time_util;

use std::env;
use std::io::Write;
use std::process;

use serde_json::{Map, Value, json};

use crate::annotations::{
    annotation_context, annotation_detail, annotations, capture_document, is_package_reference,
    stroke_times,
};
use crate::control::{control, desktop, live_meeting, running_engine, tail_log};
use crate::error::CliError;
use crate::fallback::exec_python_fallback_with_mode;
use crate::history::{HistoryFilters, read_history_snapshot, render_history, search_vocabulary};
use crate::paths::{EVENT_LOG, PROCESS_LOG, library_path, read_native_token_plain};
use crate::store::{
    SessionStore, get_meeting, meeting_detail, meeting_search_report, meetings,
    normalize_meeting_title, render_export, render_meeting_search,
};
use crate::time_util::frame_time;

const TOP_HELP: &str = "\
usage: jardctl [-h] [--json]
               {status,logs,start,quit,restart,window,settings,transforms,vocabulary,launch-at-login,check-for-updates,update-channel,model,transcribe,dictation,sessions,meetings,annotate,capture,mcp,history,setup,onboarding}
               ...

Local status, dictation, sessions, meetings, annotations, history, and file
transcription for Jaro.

positional arguments:
  {status,logs,start,quit,restart,window,settings,transforms,vocabulary,launch-at-login,check-for-updates,update-channel,model,transcribe,dictation,sessions,meetings,annotate,capture,mcp,history,setup,onboarding}
    status              Check local engine and desktop status.
    logs                Read local diagnostic logs without opening Jaro.
    start               Open Jaro if it is closed (macOS).
    quit                Close the running desktop app (macOS).
    restart             Reopen the running desktop app at the same path
                        (macOS).
    window              Show or hide the running app's workspace (macOS).
    settings            Read or atomically change running desktop preferences
                        (macOS).
    transforms          Create or check a local transformation configuration.
    vocabulary          Manage the shared dictation vocabulary.
    launch-at-login     Read or change macOS Launch at Login.
    check-for-updates   Ask the desktop updater to check now (macOS).
    update-channel      Read or select a configured desktop update channel
                        (macOS).
    model               Check or install Jaro’s pinned local models.
    transcribe          Transcribe a local audio or video file.
    dictation           Control live system dictation.
    sessions            List, search, and read all saved sessions and
                        recordings.
    meetings            Control meeting recordings and read saved meetings.
    annotate (capture)  Control live annotations and read saved captures.
    mcp                 Serve read-only session, meeting, annotation, and
                        dictation tools over stdio.
    history             Read or clear saved dictations.
    setup               Replay dictation setup.
    onboarding          Replay the dictation tour.

options:
  -h, --help            show this help message and exit
  --json                Emit machine-readable output.
";

fn main() {
    let raw_args: Vec<String> = env::args().skip(1).collect();

    if matches!(
        env::var("JARO_CLI_PYTHON").as_deref(),
        Ok("1" | "true" | "TRUE")
    ) {
        exec_python_fallback_with_mode(&raw_args);
    }

    // Fast path for top-level --help / -h
    if raw_args.len() == 1 && matches!(raw_args[0].as_str(), "-h" | "--help") {
        print!("{TOP_HELP}");
        return;
    }
    if raw_args.is_empty() {
        exec_python_fallback_with_mode(&raw_args);
    }

    // Any subcommand --help / -h delegates to Python argparse for exact subcommand help text.
    if raw_args.iter().any(|a| a == "-h" || a == "--help") {
        exec_python_fallback_with_mode(&raw_args);
    }

    // Extract leading --json flag
    let mut global_json = false;
    let mut idx = 0;
    while idx < raw_args.len() && raw_args[idx] == "--json" {
        global_json = true;
        idx += 1;
    }
    if idx >= raw_args.len() {
        exec_python_fallback_with_mode(&raw_args);
    }

    let command = raw_args[idx].as_str();
    let rest = &raw_args[idx + 1..];

    // On non-macOS, let Python handle platform-specific portable commands except pure SQLite/MCP/logs.
    let is_macos = cfg!(target_os = "macos");

    let handled: Option<(&'static str, bool, Result<(), CliError>)> = match command {
        "mcp" if rest.is_empty() => {
            let root = library_path();
            let store = SessionStore::new(root.clone());
            mcp::serve(&store, &root);
            return;
        }
        "status" if is_macos => Some(("meeting", global_json, run_status(global_json, rest))),
        "logs" => Some(("meeting", global_json, run_logs(rest))),
        "meetings" => match rest.first().map(String::as_str) {
            Some("list" | "show" | "search") => {
                let (json_flag, res) = run_recordings(true, global_json, rest);
                Some(("meeting", json_flag, res))
            }
            Some("status" | "record" | "stop" | "copy-last") if is_macos => {
                let (json_flag, res) = run_meeting_control(global_json, rest);
                Some(("meeting", json_flag, res))
            }
            _ => None,
        },
        "sessions" => match rest.first().map(String::as_str) {
            Some("list" | "show" | "search") => {
                let (json_flag, res) = run_recordings(false, global_json, rest);
                Some(("session", json_flag, res))
            }
            _ => None,
        },
        "annotate" | "capture" => match rest.first().map(String::as_str) {
            Some("list" | "show" | "context" | "inspect") => {
                // Package paths (.jaroannotation / annotation.json) fall back to Python for full validation.
                if rest.iter().skip(1).any(|a| !a.starts_with('-') && is_package_reference(a)) {
                    None
                } else {
                    let (json_flag, res) = run_annotations_read(global_json, rest);
                    Some(("annotation", json_flag, res))
                }
            }
            _ => None,
        },
        "history" if is_macos => {
            if rest.iter().any(|a| a == "clear") {
                None
            } else {
                let (json_flag, res) = run_history_read(global_json, rest);
                Some(("meeting", json_flag, res))
            }
        }
        "dictation" if is_macos => {
            let (json_flag, res) = run_dictation_control(global_json, rest);
            Some(("meeting", json_flag, res))
        }
        _ => None,
    };

    let Some((subject, json_out, result)) = handled else {
        exec_python_fallback_with_mode(&raw_args);
    };

    if let Err(err) = result {
        match err {
            CliError::Usage(msg) => {
                eprintln!("jardctl: error: {msg}");
                process::exit(2);
            }
            CliError::NoSpeech => {
                let msg = "No speech was detected.";
                if json_out {
                    eprintln!("{}", json!({"code": "no-speech", "error": msg}));
                } else {
                    eprintln!("{msg}");
                }
                process::exit(4);
            }
            CliError::Managed(msg) => {
                if json_out {
                    eprintln!("{}", json!({"code": "managed", "error": msg}));
                } else {
                    eprintln!("{msg}");
                }
                process::exit(1);
            }
            other => {
                let msg = other.formatted_for_subject(subject);
                if json_out {
                    eprintln!("{}", json!({"error": msg}));
                } else {
                    eprintln!("{msg}");
                }
                process::exit(1);
            }
        }
    }
}

fn next_opt<'a>(args: &'a [String], i: &mut usize, flag: &str) -> Result<&'a str, CliError> {
    if *i + 1 >= args.len() {
        return Err(CliError::usage(format!("argument {flag}: expected one argument")));
    }
    *i += 1;
    Ok(&args[*i])
}

fn run_status(_global_json: bool, rest: &[String]) -> Result<(), CliError> {
    for arg in rest {
        if arg != "--json" {
            return Err(CliError::usage(format!("unrecognized arguments: {arg}")));
        }
    }
    let root = library_path();
    let engine = running_engine()?;
    let mut value = Map::new();
    let status = engine
        .as_ref()
        .and_then(|e| e.get("status"))
        .and_then(Value::as_str)
        .unwrap_or("stopped");
    let model = engine
        .as_ref()
        .and_then(|e| e.get("model"))
        .and_then(Value::as_str)
        .unwrap_or("Reson8/Rumble-2-Flash");
    value.insert("engine".into(), Value::String(status.to_string()));
    value.insert("model".into(), Value::String(model.to_string()));
    if engine.is_some() {
        if let Ok(desk) = desktop(&root, "status", 12, Map::new()) {
            value.insert("desktop".into(), desk);
        }
    }
    println!("{}", Value::Object(value));
    Ok(())
}

fn run_logs(rest: &[String]) -> Result<(), CliError> {
    let mut process_log = false;
    let mut lines: i64 = 50;
    let mut follow = false;
    let mut i = 0;
    while i < rest.len() {
        let arg = rest[i].as_str();
        match arg {
            "--process" => process_log = true,
            "-f" | "--follow" => follow = true,
            "-n" | "--lines" => {
                let raw = next_opt(rest, &mut i, arg)?;
                lines = raw
                    .parse::<i64>()
                    .map_err(|_| CliError::usage(format!("argument -n/--lines: invalid int value: '{raw}'")))?;
            }
            s if s.starts_with("--lines=") => {
                let raw = &s["--lines=".len()..];
                lines = raw
                    .parse::<i64>()
                    .map_err(|_| CliError::usage(format!("argument -n/--lines: invalid int value: '{raw}'")))?;
            }
            s if s.starts_with("-n") && s.len() > 2 => {
                let raw = &s[2..];
                lines = raw
                    .parse::<i64>()
                    .map_err(|_| CliError::usage(format!("argument -n/--lines: invalid int value: '{raw}'")))?;
            }
            other => return Err(CliError::usage(format!("unrecognized arguments: {other}"))),
        }
        i += 1;
    }
    if !(0..=10_000).contains(&lines) {
        return Err(CliError::value("Choose a log line count from 0 to 10,000."));
    }
    let name = if process_log { PROCESS_LOG } else { EVENT_LOG };
    tail_log(name, lines as usize, follow, process_log)
}

fn run_recordings(meetings_only: bool, global_json: bool, rest: &[String]) -> (bool, Result<(), CliError>) {
    let action = rest[0].as_str();
    let args = &rest[1..];
    let mut json_out = global_json;
    let root = library_path();
    let library = SessionStore::new(root.clone());

    match action {
        "list" => {
            let mut query = String::new();
            let mut limit: i64 = 20;
            let mut since: Option<String> = None;
            let mut until: Option<String> = None;
            let mut include_in_progress = true;
            let mut i = 0;
            while i < args.len() {
                let a = args[i].as_str();
                let res = match a {
                    "--json" => { json_out = true; Ok(()) }
                    "--no-in-progress" => { include_in_progress = false; Ok(()) }
                    "--query" => next_opt(args, &mut i, a).map(|v| query = v.to_string()),
                    s if s.starts_with("--query=") => { query = s["--query=".len()..].to_string(); Ok(()) }
                    "--limit" => next_opt(args, &mut i, a).and_then(|v| {
                        limit = v.parse::<i64>().map_err(|_| CliError::usage(format!("argument --limit: invalid int value: '{v}'")))?;
                        Ok(())
                    }),
                    s if s.starts_with("--limit=") => {
                        let v = &s["--limit=".len()..];
                        v.parse::<i64>()
                            .map(|n| limit = n)
                            .map_err(|_| CliError::usage(format!("argument --limit: invalid int value: '{v}'")))
                    }
                    "--since" => next_opt(args, &mut i, a).map(|v| since = Some(v.to_string())),
                    s if s.starts_with("--since=") => { since = Some(s["--since=".len()..].to_string()); Ok(()) }
                    "--until" => next_opt(args, &mut i, a).map(|v| until = Some(v.to_string())),
                    s if s.starts_with("--until=") => { until = Some(s["--until=".len()..].to_string()); Ok(()) }
                    other => Err(CliError::usage(format!("unrecognized arguments: {other}"))),
                };
                if let Err(e) = res {
                    return (json_out, Err(e));
                }
                i += 1;
            }
            if !(1..=200).contains(&limit) {
                return (json_out, Err(CliError::usage(format!("argument --limit: invalid choice: {limit}"))));
            }
            let live = if include_in_progress { live_meeting(&root, false) } else { None };
            let res = meetings(
                &library,
                &query,
                Some(limit as usize),
                since.as_deref(),
                until.as_deref(),
                live.as_ref(),
                include_in_progress,
                meetings_only,
            )
            .map(|rows| {
                if json_out {
                    println!("{}", Value::Array(rows));
                } else {
                    let text = rows
                        .iter()
                        .map(|r| {
                            let id = r.get("id").and_then(Value::as_str).unwrap_or("");
                            let dt = r
                                .get("meetingStartedAt")
                                .and_then(Value::as_str)
                                .or_else(|| r.get("created").and_then(Value::as_str))
                                .unwrap_or("");
                            let date: String = dt.chars().take(10).collect();
                            let name = r.get("name").and_then(Value::as_str).unwrap_or("");
                            let status = r.get("status").and_then(Value::as_str).unwrap_or("");
                            if status != "complete" {
                                format!("{id}  {date}  {name} ({status})")
                            } else {
                                format!("{id}  {date}  {name}")
                            }
                        })
                        .collect::<Vec<_>>()
                        .join("\n");
                    println!("{}", text.trim_end_matches('\n'));
                }
            });
            (json_out, res)
        }
        "search" => {
            let target_flag = if meetings_only { "--meeting" } else { "--session" };
            let mut query: Option<String> = None;
            let mut target: Option<String> = None;
            let mut fuzzy = true;
            let mut limit: i64 = 10;
            let mut since: Option<String> = None;
            let mut until: Option<String> = None;
            let mut include_in_progress = true;
            let mut i = 0;
            while i < args.len() {
                let a = args[i].as_str();
                let res = match a {
                    "--json" => { json_out = true; Ok(()) }
                    "--no-fuzzy" => { fuzzy = false; Ok(()) }
                    "--no-in-progress" => { include_in_progress = false; Ok(()) }
                    f if f == target_flag => next_opt(args, &mut i, f).map(|v| target = Some(v.to_string())),
                    s if s.starts_with(&format!("{target_flag}=")) => {
                        target = Some(s[target_flag.len() + 1..].to_string());
                        Ok(())
                    }
                    "--limit" => next_opt(args, &mut i, a).and_then(|v| {
                        limit = v.parse::<i64>().map_err(|_| CliError::usage(format!("argument --limit: invalid int value: '{v}'")))?;
                        Ok(())
                    }),
                    s if s.starts_with("--limit=") => {
                        let v = &s["--limit=".len()..];
                        v.parse::<i64>()
                            .map(|n| limit = n)
                            .map_err(|_| CliError::usage(format!("argument --limit: invalid int value: '{v}'")))
                    }
                    "--since" => next_opt(args, &mut i, a).map(|v| since = Some(v.to_string())),
                    s if s.starts_with("--since=") => { since = Some(s["--since=".len()..].to_string()); Ok(()) }
                    "--until" => next_opt(args, &mut i, a).map(|v| until = Some(v.to_string())),
                    s if s.starts_with("--until=") => { until = Some(s["--until=".len()..].to_string()); Ok(()) }
                    "--" => {
                        if i + 1 < args.len() && query.is_none() {
                            i += 1;
                            query = Some(args[i].clone());
                            Ok(())
                        } else {
                            Err(CliError::usage("unrecognized arguments"))
                        }
                    }
                    other if other.starts_with('-') => Err(CliError::usage(format!("unrecognized arguments: {other}"))),
                    pos => {
                        if query.is_none() {
                            query = Some(pos.to_string());
                            Ok(())
                        } else {
                            Err(CliError::usage(format!("unrecognized arguments: {pos}")))
                        }
                    }
                };
                if let Err(e) = res {
                    return (json_out, Err(e));
                }
                i += 1;
            }
            let Some(q) = query else {
                return (json_out, Err(CliError::usage("the following arguments are required: query")));
            };
            if !(1..=50).contains(&limit) {
                return (json_out, Err(CliError::usage(format!("argument --limit: invalid choice: {limit}"))));
            }
            let live = if include_in_progress { live_meeting(&root, true) } else { None };
            let vocab = if fuzzy { search_vocabulary(None, None) } else { Vec::new() };
            let res = meeting_search_report(
                &library,
                &q,
                limit as usize,
                target.as_deref(),
                since.as_deref(),
                until.as_deref(),
                live.as_ref(),
                include_in_progress,
                fuzzy,
                meetings_only,
                &vocab,
            )
            .map(|report| {
                if json_out {
                    println!("{}", report["passages"]);
                } else {
                    let rendered = render_meeting_search(&report, meetings_only);
                    println!("{}", rendered.trim_end_matches('\n'));
                }
            });
            (json_out, res)
        }
        "show" => {
            let mut id: Option<String> = None;
            let mut start: Option<String> = None;
            let mut end: Option<String> = None;
            let mut format: Option<&'static str> = None;
            let mut sub_json = false;
            let mut i = 0;
            while i < args.len() {
                let a = args[i].as_str();
                let res = match a {
                    "--json" => {
                        if format.is_some() {
                            Err(CliError::usage("argument --json: not allowed with format option"))
                        } else {
                            sub_json = true;
                            json_out = true;
                            Ok(())
                        }
                    }
                    "--md" | "--notes" | "--srt" | "--vtt" => {
                        let kind = match a {
                            "--md" => "md",
                            "--notes" => "notes",
                            "--srt" => "srt",
                            "--vtt" => "vtt",
                            _ => unreachable!(),
                        };
                        if format.is_some() || sub_json {
                            Err(CliError::usage(format!("argument {a}: not allowed with previous format argument")))
                        } else {
                            format = Some(kind);
                            Ok(())
                        }
                    }
                    "--from" => next_opt(args, &mut i, a).map(|v| start = Some(v.to_string())),
                    s if s.starts_with("--from=") => { start = Some(s["--from=".len()..].to_string()); Ok(()) }
                    "--to" => next_opt(args, &mut i, a).map(|v| end = Some(v.to_string())),
                    s if s.starts_with("--to=") => { end = Some(s["--to=".len()..].to_string()); Ok(()) }
                    other if other.starts_with('-') => Err(CliError::usage(format!("unrecognized arguments: {other}"))),
                    pos => {
                        if id.is_none() {
                            id = Some(pos.to_string());
                            Ok(())
                        } else {
                            Err(CliError::usage(format!("unrecognized arguments: {pos}")))
                        }
                    }
                };
                if let Err(e) = res {
                    return (json_out, Err(e));
                }
                i += 1;
            }
            let Some(ident) = id else {
                return (json_out, Err(CliError::usage("the following arguments are required: id")));
            };
            if json_out && format.is_some() {
                return (json_out, Err(CliError::value("Choose a single meeting output format.")));
            }
            let live = live_meeting(&root, true);
            let res = get_meeting(&library, &ident, true, live.as_ref(), true, meetings_only)
                .and_then(|s| meeting_detail(&s, start.as_deref(), end.as_deref(), None))
                .map(|detail| {
                    if json_out {
                        println!("{detail}");
                    } else {
                        let kind = format.unwrap_or("md");
                        if kind == "srt" || kind == "vtt" {
                            let rendered = render_export(&detail, kind, false, false, false);
                            let _ = std::io::stdout().write_all(rendered.as_bytes());
                        } else {
                            let rendered = render_export(
                                &detail,
                                kind,
                                library.use_24_hour_clock(),
                                true,
                                kind == "md",
                            );
                            println!("{}", rendered.trim_end_matches('\n'));
                        }
                    }
                });
            (json_out, res)
        }
        _ => unreachable!(),
    }
}

fn run_annotations_read(global_json: bool, rest: &[String]) -> (bool, Result<(), CliError>) {
    let action = rest[0].as_str();
    let args = &rest[1..];
    let mut json_out = global_json;
    let root = library_path();
    let library = SessionStore::new(root);

    match action {
        "list" => {
            let mut limit: i64 = 20;
            let mut i = 0;
            while i < args.len() {
                let a = args[i].as_str();
                let res = match a {
                    "--json" => { json_out = true; Ok(()) }
                    "--limit" => next_opt(args, &mut i, a).and_then(|v| {
                        limit = v.parse::<i64>().map_err(|_| CliError::usage(format!("argument --limit: invalid int value: '{v}'")))?;
                        Ok(())
                    }),
                    s if s.starts_with("--limit=") => {
                        let v = &s["--limit=".len()..];
                        v.parse::<i64>()
                            .map(|n| limit = n)
                            .map_err(|_| CliError::usage(format!("argument --limit: invalid int value: '{v}'")))
                    }
                    other => Err(CliError::usage(format!("unrecognized arguments: {other}"))),
                };
                if let Err(e) = res {
                    return (json_out, Err(e));
                }
                i += 1;
            }
            if !(1..=200).contains(&limit) {
                return (json_out, Err(CliError::usage(format!("argument --limit: invalid choice: {limit}"))));
            }
            let res = annotations(&library, limit as usize, "").map(|rows| {
                println!("{}", json!({"version": 1, "annotations": rows}));
            });
            (json_out, res)
        }
        "show" | "context" | "inspect" => {
            let mut id = "latest".to_string();
            let mut seen_id = false;
            let mut strokes = false;
            let mut i = 0;
            while i < args.len() {
                let a = args[i].as_str();
                let res = match a {
                    "--json" => { json_out = true; Ok(()) }
                    "--strokes" if action == "inspect" => { strokes = true; Ok(()) }
                    other if other.starts_with('-') => Err(CliError::usage(format!("unrecognized arguments: {other}"))),
                    pos => {
                        if !seen_id {
                            id = pos.to_string();
                            seen_id = true;
                            Ok(())
                        } else {
                            Err(CliError::usage(format!("unrecognized arguments: {pos}")))
                        }
                    }
                };
                if let Err(e) = res {
                    return (json_out, Err(e));
                }
                i += 1;
            }
            let inspect_capture = action == "inspect" && (json_out || strokes);
            let res = annotation_detail(&library, &id, inspect_capture).and_then(|detail| {
                if inspect_capture {
                    let doc = capture_document(&detail)?;
                    if strokes {
                        let st = stroke_times(&doc);
                        if json_out {
                            println!("{}", json!({"id": detail["id"], "strokes": st}));
                        } else {
                            let lines: Vec<String> = st
                                .iter()
                                .map(|s| {
                                    let num = s["stroke"].as_i64().unwrap_or(0);
                                    let start = s["start"].as_f64().unwrap_or(0.0);
                                    let end = s["end"].as_f64().unwrap_or(0.0);
                                    format!("Stroke {num}  {}–{}", frame_time(start), frame_time(end))
                                })
                                .collect();
                            if lines.is_empty() {
                                println!("No strokes recorded.");
                            } else {
                                println!("{}", lines.join("\n"));
                            }
                        }
                    } else {
                        println!("{doc}");
                    }
                    return Ok(());
                }
                if action == "context" || action == "inspect" {
                    let mut ctx = annotation_context(&detail, 0, None)?;
                    if let Value::Object(ref mut m) = ctx {
                        m.insert(
                            "contactSheetPath".into(),
                            detail.get("contactSheetPath").cloned().unwrap_or(Value::Null),
                        );
                    }
                    if json_out {
                        println!("{ctx}");
                    } else {
                        print!("{}", ctx["markdown"].as_str().unwrap_or(""));
                    }
                    return Ok(());
                }
                println!("{detail}");
                Ok(())
            });
            (json_out, res)
        }
        _ => unreachable!(),
    }
}

fn run_history_read(global_json: bool, args: &[String]) -> (bool, Result<(), CliError>) {
    let mut json_out = global_json;
    let mut sub_json = false;
    let mut text_out = false;
    let mut query: Option<String> = None;
    let mut offset: i64 = 0;
    let mut since: Option<String> = None;
    let mut until: Option<String> = None;
    let mut app: Option<String> = None;
    let mut grep: Option<String> = None;
    let mut search: Option<String> = None;
    let mut oldest_first = false;
    let mut limit: Option<i64> = None;
    let mut raw = false;

    let mut i = 0;
    while i < args.len() {
        let a = args[i].as_str();
        let res = match a {
            "--json" => {
                if text_out {
                    Err(CliError::usage("argument --json: not allowed with argument --text"))
                } else {
                    sub_json = true;
                    json_out = true;
                    Ok(())
                }
            }
            "--text" => {
                if sub_json {
                    Err(CliError::usage("argument --text: not allowed with argument --json"))
                } else {
                    text_out = true;
                    Ok(())
                }
            }
            "--raw" => { raw = true; Ok(()) }
            "--oldest-first" => { oldest_first = true; Ok(()) }
            "--query" => next_opt(args, &mut i, a).map(|v| query = Some(v.to_string())),
            s if s.starts_with("--query=") => { query = Some(s["--query=".len()..].to_string()); Ok(()) }
            "--since" => next_opt(args, &mut i, a).map(|v| since = Some(v.to_string())),
            s if s.starts_with("--since=") => { since = Some(s["--since=".len()..].to_string()); Ok(()) }
            "--until" => next_opt(args, &mut i, a).map(|v| until = Some(v.to_string())),
            s if s.starts_with("--until=") => { until = Some(s["--until=".len()..].to_string()); Ok(()) }
            "--app" => next_opt(args, &mut i, a).map(|v| app = Some(v.to_string())),
            s if s.starts_with("--app=") => { app = Some(s["--app=".len()..].to_string()); Ok(()) }
            "--grep" => next_opt(args, &mut i, a).map(|v| grep = Some(v.to_string())),
            s if s.starts_with("--grep=") => { grep = Some(s["--grep=".len()..].to_string()); Ok(()) }
            "--search" => next_opt(args, &mut i, a).map(|v| search = Some(v.to_string())),
            s if s.starts_with("--search=") => { search = Some(s["--search=".len()..].to_string()); Ok(()) }
            "--offset" => next_opt(args, &mut i, a).and_then(|v| {
                offset = v.parse::<i64>().map_err(|_| CliError::usage(format!("argument --offset: invalid int value: '{v}'")))?;
                Ok(())
            }),
            s if s.starts_with("--offset=") => {
                let v = &s["--offset=".len()..];
                v.parse::<i64>()
                    .map(|n| offset = n)
                    .map_err(|_| CliError::usage(format!("argument --offset: invalid int value: '{v}'")))
            }
            "--limit" => next_opt(args, &mut i, a).and_then(|v| {
                limit = Some(v.parse::<i64>().map_err(|_| CliError::usage(format!("argument --limit: invalid int value: '{v}'")))?);
                Ok(())
            }),
            s if s.starts_with("--limit=") => {
                let v = &s["--limit=".len()..];
                v.parse::<i64>()
                    .map(|n| limit = Some(n))
                    .map_err(|_| CliError::usage(format!("argument --limit: invalid int value: '{v}'")))
            }
            other => Err(CliError::usage(format!("unrecognized arguments: {other}"))),
        };
        if let Err(e) = res {
            return (json_out, Err(e));
        }
        i += 1;
    }

    if text_out && json_out {
        return (json_out, Err(CliError::value("Choose either JSON or text output.")));
    }
    let legacy = !(since.is_some()
        || until.is_some()
        || app.is_some()
        || grep.is_some()
        || search.is_some()
        || limit.is_some()
        || raw
        || oldest_first
        || text_out);
    let page_size = if legacy { 15 } else { 50 };
    let effective_limit = limit.unwrap_or(page_size);
    if effective_limit <= 0 {
        return (json_out, Err(CliError::value("Use a history limit from 1 to 10,000.")));
    }
    let filters = HistoryFilters {
        since,
        until,
        app,
        grep,
        search,
        oldest_first,
        query,
        offset: offset.max(0) as usize,
        limit: effective_limit as usize,
        raw,
        legacy,
    };
    let root = library_path();
    let res = read_history_snapshot(&root, &filters).map(|result| {
        if json_out || legacy {
            println!("{result}");
        } else {
            println!("{}", render_history(&result));
        }
    });
    (json_out, res)
}

fn run_meeting_control(global_json: bool, rest: &[String]) -> (bool, Result<(), CliError>) {
    let action = rest[0].as_str();
    let args = &rest[1..];
    let mut json_out = global_json;
    let mut title: Option<String> = None;
    let mut i = 0;
    while i < args.len() {
        let a = args[i].as_str();
        let res = match a {
            "--json" => { json_out = true; Ok(()) }
            "--title" if action == "record" => next_opt(args, &mut i, a).map(|v| title = Some(v.to_string())),
            s if action == "record" && s.starts_with("--title=") => {
                title = Some(s["--title=".len()..].to_string());
                Ok(())
            }
            other => Err(CliError::usage(format!("unrecognized arguments: {other}"))),
        };
        if let Err(e) = res {
            return (json_out, Err(e));
        }
        i += 1;
    }

    let res = (|| -> Result<(), CliError> {
        let root = library_path();
        if running_engine()?.is_none() {
            return Err(CliError::runtime("Open Jaro to start the local engine."));
        }
        let token = read_native_token_plain(&root)?;
        let mut opts = Map::new();
        if action == "record" {
            if let Some(ref t) = title {
                if !t.trim().is_empty() {
                    opts.insert("title".into(), Value::String(normalize_meeting_title(t)?));
                }
            }
        }
        let val = control(&root, &token, &format!("meeting-{action}"), 12, opts)?;
        println!("{val}");
        Ok(())
    })();
    (json_out, res)
}

fn run_dictation_control(global_json: bool, args: &[String]) -> (bool, Result<(), CliError>) {
    let mut json_out = global_json;
    let mut action: Option<String> = None;
    let mut raw = false;
    let mut print_transcript = false;
    for a in args {
        match a.as_str() {
            "--json" => json_out = true,
            "--raw" => raw = true,
            "--print" => print_transcript = true,
            other if other.starts_with('-') => {
                return (json_out, Err(CliError::usage(format!("unrecognized arguments: {other}"))));
            }
            pos => {
                if action.is_none() {
                    action = Some(pos.to_string());
                } else {
                    return (json_out, Err(CliError::usage(format!("unrecognized arguments: {pos}"))));
                }
            }
        }
    }
    let Some(act) = action else {
        return (json_out, Err(CliError::usage("the following arguments are required: action")));
    };
    if !matches!(act.as_str(), "start" | "stop" | "toggle" | "cancel" | "paste-last") {
        return (json_out, Err(CliError::usage(format!("argument action: invalid choice: '{act}'"))));
    }
    if raw && !matches!(act.as_str(), "start" | "toggle") {
        return (json_out, Err(CliError::usage("--raw requires start or toggle")));
    }
    if print_transcript && !matches!(act.as_str(), "start" | "stop") {
        return (json_out, Err(CliError::usage("--print requires start or stop")));
    }

    let res = (|| -> Result<(), CliError> {
        let root = library_path();
        if running_engine()?.is_none() {
            return Err(CliError::runtime("Open Jaro to start the local engine."));
        }
        let token = read_native_token_plain(&root)?;
        let (rpc_action, timeout) = if raw || print_transcript {
            let flags = format!(
                "{}{}",
                if raw { "-raw" } else { "" },
                if print_transcript { "-print" } else { "" }
            );
            let t = if print_transcript {
                if act == "start" { 31 * 60 + 10 } else { 70 }
            } else {
                12
            };
            (format!("dictation-{act}{flags}"), t)
        } else {
            (act.clone(), 12)
        };
        let value = control(&root, &token, &rpc_action, timeout, Map::new())?;
        if print_transcript {
            if value.get("status").and_then(Value::as_str) == Some("no-speech") {
                return Err(CliError::NoSpeech);
            }
            if !json_out {
                println!("{}", value.get("text").and_then(Value::as_str).unwrap_or(""));
                return Ok(());
            }
        }
        println!("{value}");
        Ok(())
    })();
    (json_out, res)
}
