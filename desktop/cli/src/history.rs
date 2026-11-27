use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::LazyLock;

use chrono::{DateTime, Utc};
use plist::Value as PlistValue;
use regex::Regex;
use serde_json::{Map, Value, json};
use unicode_general_category::{GeneralCategory, get_general_category};
use unicode_normalization::UnicodeNormalization;

use crate::control::desktop;
use crate::error::CliError;
use crate::paths::{casefold, desktop_control_path, open_private_regular, preferences_path};
use crate::search::{count_tokens, rank_passages};
use crate::time_util::{date_bound, format_local_iso_seconds, py_round_i64};

pub const HISTORY_LIMIT: usize = 10_000;
const HISTORY_TEXT_BUDGET: usize = 16 * 1024 * 1024;
const MAX_VOCABULARY: usize = 32;
const MAX_TIMESTAMP: f64 = 253_402_300_800.0;

static RE_CHECKPOINT: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^history-[0-9a-f-]{36}\.plist$").unwrap());

fn retention_label(days: i64) -> Option<&'static str> {
    match days {
        0 => Some("off"),
        1 => Some("day"),
        7 => Some("week"),
        30 => Some("month"),
        _ => None,
    }
}

fn plist_to_dict(path: &Path) -> Result<plist::Dictionary, CliError> {
    let file_opt = open_private_regular(path).map_err(|err| {
        if err.kind() == std::io::ErrorKind::PermissionDenied {
            CliError::value("Jard history must be saved in a private file owned by the current user.")
        } else {
            CliError::value("Could not open the saved Jard dictation history.")
        }
    })?;
    let Some(file) = file_opt else {
        return Ok(plist::Dictionary::new());
    };
    let val = PlistValue::from_reader(file)
        .map_err(|_| CliError::value("The saved Jard dictation history is invalid."))?;
    match val {
        PlistValue::Dictionary(d) => Ok(d),
        _ => Err(CliError::value("The saved Jard dictation history is invalid.")),
    }
}

fn vocabulary_key(word: &str) -> String {
    let nfc: String = word.nfc().collect();
    casefold(&nfc)
}

fn vocabulary_text(word: &str) -> bool {
    !word.is_empty()
        && !word.chars().any(|c| {
            matches!(
                get_general_category(c),
                GeneralCategory::Control | GeneralCategory::Surrogate
            )
        })
        && word.encode_utf16().count() * 2 <= 160
}

fn preferred_terms(settings: &plist::Dictionary) -> Vec<String> {
    let Some(PlistValue::String(vocab)) = settings.get("vocabulary") else {
        return Vec::new();
    };
    let mut terms = Vec::new();
    let mut seen = HashSet::new();
    for line in vocab.lines() {
        let word = line.trim();
        if vocabulary_text(word) {
            let key = vocabulary_key(word);
            if seen.insert(key) {
                terms.push(word.to_string());
                if terms.len() == MAX_VOCABULARY {
                    break;
                }
            }
        }
    }
    terms
}

fn vocabulary_from_dict(dict: &plist::Dictionary) -> Vec<(String, Vec<String>)> {
    let Some(PlistValue::Dictionary(settings)) = dict.get("JardSystemDictation") else {
        return Vec::new();
    };
    let mut indexed: HashMap<String, Vec<String>> = HashMap::new();
    if let Some(PlistValue::Dictionary(aliases)) = settings.get("vocabularySoundsLike") {
        for (k, v) in aliases {
            if let PlistValue::Array(arr) = v {
                let spellings: Vec<String> = arr
                    .iter()
                    .take(8)
                    .filter_map(|item| item.as_string())
                    .filter(|s| vocabulary_text(s) && s.trim().chars().count() >= 2)
                    .map(|s| s.trim().to_string())
                    .collect();
                indexed.insert(vocabulary_key(k), spellings);
            }
        }
    }
    let mut out = Vec::new();
    for term in preferred_terms(settings) {
        let key = vocabulary_key(&term);
        let cleaned = indexed.remove(&key).unwrap_or_default();
        out.push((term, cleaned));
    }
    out
}

