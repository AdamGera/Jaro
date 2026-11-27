use std::ffi::CString;
use std::fs::File;
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom, Write};
use std::net::{SocketAddr, TcpStream};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::io::{AsRawFd, FromRawFd};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::thread;
use std::time::Duration;

use serde_json::{Map, Value};

use crate::error::CliError;
use crate::paths::{
    API_VERSION, HOST, constant_time_eq, desktop_control_path, engine_proof, library_id,
    library_path, log_directory, port, random_hex, read_native_token, read_native_token_plain, url,
};

const CONTROL_LIMIT: usize = 4 * 1024 * 1024;
const TAIL_BYTES: usize = 4 * 1024 * 1024;

pub fn control(
    root: &Path,
    token: &str,
    action: &str,
    timeout_secs: u64,
    options: Map<String, Value>,
) -> Result<Value, CliError> {
    let mut payload = options;
    payload.insert("token".to_string(), Value::String(token.to_string()));
    payload.insert("action".to_string(), Value::String(action.to_string()));
    let mut bytes = serde_json::to_vec(&Value::Object(payload))
        .map_err(|_| CliError::desktop_unavailable("Open Jaro before using desktop controls."))?;
    bytes.push(b'\n');

    let sock_path = desktop_control_path(root)
        .map_err(|_| CliError::desktop_unavailable("Open Jaro before using desktop controls."))?;
    let mut stream = UnixStream::connect(&sock_path)
        .map_err(|_| CliError::desktop_unavailable("Open Jaro before using desktop controls."))?;
    let timeout = Some(Duration::from_secs(timeout_secs));
    let _ = stream.set_read_timeout(timeout);
    let _ = stream.set_write_timeout(timeout);
    stream
        .write_all(&bytes)
        .map_err(|_| CliError::desktop_unavailable("Open Jaro before using desktop controls."))?;

    let mut line = Vec::new();
    let mut reader = BufReader::new(stream).take(CONTROL_LIMIT as u64);
    reader
        .read_until(b'\n', &mut line)
        .map_err(|_| CliError::desktop_unavailable("Open Jaro before using desktop controls."))?;
    let result: Value = serde_json::from_slice(&line)
        .map_err(|_| CliError::desktop_unavailable("Open Jaro before using desktop controls."))?;
    let ok = result.get("ok").and_then(Value::as_bool).unwrap_or(false);
    if !ok {
        let err_msg = result
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("The desktop command failed.")
            .to_string();
        if result.get("code").and_then(Value::as_str) == Some("managed") {
            return Err(CliError::managed(err_msg));
        }
        return Err(CliError::runtime(err_msg));
    }
    Ok(result.get("result").cloned().unwrap_or(Value::Null))
}

pub fn desktop(
    root: &Path,
    action: &str,
    timeout_secs: u64,
    options: Map<String, Value>,
) -> Result<Value, CliError> {
    let token = read_native_token_plain(root)
        .map_err(|_| CliError::desktop_unavailable("Open Jaro before using desktop controls."))?;
    control(root, &token, action, timeout_secs, options)
}

pub fn live_meeting(root: &Path, turns: bool) -> Option<Value> {
    let token = read_native_token_plain(root).ok()?;
    let mut opts = Map::new();
    opts.insert("turns".to_string(), Value::Bool(turns));
    let val = control(root, &token, "meeting-live", 1, opts).ok()?;
    if val.is_object() { Some(val) } else { None }
}

