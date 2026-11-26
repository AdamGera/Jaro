//! Safe, bounded GGUF header parser for inspecting Parakeet streaming chunk
//! menus (`att_chunk_chunk_choices`, `att_chunk_right_choices`) without loading
//! model weights.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufReader, Read, Seek, SeekFrom};
use std::path::Path;

use anyhow::{Result, bail};

const MAGIC: &[u8; 4] = b"GGUF";
const MAX_KEYS: u64 = 1 << 20;
const MAX_STRING: u64 = 1 << 24;
const MAX_ARRAY: u64 = 1 << 26;
const MAX_MENU: u64 = 4_096;

const STREAM_SCALARS: [&str; 3] = [
    "stt.parakeet.encoder.subsampling_factor",
    "stt.frontend.hop_length",
    "stt.frontend.sample_rate",
];
const STREAM_MENUS: [&str; 2] = [
    "stt.parakeet.encoder.att_chunk_chunk_choices",
    "stt.parakeet.encoder.att_chunk_right_choices",
];

/// Faster live text target chunk frames (7 * 80 ms = 560 ms; lookahead 1.12 s vs 2.08 s default).
const FASTER_TARGET_FRAMES: i32 = 7;
const FASTER_MIN_FRAMES: i32 = 2;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct StreamHeader {
    pub u32s: BTreeMap<String, u32>,
    pub int32_arrays: BTreeMap<String, Vec<i32>>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum StreamMenus {
    Buffered {
        frame_ms: i32,
        chunk_frames: Vec<i32>,
        right_frames: Vec<i32>,
    },
    NotBuffered,
}

impl StreamMenus {
    pub fn read(path: &Path) -> Option<Self> {
        let header = read_stream_header(path).ok()?;
        Some(Self::from_header(&header))
    }

    fn from_header(header: &StreamHeader) -> Self {
        Self::buffered(header).unwrap_or(Self::NotBuffered)
    }

    fn buffered(header: &StreamHeader) -> Option<Self> {
        let scalar = |key: &str| header.u32s.get(key).copied().map(i64::from);
        let subsampling = scalar("stt.parakeet.encoder.subsampling_factor")?;
        let hop = scalar("stt.frontend.hop_length")?;
        let sample_rate = scalar("stt.frontend.sample_rate")?.max(1);
        let frame_ms = subsampling.checked_mul(hop)?.checked_mul(1_000)? / sample_rate;
        let frame_ms = i32::try_from(frame_ms).ok().filter(|&ms| ms > 0)?;
        let menu = |key: &str| {
            header
                .int32_arrays
                .get(key)
                .cloned()
                .filter(|frames| !frames.is_empty())
        };
        Some(Self::Buffered {
            frame_ms,
            chunk_frames: menu("stt.parakeet.encoder.att_chunk_chunk_choices")?,
            right_frames: menu("stt.parakeet.encoder.att_chunk_right_choices")?,
        })
    }

    /// Returns `(chunk_ms, right_ms)` for Faster Live Text if the model's
    /// buffered menu includes a supported low-latency tuple.
    pub fn faster_ms(&self) -> Option<(i32, i32)> {
        let Self::Buffered {
            frame_ms,
            chunk_frames,
            right_frames,
        } = self
        else {
            return None;
        };
        let chunk = chunk_frames
            .iter()
            .copied()
            .filter(|frames| (FASTER_MIN_FRAMES..=FASTER_TARGET_FRAMES).contains(frames))
            .max()?;
        let right = right_frames
            .iter()
            .copied()
            .filter(|frames| (0..=FASTER_TARGET_FRAMES).contains(frames))
            .max()?;
        Some((chunk * frame_ms, right * frame_ms))
    }
}

pub fn read_stream_header(path: &Path) -> Result<StreamHeader> {
    let file = File::open(path)?;
    let mut reader = BufReader::new(file);
    let mut magic = [0u8; 4];
    reader.read_exact(&mut magic)?;
    if &magic != MAGIC {
        bail!("not a GGUF file");
    }
    let version = read_u32(&mut reader)?;
    if !(2..=3).contains(&version) {
        bail!("unsupported GGUF version {version}");
    }
    let _tensor_count = read_u64(&mut reader)?;
    let kv_count = read_u64(&mut reader)?;
    if kv_count > MAX_KEYS {
        bail!("GGUF metadata key count {kv_count} exceeds limit");
    }

    let mut header = StreamHeader::default();
    for _ in 0..kv_count {
        let key = read_string(&mut reader)?;
        let value_type = read_u32(&mut reader)?;
        if STREAM_SCALARS.contains(&key.as_str()) {
            if let Some(value) = read_u32_like(&mut reader, value_type)? {
                header.u32s.insert(key, value);
            }
        } else if STREAM_MENUS.contains(&key.as_str()) && value_type == 9 {
            if let Some(values) = read_i32_array(&mut reader)? {
                header.int32_arrays.insert(key, values);
            }
        } else {
            skip_value(&mut reader, value_type)?;
        }
    }
    Ok(header)
}

fn read_u32(r: &mut impl Read) -> Result<u32> {
    let mut b = [0u8; 4];
    r.read_exact(&mut b)?;
    Ok(u32::from_le_bytes(b))
}

fn read_i32(r: &mut impl Read) -> Result<i32> {
    let mut b = [0u8; 4];
    r.read_exact(&mut b)?;
    Ok(i32::from_le_bytes(b))
}

fn read_u64(r: &mut impl Read) -> Result<u64> {
    let mut b = [0u8; 8];
    r.read_exact(&mut b)?;
    Ok(u64::from_le_bytes(b))
}

fn read_string(r: &mut impl Read) -> Result<String> {
    let len = read_u64(r)?;
    if len > MAX_STRING {
        bail!("GGUF string length {len} exceeds limit");
    }
    let mut buf = vec![0u8; len as usize];
    r.read_exact(&mut buf)?;
    Ok(String::from_utf8_lossy(&buf).into_owned())
}

fn read_u32_like<R: Read + Seek>(r: &mut R, value_type: u32) -> Result<Option<u32>> {
    match value_type {
        4 => Ok(Some(read_u32(r)?)),
        5 => Ok(u32::try_from(read_i32(r)?).ok()),
        10 => Ok(u32::try_from(read_u64(r)?).ok()),
        11 => {
            let mut b = [0u8; 8];
            r.read_exact(&mut b)?;
            Ok(u32::try_from(i64::from_le_bytes(b)).ok())
        }
        other => {
            skip_value(r, other)?;
            Ok(None)
        }
    }
}

fn read_i32_array<R: Read + Seek>(r: &mut R) -> Result<Option<Vec<i32>>> {
    let elem_type = read_u32(r)?;
    let count = read_u64(r)?;
    if count > MAX_ARRAY {
        bail!("GGUF array count {count} exceeds limit");
    }
    if elem_type == 5 && count <= MAX_MENU {
        let mut out = Vec::with_capacity(count as usize);
        for _ in 0..count {
            out.push(read_i32(r)?);
        }
        return Ok(Some(out));
    }
    if elem_type == 4 && count <= MAX_MENU {
        let mut out = Vec::with_capacity(count as usize);
        for _ in 0..count {
            out.push(read_u32(r)? as i32);
        }
        return Ok(Some(out));
    }
    skip_array_elems(r, elem_type, count)?;
    Ok(None)
}

fn scalar_size(value_type: u32) -> Option<u64> {
    match value_type {
        0 | 1 | 7 => Some(1),      // u8, i8, bool
        2 | 3 => Some(2),          // u16, i16
        4 | 5 | 6 => Some(4),      // u32, i32, f32
        10 | 11 | 12 => Some(8),   // u64, i64, f64
        _ => None,
    }
}

fn skip_value<R: Read + Seek>(r: &mut R, value_type: u32) -> Result<()> {
    if let Some(bytes) = scalar_size(value_type) {
        r.seek(SeekFrom::Current(bytes as i64))?;
        return Ok(());
    }
    match value_type {
        8 => {
            let len = read_u64(r)?;
            if len > MAX_STRING {
                bail!("GGUF string length {len} exceeds limit");
            }
            r.seek(SeekFrom::Current(len as i64))?;
        }
        9 => {
            let elem_type = read_u32(r)?;
            let count = read_u64(r)?;
            if count > MAX_ARRAY {
                bail!("GGUF array count {count} exceeds limit");
            }
            skip_array_elems(r, elem_type, count)?;
        }
        other => bail!("unknown GGUF value type {other}"),
    }
    Ok(())
}

fn skip_array_elems<R: Read + Seek>(r: &mut R, elem_type: u32, count: u64) -> Result<()> {
    if let Some(bytes) = scalar_size(elem_type) {
        let total = bytes
            .checked_mul(count)
            .ok_or_else(|| anyhow::anyhow!("GGUF array byte length overflow"))?;
        r.seek(SeekFrom::Current(total as i64))?;
        return Ok(());
    }
    for _ in 0..count {
        skip_value(r, elem_type)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn faster_ms_picks_balanced_low_latency_tuple() {
        let menus = StreamMenus::Buffered {
            frame_ms: 80,
            chunk_frames: vec![1, 2, 7, 13],
            right_frames: vec![0, 1, 7, 13],
        };
        assert_eq!(menus.faster_ms(), Some((560, 560)));
    }
}