pub fn search_vocabulary(snapshot: Option<&plist::Dictionary>, path: Option<&Path>) -> Vec<(String, Vec<String>)> {
    if let Some(snap) = snapshot {
        return vocabulary_from_dict(snap);
    }
    let resolved = match path {
        Some(p) => p.to_path_buf(),
        None => match preferences_path() {
            Ok(p) => p,
            Err(_) => return Vec::new(),
        },
    };
    match plist_to_dict(&resolved) {
        Ok(d) => vocabulary_from_dict(&d),
        Err(_) => Vec::new(),
    }
}

#[derive(Clone)]
struct DictationEntry {
    id: String,
    text: String,
    time: f64,
    raw: Option<String>,
    app: Option<String>,
    seconds: Option<f64>,
    mode: Option<String>,
    kind: Option<String>,
    score: Option<f64>,
    snippet: Option<String>,
}

fn plist_number(val: &PlistValue) -> Option<f64> {
    match val {
        PlistValue::Integer(i) => {
            let n = i.as_signed().map(|x| x as f64).or_else(|| i.as_unsigned().map(|x| x as f64))?;
            if (0.0..MAX_TIMESTAMP).contains(&n) { Some(n) } else { None }
        }
        PlistValue::Real(r) => {
            if r.is_finite() && (0.0..MAX_TIMESTAMP).contains(r) { Some(*r) } else { None }
        }
        _ => None,
    }
}

fn plist_int_exact(val: &PlistValue) -> Option<i64> {
    match val {
        PlistValue::Integer(i) => i.as_signed(),
        _ => None,
    }
}

fn saved_history(
    snapshot: &plist::Dictionary,
    now: DateTime<Utc>,
) -> Result<(Vec<DictationEntry>, &'static str), CliError> {
    let settings = match snapshot.get("JardSystemDictation") {
        None => None,
        Some(PlistValue::Dictionary(d)) => Some(d),
        Some(_) => return Err(CliError::value("The saved Jard history retention is invalid.")),
    };
    let retention = match settings.and_then(|s| s.get("retention")) {
        None => 7i64,
        Some(v) => plist_int_exact(v)
            .ok_or_else(|| CliError::value("The saved Jard history retention is invalid."))?,
    };
    let previous_retention = match settings.and_then(|s| s.get("previousRetention")) {
        None => 7i64,
        Some(v) => plist_int_exact(v)
            .ok_or_else(|| CliError::value("The saved Jard history retention is invalid."))?,
    };
    let Some(ret_label) = retention_label(retention) else {
        return Err(CliError::value("The saved Jard history retention is invalid."));
    };
    if !matches!(previous_retention, 1 | 7 | 30) {
        return Err(CliError::value("The saved Jard history retention is invalid."));
    }

    let history_slice: &[PlistValue] = match snapshot.get("JardSystemDictationHistory") {
        None => &[],
        Some(PlistValue::Array(arr)) => arr.as_slice(),
        Some(_) => return Err(CliError::value("The saved Jard dictation history is invalid.")),
    };

    let now_ts = now.timestamp() as f64 + (now.timestamp_subsec_nanos() as f64) / 1e9;
    let active_days = if retention != 0 { retention } else { previous_retention };
    let expires_before = now_ts - (active_days as f64) * 86_400.0;

    let mut entries = Vec::new();
    let mut total_bytes = 0usize;
    for item in history_slice {
        if entries.len() == HISTORY_LIMIT {
            break;
        }
        let PlistValue::Dictionary(entry) = item else {
            return Err(CliError::value("A saved Jard dictation entry is invalid."));
        };
        let Some(PlistValue::String(id)) = entry.get("id") else {
            return Err(CliError::value("A saved Jard dictation entry is invalid."));
        };
        if id.is_empty() {
            return Err(CliError::value("A saved Jard dictation entry is invalid."));
        }
        let Some(PlistValue::String(text)) = entry.get("text") else {
            return Err(CliError::value("A saved Jard dictation entry is invalid."));
        };
        let Some(ts) = entry.get("time").and_then(plist_number) else {
            return Err(CliError::value("A saved Jard dictation entry is invalid."));
        };
        let opt_str = |field: &str| -> Result<Option<String>, CliError> {
            match entry.get(field) {
                None => Ok(None),
                Some(PlistValue::String(s)) => Ok(Some(s.clone())),
                Some(_) => Err(CliError::value("A saved Jard dictation entry is invalid.")),
            }
        };
        let raw = opt_str("raw")?;
        let app = opt_str("app")?;
        let mode = opt_str("mode")?;
        let kind = opt_str("kind")?;
        let seconds = match entry.get("seconds") {
            None => None,
            Some(v) => Some(
                plist_number(v)
                    .ok_or_else(|| CliError::value("A saved Jard dictation entry is invalid."))?,
            ),
        };
        if ts < expires_before {
            continue;
        }
        total_bytes += text.len() + raw.as_deref().map_or(0, str::len);
        if total_bytes > HISTORY_TEXT_BUDGET {
            break;
        }
        entries.push(DictationEntry {
            id: id.clone(),
            text: text.clone(),
            time: ts,
            raw,
            app,
            seconds,
            mode,
            kind,
            score: None,
            snippet: None,
        });
    }
    Ok((entries, ret_label))
}