pub fn running_engine() -> Result<Option<Value>, CliError> {
    let p = port();
    let addr: SocketAddr = format!("{HOST}:{p}").parse().unwrap();
    match TcpStream::connect_timeout(&addr, Duration::from_millis(500)) {
        Ok(_) => {}
        Err(err) if err.kind() == std::io::ErrorKind::ConnectionRefused => return Ok(None),
        Err(err) => return Err(CliError::runtime(format!("Could not reach {}: {err}", url()))),
    }

    let challenge = random_hex(16)
        .map_err(|e| CliError::runtime(format!("Could not verify the running Jaro engine: {e}")))?;

    let info = (|| -> Result<Value, String> {
        let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(2))
            .map_err(|e| e.to_string())?;
        let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
        let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));
        let req = format!(
            "GET /api/status?challenge={challenge} HTTP/1.1\r\nHost: {HOST}:{p}\r\nConnection: close\r\n\r\n"
        );
        stream.write_all(req.as_bytes()).map_err(|e| e.to_string())?;
        let mut raw = Vec::new();
        stream.take(65_536).read_to_end(&mut raw).map_err(|e| e.to_string())?;
        let header_end = raw
            .windows(4)
            .position(|w| w == b"\r\n\r\n")
            .ok_or_else(|| "unexpected response".to_string())?;
        let headers = std::str::from_utf8(&raw[..header_end]).map_err(|_| "unexpected response".to_string())?;
        let status_line = headers.lines().next().unwrap_or("");
        let status_code = status_line.split_whitespace().nth(1).unwrap_or("");
        if status_code != "200" {
            return Err("unexpected response".to_string());
        }
        let body = &raw[header_end + 4..];
        let body = if headers
            .lines()
            .any(|l| l.to_ascii_lowercase().starts_with("transfer-encoding:") && l.to_ascii_lowercase().contains("chunked"))
        {
            decode_chunked(body).ok_or_else(|| "unexpected response".to_string())?
        } else {
            body.to_vec()
        };
        let limited = &body[..body.len().min(32_768)];
        let val: Value = serde_json::from_slice(limited).map_err(|_| "unexpected response".to_string())?;
        if val.get("model").and_then(Value::as_str) != Some("Reson8/Rumble-2-Flash") {
            return Err("unexpected server".to_string());
        }
        Ok(val)
    })()
    .map_err(|_| CliError::runtime(format!("Another application is using {}. Close it and reopen Jaro.", url())))?;

    if info.get("apiVersion").and_then(Value::as_i64) != Some(API_VERSION) {
        return Err(CliError::runtime(
            "Another Jaro version is running. Quit it, then reopen Jaro.",
        ));
    }
    let root = library_path();
    if info.get("libraryId").and_then(Value::as_str) != Some(library_id(&root).as_str()) {
        return Err(CliError::runtime(
            "The running Jaro engine is using a different session library.",
        ));
    }
    let token = read_native_token(&root)
        .map_err(|_| CliError::runtime("Could not verify the running Jaro engine."))?;
    let expected = engine_proof(&token, &challenge);
    let proof = info.get("proof").and_then(Value::as_str).unwrap_or("");
    if !proof.is_ascii() || !constant_time_eq(proof.as_bytes(), expected.as_bytes()) {
        return Err(CliError::runtime("Could not verify the running Jaro engine."));
    }
    Ok(Some(info))
}

fn decode_chunked(mut data: &[u8]) -> Option<Vec<u8>> {
    let mut out = Vec::new();
    loop {
        let pos = data.windows(2).position(|w| w == b"\r\n")?;
        let size_str = std::str::from_utf8(&data[..pos]).ok()?.split(';').next()?.trim();
        let size = usize::from_str_radix(size_str, 16).ok()?;
        data = &data[pos + 2..];
        if size == 0 {
            break;
        }
        if data.len() < size + 2 {
            return None;
        }
        out.extend_from_slice(&data[..size]);
        data = &data[size + 2..];
    }
    Some(out)
}

fn open_log_readonly(name: &str) -> Result<File, std::io::Error> {
    let path = log_directory().join(name);
    let cpath = CString::new(path.as_os_str().as_bytes())
        .map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidInput, "invalid log path"))?;
    let flags = libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK;
    let fd = unsafe { libc::open(cpath.as_ptr(), flags) };
    if fd < 0 {
        return Err(std::io::Error::last_os_error());
    }
    let file = unsafe { File::from_raw_fd(fd) };
    let mut st: libc::stat = unsafe { std::mem::zeroed() };
    if unsafe { libc::fstat(fd, &mut st) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    let euid = unsafe { libc::geteuid() };
    if (st.st_mode & libc::S_IFMT) != libc::S_IFREG || st.st_uid != euid || (st.st_mode & 0o022) != 0 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::PermissionDenied,
            "Jaro logs must be regular files owned by the current user.",
        ));
    }
    Ok(file)
}

