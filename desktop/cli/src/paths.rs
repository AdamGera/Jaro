use std::env;
use std::ffi::CString;
use std::fs::File;
use std::io::Read;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::io::FromRawFd;
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

use crate::error::CliError;

pub const HOST: &str = "127.0.0.1";
pub const API_VERSION: i64 = 7;
pub const DEFAULT_BUNDLE_ID: &str = "local.jardaudio.desktop";
pub const EVENT_LOG: &str = "live.ndjson";
pub const PROCESS_LOG: &str = "server.log";

pub fn home_dir() -> PathBuf {
    env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/"))
}

pub fn expanduser(path: &str) -> PathBuf {
    if path == "~" {
        return home_dir();
    }
    if let Some(rest) = path.strip_prefix("~/") {
        return home_dir().join(rest);
    }
    PathBuf::from(path)
}

pub fn library_path() -> PathBuf {
    if let Ok(configured) = env::var("RUMBLE_DATA_DIR") {
        if !configured.is_empty() {
            return expanduser(&configured);
        }
    }
    if cfg!(target_os = "macos") {
        return home_dir().join("Library/Application Support/Rumble");
    }
    if let Ok(xdg) = env::var("XDG_DATA_HOME") {
        if !xdg.is_empty() {
            return expanduser(&xdg).join("jaro");
        }
    }
    home_dir().join(".local/share/jaro")
}

pub fn log_directory() -> PathBuf {
    if let Ok(configured) = env::var("RUMBLE_LOG_DIR") {
        if !configured.is_empty() {
            return PathBuf::from(configured);
        }
    }
    if cfg!(target_os = "macos") {
        return home_dir().join("Library/Logs/Jaro");
    }
    library_path().join("logs")
}

pub fn port() -> u16 {
    env::var("RUMBLE_PORT")
        .ok()
        .and_then(|s| s.parse::<u16>().ok())
        .unwrap_or(8765)
}

pub fn url() -> String {
    format!("http://{HOST}:{}", port())
}

fn bundled_bundle_id() -> Option<String> {
    let exe = env::current_exe().ok()?.canonicalize().ok()?;
    let contents = exe.parent()?.parent()?;
    let info_plist = contents.join("Info.plist");
    if !info_plist.is_file() {
        return None;
    }
    let val = plist::Value::from_file(info_plist).ok()?;
    val.as_dictionary()?
        .get("CFBundleIdentifier")?
        .as_string()
        .map(String::from)
}

pub fn preferences_path() -> Result<PathBuf, CliError> {
    let identifier = env::var("JARD_DESKTOP_BUNDLE_ID")
        .ok()
        .or_else(bundled_bundle_id)
        .unwrap_or_else(|| DEFAULT_BUNDLE_ID.to_string());
    let valid = !identifier.is_empty()
        && identifier.as_bytes()[0].is_ascii_alphanumeric()
        && identifier
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'-');
    if !valid {
        return Err(CliError::value("The Jard desktop bundle identifier is invalid."));
    }
    Ok(home_dir()
        .join("Library")
        .join("Preferences")
        .join(format!("{identifier}.plist")))
}

/// Open a regular file owned by current euid with no group/other permissions, refusing symlinks.
pub fn open_private_regular(path: &Path) -> Result<Option<File>, std::io::Error> {
    let cpath = CString::new(path.as_os_str().as_bytes())
        .map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidInput, "invalid path"))?;
    let flags = libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK;
    let fd = unsafe { libc::open(cpath.as_ptr(), flags) };
    if fd < 0 {
        let err = std::io::Error::last_os_error();
        if err.kind() == std::io::ErrorKind::NotFound {
            return Ok(None);
        }
        return Err(err);
    }
    let file = unsafe { File::from_raw_fd(fd) };
    let mut st: libc::stat = unsafe { std::mem::zeroed() };
    if unsafe { libc::fstat(fd, &mut st) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    let euid = unsafe { libc::geteuid() };
    if (st.st_mode & libc::S_IFMT) != libc::S_IFREG || st.st_uid != euid || (st.st_mode & 0o077) != 0 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::PermissionDenied,
            "not a private regular file owned by current user",
        ));
    }
    Ok(Some(file))
}