#[derive(Default)]
pub struct HistoryFilters {
    pub since: Option<String>,
    pub until: Option<String>,
    pub app: Option<String>,
    pub grep: Option<String>,
    pub search: Option<String>,
    pub oldest_first: bool,
    pub query: Option<String>,
    pub offset: usize,
    pub limit: usize,
    pub raw: bool,
    pub legacy: bool,
}

pub fn read_history_snapshot(root: &Path, filters: &HistoryFilters) -> Result<Value, CliError> {
    let path = preferences_path()?;
    let checkpoint = match desktop(root, "history-checkpoint", 12, Map::new()) {
        Ok(val) => Some(val),
        Err(CliError::DesktopUnavailable(_)) => None,
        Err(CliError::Runtime(msg))
            if msg == "Unknown Jard control action." || msg == "Unknown desktop control." =>
        {
            None
        }
        Err(e) => return Err(e),
    };
    let mut snapshot: Option<plist::Dictionary> = None;
    if let Some(cp_val) = checkpoint {
        let Some(cp_name) = cp_val.as_str() else {
            return Err(CliError::runtime("Jaro returned an invalid live history file."));
        };
        if !RE_CHECKPOINT.is_match(cp_name) {
            return Err(CliError::runtime("Jaro returned an invalid live history file."));
        }
        let sock_path = desktop_control_path(root)
            .map_err(|_| CliError::runtime("Jaro's live history file is unavailable."))?;
        let snapshot_path = sock_path.with_file_name(cp_name);
        let read_res = plist_to_dict(&snapshot_path);
        let _ = std::fs::remove_file(&snapshot_path);
        let saved = read_res?;
        if !saved.contains_key("JardSystemDictationHistory") {
            return Err(CliError::runtime("Jaro's live history file is unavailable."));
        }
        let stem = path.file_stem().and_then(|s| s.to_str());
        if saved.get("bundleId").and_then(|v| v.as_string()) == stem {
            snapshot = Some(saved);
        }
    }
    history_page(&path, filters, snapshot.as_ref())
}