fn file_stat(file: &File) -> Result<(u64, u64, u64), std::io::Error> {
    let mut st: libc::stat = unsafe { std::mem::zeroed() };
    if unsafe { libc::fstat(file.as_raw_fd(), &mut st) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok((st.st_dev as u64, st.st_ino as u64, st.st_size.max(0) as u64))
}

fn initial_tail(file: &mut File, lines: usize, follow: bool) -> Result<(Vec<u8>, bool, u64), std::io::Error> {
    let (_, _, size) = file_stat(file)?;
    let start = size.saturating_sub(TAIL_BYTES as u64);
    file.seek(SeekFrom::Start(start.saturating_sub(1)))?;
    if start > 0 {
        let mut prev = [0u8; 1];
        let n = file.read(&mut prev)?;
        if n == 0 || prev[0] != b'\n' {
            let mut reader = BufReader::new(&mut *file);
            let mut discarded = Vec::new();
            let mut limited = reader.by_ref().take(TAIL_BYTES as u64);
            limited.read_until(b'\n', &mut discarded)?;
            let pos = file.stream_position()?;
            if !discarded.ends_with(b"\n") {
                return Ok((Vec::new(), true, pos));
            }
        }
    }
    let pos_before = file.stream_position()?;
    let to_read = size.saturating_sub(pos_before) as usize;
    let mut data = vec![0u8; to_read];
    let n = file.read(&mut data)?;
    data.truncate(n);
    let mut position = pos_before + n as u64;
    if follow && !data.is_empty() && !data.ends_with(b"\n") {
        let ending = data.iter().rposition(|&b| b == b'\n').map_or(0, |i| i + 1);
        let back = (data.len() - ending) as u64;
        position -= back;
        data.truncate(ending);
    }
    if lines == 0 {
        return Ok((Vec::new(), false, position));
    }
    let mut start_of_line = data.len().saturating_sub(usize::from(data.ends_with(b"\n")));
    let mut found_idx: Option<usize> = None;
    for _ in 0..lines {
        if let Some(pos) = data[..start_of_line].iter().rposition(|&b| b == b'\n') {
            start_of_line = pos;
            found_idx = Some(pos);
        } else {
            found_idx = None;
            break;
        }
    }
    let slice_start = found_idx.map_or(0, |i| i + 1);
    Ok((data[slice_start..].to_vec(), false, position))
}

pub fn tail_log(name: &str, lines: usize, follow: bool, process_log: bool) -> Result<(), CliError> {
    unsafe {
        libc::signal(libc::SIGPIPE, libc::SIG_DFL);
    }
    let mut file = match open_log_readonly(name) {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Err(CliError::value(if process_log {
                "Jaro has no process log yet."
            } else {
                "Jaro has no event log yet."
            }));
        }
        Err(e) => return Err(CliError::runtime(e.to_string())),
    };
    let (dev, ino, _) = file_stat(&file)?;
    let mut identity = (dev, ino);
    let (data, mut skipping, mut position) = initial_tail(&mut file, lines, follow)?;
    drop(file);

    let mut stdout = std::io::stdout().lock();
    stdout.write_all(&data)?;
    stdout.flush()?;

    while follow {
        thread::sleep(Duration::from_millis(250));
        let mut f = match open_log_readonly(name) {
            Ok(f) => f,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(e) => return Err(CliError::runtime(e.to_string())),
        };
        let (d, i, sz) = file_stat(&f)?;
        let current = (d, i);
        if current != identity || sz < position {
            identity = current;
            position = 0;
            skipping = false;
        }
        f.seek(SeekFrom::Start(position))?;
        loop {
            let mut line = Vec::new();
            let mut reader = BufReader::new(&mut f);
            let mut limited = reader.by_ref().take(TAIL_BYTES as u64);
            let n = limited.read_until(b'\n', &mut line)?;
            if n == 0 {
                break;
            }
            let complete = line.ends_with(b"\n");
            if !complete && !skipping && line.len() < TAIL_BYTES {
                break;
            }
            position += n as u64;
            if complete && !skipping {
                stdout.write_all(&line)?;
            }
            skipping = !complete;
            f.seek(SeekFrom::Start(position))?;
        }
        stdout.flush()?;
    }
    Ok(())
}