pub fn desktop_control_path(root: &Path) -> Result<PathBuf, std::io::Error> {
    let pointer = root.join(".native-control");
    let mut file = open_private_regular(&pointer)?.ok_or_else(|| {
        std::io::Error::new(std::io::ErrorKind::NotFound, "desktop control pointer missing")
    })?;
    let mut buf = [0u8; 256];
    let n = file.read(&mut buf)?;
    let text = std::str::from_utf8(&buf[..n])
        .map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidData, "Invalid desktop pointer"))?;
    let path = PathBuf::from(text.trim());
    let name_ok = path.file_name().and_then(|n| n.to_str()) == Some("socket");
    let parent = path.parent();
    let parent_name_ok = parent
        .and_then(|p| p.file_name())
        .and_then(|n| n.to_str())
        .map(|s| s.starts_with("jard-control-"))
        .unwrap_or(false);
    let grandparent_ok = parent.and_then(|p| p.parent()) == Some(Path::new("/tmp"));
    if !name_ok || !parent_name_ok || !grandparent_ok {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "Invalid desktop socket",
        ));
    }
    let parent_path = parent.unwrap();
    let parent_c = CString::new(parent_path.as_os_str().as_bytes())
        .map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidInput, "Invalid path"))?;
    let mut pst: libc::stat = unsafe { std::mem::zeroed() };
    if unsafe { libc::lstat(parent_c.as_ptr(), &mut pst) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    let euid = unsafe { libc::geteuid() };
    if (pst.st_mode & libc::S_IFMT) != libc::S_IFDIR || pst.st_uid != euid || (pst.st_mode & 0o077) != 0 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::PermissionDenied,
            "Invalid desktop socket owner",
        ));
    }
    let sock_c = CString::new(path.as_os_str().as_bytes())
        .map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidInput, "Invalid path"))?;
    let mut sst: libc::stat = unsafe { std::mem::zeroed() };
    if unsafe { libc::lstat(sock_c.as_ptr(), &mut sst) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    if (sst.st_mode & libc::S_IFMT) != libc::S_IFSOCK {
        return Err(std::io::Error::new(
            std::io::ErrorKind::Other,
            "The desktop control service is unavailable",
        ));
    }
    Ok(path)
}

pub fn read_native_token(root: &Path) -> Result<String, std::io::Error> {
    let token_path = root.join(".native-token");
    let mut file = open_private_regular(&token_path)?.ok_or_else(|| {
        std::io::Error::new(std::io::ErrorKind::NotFound, "Invalid Jaro engine credential.")
    })?;
    let mut buf = [0u8; 66];
    let n = file.read(&mut buf)?;
    let slice = &buf[..n];
    let trimmed = slice.strip_suffix(b"\n").unwrap_or(slice);
    if trimmed.len() != 64 || !trimmed.iter().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "Invalid Jaro engine credential.",
        ));
    }
    Ok(std::str::from_utf8(trimmed).unwrap().to_string())
}

pub fn read_native_token_plain(root: &Path) -> Result<String, std::io::Error> {
    let text = std::fs::read_to_string(root.join(".native-token"))?;
    Ok(text.trim().to_string())
}

pub fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for &b in bytes {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 0x0f) as usize] as char);
    }
    out
}

pub fn library_id(root: &Path) -> String {
    let mut hasher = Sha256::new();
    hasher.update(root.as_os_str().as_bytes());
    hex_encode(&hasher.finalize())
}

/// RFC 2104 HMAC-SHA256 hex digest matching Python `hmac.digest(token.encode(), challenge.encode("ascii"), "sha256").hex()`.
pub fn engine_proof(token: &str, challenge: &str) -> String {
    let key = token.as_bytes();
    let mut key_block = [0u8; 64];
    if key.len() > 64 {
        let hashed = Sha256::digest(key);
        key_block[..32].copy_from_slice(&hashed);
    } else {
        key_block[..key.len()].copy_from_slice(key);
    }
    let mut ipad = [0x36u8; 64];
    let mut opad = [0x5cu8; 64];
    for i in 0..64 {
        ipad[i] ^= key_block[i];
        opad[i] ^= key_block[i];
    }
    let mut inner = Sha256::new();
    inner.update(ipad);
    inner.update(challenge.as_bytes());
    let inner_digest = inner.finalize();

    let mut outer = Sha256::new();
    outer.update(opad);
    outer.update(inner_digest);
    hex_encode(&outer.finalize())
}

pub fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (&x, &y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

pub fn random_hex(nbytes: usize) -> Result<String, std::io::Error> {
    let mut f = File::open("/dev/urandom")?;
    let mut buf = vec![0u8; nbytes];
    f.read_exact(&mut buf)?;
    Ok(hex_encode(&buf))
}

/// Unicode full casefolding matching Python `str.casefold()`.
pub fn casefold(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for ch in s.chars() {
        match ch {
            'ß' | 'ẞ' => out.push_str("ss"),
            'ς' => out.push('σ'),
            'µ' => out.push('μ'),
            'İ' => out.push_str("i\u{0307}"),
            'ﬀ' => out.push_str("ff"),
            'ﬁ' => out.push_str("fi"),
            'ﬂ' => out.push_str("fl"),
            'ﬃ' => out.push_str("ffi"),
            'ﬄ' => out.push_str("ffl"),
            'ﬅ' | 'ﬆ' => out.push_str("st"),
            _ => {
                for lower in ch.to_lowercase() {
                    out.push(lower);
                }
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_hmac_sha256_known_vector() {
        // RFC 4231 Test Case 2: key="Jefe", data="what do ya want for nothing?"
        let mac = engine_proof("Jefe", "what do ya want for nothing?");
        assert_eq!(
            mac,
            "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
        );
    }

    #[test]
    fn test_casefold_unicode() {
        assert_eq!(casefold("Straße"), "strasse");
        assert_eq!(casefold("GROẞ"), "gross");
        assert_eq!(casefold("İstanbul"), "i\u{0307}stanbul");
    }
}