pub fn history_page(
    path: &Path,
    filters: &HistoryFilters,
    snapshot: Option<&plist::Dictionary>,
) -> Result<Value, CliError> {
    if !(1..=HISTORY_LIMIT).contains(&filters.limit) {
        return Err(CliError::value("Use a history limit from 1 to 10,000."));
    }
    if filters.search.is_some() && filters.oldest_first {
        return Err(CliError::value("Use either ranked search or oldest-first order."));
    }
    let now = Utc::now();
    let lower = date_bound(filters.since.as_deref(), now, false)?;
    let upper = date_bound(filters.until.as_deref(), now, true)?;
    if let (Some(l), Some(u)) = (lower, upper) {
        if l >= u {
            return Err(CliError::value("The history start must be before its end."));
        }
    }
    let owned_dict;
    let source = match snapshot {
        Some(s) => s,
        None => {
            owned_dict = plist_to_dict(path)?;
            &owned_dict
        }
    };
    let (entries, retention) = saved_history(source, now)?;
    let first = lower
        .map(|d| d.timestamp() as f64 + (d.timestamp_subsec_nanos() as f64) / 1e9)
        .unwrap_or(f64::NEG_INFINITY);
    let last = upper
        .map(|d| d.timestamp() as f64 + (d.timestamp_subsec_nanos() as f64) / 1e9)
        .unwrap_or(f64::INFINITY);
    let app_folded = filters.app.as_deref().map(casefold);
    let literal = filters.grep.as_deref().map(casefold);
    let old_query = filters.query.as_deref().map(casefold);

    let mut filtered: Vec<DictationEntry> = Vec::new();
    for entry in entries {
        if !(first <= entry.time && entry.time < last) {
            continue;
        }
        let application = entry.app.as_deref().map(casefold).unwrap_or_default();
        if let Some(ref af) = app_folded {
            if af != &application {
                continue;
            }
        }
        let text_f = casefold(&entry.text);
        let raw_f = entry.raw.as_deref().map(casefold).unwrap_or_default();
        if let Some(ref lit) = literal {
            if !text_f.contains(lit.as_str()) && !raw_f.contains(lit.as_str()) {
                continue;
            }
        }
        if let Some(ref oq) = old_query {
            if !text_f.contains(oq.as_str())
                && !raw_f.contains(oq.as_str())
                && !application.contains(oq.as_str())
            {
                continue;
            }
        }
        filtered.push(entry);
    }

    filtered.sort_by(|a, b| {
        let ord = a.time.total_cmp(&b.time).then_with(|| a.id.cmp(&b.id));
        if filters.oldest_first { ord } else { ord.reverse() }
    });

    if let Some(ref search_q) = filters.search {
        let has_vocab_in_snap = snapshot
            .and_then(|s| s.get("JardSystemDictation"))
            .and_then(|v| v.as_dictionary())
            .is_some_and(|d| d.contains_key("vocabulary"));
        let vocab = if snapshot.is_none() || has_vocab_in_snap {
            search_vocabulary(Some(source), Some(path))
        } else {
            search_vocabulary(None, Some(path))
        };
        let mut passages: Vec<(String, String)> = Vec::new();
        let mut by_id: HashMap<String, DictationEntry> = HashMap::new();
        for entry in filtered {
            let key = entry.id.clone();
            passages.push((key.clone(), entry.text.clone()));
            if let Some(ref raw_t) = entry.raw {
                if !raw_t.is_empty() && raw_t != &entry.text {
                    passages.push((key.clone(), raw_t.clone()));
                }
            }
            by_id.insert(key, entry);
        }
        let (hits, _) = rank_passages(&passages, search_q, passages.len(), true, true, &vocab)?;
        let mut seen = HashSet::new();
        let mut ranked = Vec::new();
        for hit in hits {
            let doc_id = &passages[hit.index].0;
            if seen.insert(doc_id.clone()) {
                if let Some(mut e) = by_id.get(doc_id).cloned() {
                    e.score = Some(hit.score);
                    e.snippet = Some(hit.snippet);
                    ranked.push(e);
                }
            }
        }
        filtered = ranked;
    }

    let total_len = filtered.len();
    let start = filters.offset.min(total_len);
    let end = (start + filters.limit).min(total_len);
    let page = &filtered[start..end];

    if filters.legacy {
        let words_total: usize = filtered.iter().map(|e| count_tokens(&e.text)).sum();
        let seconds_total: f64 = filtered.iter().filter_map(|e| e.seconds).sum();
        let out_entries: Vec<Value> = page
            .iter()
            .map(|e| {
                let mut m = Map::new();
                m.insert("id".into(), Value::String(e.id.clone()));
                m.insert("time".into(), json!(e.time));
                m.insert("text".into(), Value::String(e.text.clone()));
                if let Some(ref r) = e.raw {
                    m.insert("raw".into(), Value::String(r.clone()));
                }
                if let Some(ref a) = e.app {
                    m.insert("app".into(), Value::String(a.clone()));
                }
                if let Some(s) = e.seconds {
                    m.insert("seconds".into(), json!(s));
                }
                if let Some(ref mo) = e.mode {
                    m.insert("mode".into(), Value::String(mo.clone()));
                }
                if let Some(ref k) = e.kind {
                    m.insert("kind".into(), Value::String(k.clone()));
                }
                Value::Object(m)
            })
            .collect();
        return Ok(json!({
            "entries": out_entries,
            "total": total_len,
            "words": words_total,
            "seconds": seconds_total,
        }));
    }

    let out_entries: Vec<Value> = page
        .iter()
        .map(|e| {
            let mut m = Map::new();
            m.insert("id".into(), Value::String(e.id.clone()));
            m.insert("timestamp_ms".into(), json!(py_round_i64(e.time * 1000.0)));
            m.insert(
                "application".into(),
                e.app.as_ref().map(|s| Value::String(s.clone())).unwrap_or(Value::Null),
            );
            m.insert(
                "mode".into(),
                e.mode.as_ref().map(|s| Value::String(s.clone())).unwrap_or(Value::Null),
            );
            m.insert(
                "kind".into(),
                e.kind.as_ref().map(|s| Value::String(s.clone())).unwrap_or(Value::Null),
            );
            m.insert("final_text".into(), Value::String(e.text.clone()));
            if filters.raw {
                m.insert(
                    "raw_text".into(),
                    e.raw.as_ref().map(|s| Value::String(s.clone())).unwrap_or(Value::Null),
                );
            }
            if let Some(sc) = e.score {
                m.insert("score".into(), json!(sc));
            }
            if let Some(ref sn) = e.snippet {
                m.insert("snippet".into(), Value::String(sn.clone()));
            }
            Value::Object(m)
        })
        .collect();

    Ok(json!({
        "version": 1,
        "retention": retention,
        "truncated": total_len > filters.offset.saturating_add(filters.limit),
        "entries": out_entries,
    }))
}

pub fn render_history(result: &Value) -> String {
    let entries = result
        .get("entries")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let mut parts = Vec::new();
    for entry in entries {
        let ts_ms = entry.get("timestamp_ms").and_then(Value::as_i64).unwrap_or(0);
        let dt = DateTime::<Utc>::from_timestamp_millis(ts_ms).unwrap_or_else(Utc::now);
        let heading = format_local_iso_seconds(dt);
        let labels = [
            entry.get("application").and_then(Value::as_str),
            entry.get("mode").and_then(Value::as_str),
        ]
        .into_iter()
        .flatten()
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" · ");
        let first_line = if labels.is_empty() {
            heading
        } else {
            format!("{heading}  {labels}")
        };
        let mut lines = vec![
            first_line,
            entry
                .get("final_text")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
        ];
        if let Some(raw_text) = entry.get("raw_text").and_then(Value::as_str) {
            lines.push("Original transcript".to_string());
            lines.push(raw_text.to_string());
        }
        if let Some(snip) = entry.get("snippet").and_then(Value::as_str) {
            lines.push(snip.to_string());
        }
        parts.push(lines.join("\n"));
    }
    if parts.is_empty() {
        "No retained dictations matched.".to_string()
    } else {
        parts.join("\n\n")
    }
}
