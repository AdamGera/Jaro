use std::sync::LazyLock;

use chrono::{DateTime, Datelike, Days, Duration, Local, NaiveDate, NaiveDateTime, NaiveTime, TimeZone, Timelike, Utc};
use regex::Regex;

use crate::error::CliError;

static RE_EPOCH: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[0-9]{10,13}$").unwrap());
static RE_RELATIVE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^([0-9]+)\s*([mhdw])(?: ago)?$").unwrap());
static RE_DATE_ONLY: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$").unwrap());
static RE_ISO_DT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[t ][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?\s*(?:z|[+-][0-9]{2}:?[0-9]{2})?$")
        .unwrap()
});

pub fn parse_offset(value: Option<&str>) -> Result<Option<u64>, CliError> {
    let Some(raw) = value else {
        return Ok(None);
    };
    let text = raw.trim().to_ascii_lowercase();
    if text.is_empty() {
        return Ok(None);
    }
    let err = || CliError::value("Use seconds, MM:SS, or HH:MM:SS for transcript offsets.");
    let seconds: f64 = if text.contains(':') {
        let parts: Vec<&str> = text.split(':').map(|p| p.trim()).collect();
        if parts.len() != 2 && parts.len() != 3 {
            return Err(err());
        }
        if parts.iter().any(|p| p.starts_with('+') || p.starts_with('-')) {
            return Err(err());
        }
        let mut numbers = Vec::with_capacity(parts.len());
        for part in &parts {
            let n: f64 = part.parse().map_err(|_| err())?;
            if !n.is_finite() {
                return Err(err());
            }
            numbers.push(n);
        }
        if numbers[..numbers.len() - 1].iter().any(|n| n.fract() != 0.0) {
            return Err(err());
        }
        if numbers[1..].iter().any(|&n| n >= 60.0) {
            return Err(err());
        }
        let mut total = 0.0;
        for (idx, &n) in numbers.iter().rev().enumerate() {
            total += n * 60f64.powi(idx as i32);
        }
        total
    } else {
        let (amount, scale) = match text.as_bytes().last() {
            Some(b's') => (text[..text.len() - 1].trim(), 1.0),
            Some(b'm') => (text[..text.len() - 1].trim(), 60.0),
            Some(b'h') => (text[..text.len() - 1].trim(), 3600.0),
            _ => (text.as_str(), 1.0),
        };
        if amount.starts_with('+') || amount.starts_with('-') {
            return Err(err());
        }
        let n: f64 = amount.parse().map_err(|_| err())?;
        n * scale
    };
    let ms = seconds * 1000.0;
    if !ms.is_finite() || ms < 0.0 || ms >= 18446744073709551616.0 {
        return Err(err());
    }
    Ok(Some((ms + 0.5).floor() as u64))
}

fn local_midnight_utc(d: NaiveDate) -> Result<DateTime<Utc>, CliError> {
    let dt = d.and_time(NaiveTime::MIN);
    let local = Local
        .from_local_datetime(&dt)
        .earliest()
        .or_else(|| Local.from_local_datetime(&dt).latest())
        .ok_or_else(|| CliError::value("Use a date, ISO date and time, today, yesterday, or a period such as 7d."))?;
    Ok(local.with_timezone(&Utc))
}

pub fn parse_iso_utc(value: &str) -> Option<DateTime<Utc>> {
    let trimmed = value.trim();
    if let Ok(dt) = DateTime::parse_from_rfc3339(trimmed) {
        return Some(dt.with_timezone(&Utc));
    }
    // Normalize lowercase t/z, space separator, and +HHMM without colon
    let mut normalized = trimmed.replace(' ', "T").replace('t', "T").replace('z', "+00:00").replace('Z', "+00:00");
    // Strip whitespace before timezone offset if any (e.g. "2026-10-20T12:00 +02:00")
    if let Some(pos) = normalized.rfind(['+', '-']) {
        if pos > 10 {
            let before = normalized[..pos].trim_end();
            let tz = &normalized[pos..];
            let tz_fixed = if tz.len() == 5 && !tz.contains(':') {
                format!("{}:{}", &tz[..3], &tz[3..])
            } else {
                tz.to_string()
            };
            normalized = format!("{before}{tz_fixed}");
        }
    }
    if let Ok(dt) = DateTime::parse_from_rfc3339(&normalized) {
        return Some(dt.with_timezone(&Utc));
    }
    // Handle YYYY-MM-DDTHH:MM+HH:MM (no seconds)
    if let Ok(dt) = DateTime::parse_from_str(&normalized, "%Y-%m-%dT%H:%M%:z") {
        return Some(dt.with_timezone(&Utc));
    }
    // Naive datetime -> interpret in local timezone like Python datetime.fromisoformat(...).astimezone(timezone.utc)
    for fmt in [
        "%Y-%m-%dT%H:%M:%S%.f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
    ] {
        if let Ok(ndt) = NaiveDateTime::parse_from_str(&normalized, fmt) {
            if let Some(local) = Local.from_local_datetime(&ndt).earliest() {
                return Some(local.with_timezone(&Utc));
            }
        }
    }
    None
}

pub fn date_bound(value: Option<&str>, now: DateTime<Utc>, through: bool) -> Result<Option<DateTime<Utc>>, CliError> {
    let Some(raw) = value else {
        return Ok(None);
    };
    let val = raw.trim().to_ascii_lowercase();
    let err = || CliError::value("Use a date, ISO date and time, today, yesterday, or a period such as 7d.");
    if val == "now" {
        return Ok(Some(now));
    }
    if val == "today" || val == "yesterday" {
        let mut local_date = now.with_timezone(&Local).date_naive();
        if val == "yesterday" {
            local_date = local_date.checked_sub_days(Days::new(1)).ok_or_else(err)?;
        }
        if through {
            local_date = local_date.checked_add_days(Days::new(1)).ok_or_else(err)?;
        }
        return Ok(Some(local_midnight_utc(local_date)?));
    }
    if RE_EPOCH.is_match(&val) {
        let n: i64 = val.parse().map_err(|_| err())?;
        let dt = if val.len() > 10 {
            DateTime::<Utc>::from_timestamp_millis(n)
        } else {
            DateTime::<Utc>::from_timestamp(n, 0)
        }
        .ok_or_else(err)?;
        return Ok(Some(dt));
    }
    if let Some(caps) = RE_RELATIVE.captures(&val) {
        let amount: i64 = caps[1].parse().map_err(|_| err())?;
        let mins = match &caps[2] {
            "m" => 1i64,
            "h" => 60,
            "d" => 1440,
            "w" => 10080,
            _ => return Err(err()),
        };
        let total_mins = amount.checked_mul(mins).ok_or_else(err)?;
        let delta = Duration::try_minutes(total_mins).ok_or_else(err)?;
        return Ok(Some(now.checked_sub_signed(delta).ok_or_else(err)?));
    }
    if RE_DATE_ONLY.is_match(&val) {
        let mut d = NaiveDate::parse_from_str(&val, "%Y-%m-%d").map_err(|_| err())?;
        if through {
            d = d.checked_add_days(Days::new(1)).ok_or_else(err)?;
        }
        return Ok(Some(local_midnight_utc(d)?));
    }
    if RE_ISO_DT.is_match(&val) {
        if let Some(dt) = parse_iso_utc(&val) {
            return Ok(Some(dt));
        }
    }
    Err(err())
}

/// Python-style round-half-to-even for integer rounding of floats (`round(x)`).
pub fn py_round_i64(x: f64) -> i64 {
    x.round_ties_even() as i64
}

pub fn timestamp(seconds: f64, subtitle: bool, srt: bool) -> String {
    let total_ms = py_round_i64(seconds * 1000.0).max(0);
    let hours = total_ms / 3_600_000;
    let rest = total_ms % 3_600_000;
    let minutes = rest / 60_000;
    let rest = rest % 60_000;
    let secs = rest / 1000;
    let ms = rest % 1000;
    if subtitle {
        let sep = if srt { ',' } else { '.' };
        format!("{hours:02}:{minutes:02}:{secs:02}{sep}{ms:03}")
    } else if hours > 0 {
        format!("{hours}:{minutes:02}:{secs:02}")
    } else {
        format!("{minutes:02}:{secs:02}")
    }
}

pub fn range_clock(milliseconds: u64) -> String {
    let seconds = milliseconds / 1000;
    let fraction = milliseconds % 1000;
    let base = timestamp(seconds as f64, false, false);
    if fraction > 0 {
        format!("{base}.{fraction:03}")
    } else {
        base
    }
}

pub fn frame_time(seconds: f64) -> String {
    let total_tenths = (seconds * 10.0 + 0.0001) as i64;
    let whole = total_tenths / 10;
    let tenth = total_tenths % 10;
    let minutes = whole / 60;
    let secs = whole % 60;
    let hours = minutes / 60;
    let mins_of_hour = minutes % 60;
    let clock = if hours > 0 {
        format!("{hours}:{mins_of_hour:02}")
    } else {
        format!("{minutes}")
    };
    format!("{clock}:{secs:02}.{tenth}")
}

pub fn meeting_date_line(iso: &str, use_24_hour: bool) -> String {
    let dt = parse_iso_utc(iso)
        .unwrap_or_else(Utc::now)
        .with_timezone(&Local);
    let clock = if use_24_hour {
        format!("{:02}:{:02}", dt.hour(), dt.minute())
    } else {
        let period = if dt.hour() < 12 { "AM" } else { "PM" };
        let hour12 = match dt.hour() % 12 {
            0 => 12,
            h => h,
        };
        format!("{hour12}:{:02} {period}", dt.minute())
    };
    format!("- **Date:** {:04}-{:02}-{:02}, {clock}", dt.year(), dt.month(), dt.day())
}

pub fn format_local_iso_millis(dt: DateTime<Utc>) -> String {
    let local = dt.with_timezone(&Local);
    local.format("%Y-%m-%dT%H:%M:%S%.3f%:z").to_string()
}

pub fn format_local_iso_seconds(dt: DateTime<Utc>) -> String {
    let local = dt.with_timezone(&Local);
    local.format("%Y-%m-%dT%H:%M:%S%:z").to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_offset() {
        assert_eq!(parse_offset(None).unwrap(), None);
        assert_eq!(parse_offset(Some("90")).unwrap(), Some(90_000));
        assert_eq!(parse_offset(Some("1:30")).unwrap(), Some(90_000));
        assert_eq!(parse_offset(Some("1:02:03.5")).unwrap(), Some(3_723_500));
        assert_eq!(parse_offset(Some("2m")).unwrap(), Some(120_000));
        assert_eq!(parse_offset(Some("1h")).unwrap(), Some(3_600_000));
        assert!(parse_offset(Some("-5")).is_err());
        assert!(parse_offset(Some("1:60")).is_err());
    }

    #[test]
    fn test_timestamp_and_frame_time() {
        assert_eq!(timestamp(65.0, false, false), "01:05");
        assert_eq!(timestamp(3665.0, false, false), "1:01:05");
        assert_eq!(timestamp(65.125, true, true), "00:01:05,125");
        assert_eq!(timestamp(65.125, true, false), "00:01:05.125");
        assert_eq!(frame_time(12.3), "0:12.3");
        assert_eq!(frame_time(3661.5), "1:01:01.5");
    }
}
