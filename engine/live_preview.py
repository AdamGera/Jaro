"""Shared pause-aware live preview window planner for Sessions and Meetings.

Keeps live decodes tightly bounded (<= 8 s stable, <= 6 s tentative) so hotkey
dictation stays responsive, commits on natural pauses to avoid chopping words,
and guarantees exact 20 ms frame alignment (lossless 3:1 mapping between 48 kHz
and 16 kHz).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import re
from typing import Callable
from unicodedata import normalize

import numpy as np

from audio_file import quiet_sample_boundary


FRAME_MS = 20
MIN_TENTATIVE_SECONDS = 1.6
TENTATIVE_STEP_SECONDS = 1.2
MAX_TENTATIVE_SECONDS = 6.0
MIN_COMMIT_SECONDS = 2.5
PAUSE_COMMIT_MS = 450
LONG_UTTERANCE_SECONDS = 6.0
LONG_PAUSE_COMMIT_MS = 280
MAX_COMMIT_SECONDS = 8.0
SILENCE_ADVANCE_SECONDS = 4.0
MAX_BACKLOG_SECONDS = 16.0

SPEECH_MARGIN_DB = 12.0
MINIMUM_SPEECH_DB = -55.0
MAX_INITIAL_FLOOR_DB = -48.0
LOWEST_FLOOR_DB = -90.0
FLOOR_RISE_DB_PER_FRAME = 0.06

_WORD_TOKEN = re.compile(r"[^\s]+")
_NORM_CHARS = re.compile(r"[^\W_]+")


@dataclass(frozen=True)
class WaitAction:
    kind: str = "wait"


@dataclass(frozen=True)
class TentativeAction:
    start: int
    frames: int
    kind: str = "tentative"


@dataclass(frozen=True)
class CommitAction:
    start: int
    frames: int
    forced: bool = False
    kind: str = "commit"


@dataclass(frozen=True)
class SkipAction:
    cursor: int
    kind: str = "skip"


LiveAction = WaitAction | TentativeAction | CommitAction | SkipAction


async def finish_live_preview(
    preview: asyncio.Task, timeout: float, logger: logging.Logger, recording_id: object,
) -> None:
    """Give preview a short grace period while keeping cleanup off the save path."""
    def log_failure(task: asyncio.Task) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("Live preview failed while stopping recording %s.", recording_id, exc_info=error)

    try:
        await asyncio.wait_for(asyncio.shield(preview), timeout)
    except (TimeoutError, asyncio.CancelledError):
        preview.cancel()
        preview.add_done_callback(log_failure)
        if asyncio.current_task().cancelling():
            raise
    except Exception:
        log_failure(preview)


def read_wav_i16_slice(path: Path, start_frame: int, frames: int) -> bytes | None:
    """Read mono 16-bit PCM frames from a fixed 44-byte WAV header."""
    if start_frame < 0 or frames <= 0:
        return None
    expected = frames * 2
    try:
        with path.open("rb") as handle:
            handle.seek(44 + start_frame * 2)
            data = handle.read(expected)
    except OSError:
        return None
    return data if len(data) == expected else None


def analyze_pcm16_window(pcm_i16: bytes, rate: int) -> tuple[bool, int | None]:
    """Detect whether speech occurred and whether a natural pause closed a commit window.

    Returns `(heard_speech, pause_cut_frames)` where `pause_cut_frames` is snapped
    to a 20 ms frame boundary (always a multiple of 3 at 48 kHz).
    """
    frame_size = rate // (1000 // FRAME_MS)
    total_samples = len(pcm_i16) // 2
    num_frames = total_samples // frame_size
    if num_frames <= 0:
        return False, None

    samples = np.frombuffer(pcm_i16, dtype="<i2", count=num_frames * frame_size).astype(np.float32) * np.float32(1.0 / 32768.0)
    blocks = samples.reshape(num_frames, frame_size)
    centered = blocks - blocks.mean(axis=1, keepdims=True)
    energy = np.mean(centered * centered, axis=1)
    levels = 10.0 * np.log10(np.maximum(energy, 1e-18))

    min_commit_frames = round(MIN_COMMIT_SECONDS * 1000 / FRAME_MS)
    long_frames = round(LONG_UTTERANCE_SECONDS * 1000 / FRAME_MS)
    pause_frames = max(1, round(PAUSE_COMMIT_MS / FRAME_MS))
    long_pause_frames = max(1, round(LONG_PAUSE_COMMIT_MS / FRAME_MS))

    finite = levels[np.isfinite(levels)]
    floor = max(LOWEST_FLOOR_DB, min(MAX_INITIAL_FLOOR_DB, float(np.percentile(finite, 10)))) if finite.size else LOWEST_FLOOR_DB
    heard_speech = False
    silent_run = 0

    for idx, level in enumerate(levels):
        lvl = float(level)
        if not np.isfinite(lvl):
            lvl = 0.0
        if lvl >= floor:
            floor = min(MAX_INITIAL_FLOOR_DB, floor + FLOOR_RISE_DB_PER_FRAME)
        else:
            floor = max(LOWEST_FLOOR_DB, lvl)
        is_speech = lvl > max(MINIMUM_SPEECH_DB, floor + SPEECH_MARGIN_DB)
        if is_speech:
            heard_speech = True
            silent_run = 0
            continue
        silent_run += 1
        elapsed_frames = idx + 1
        needed_pause = long_pause_frames if elapsed_frames >= long_frames else pause_frames
        if heard_speech and elapsed_frames >= min_commit_frames and silent_run >= needed_pause:
            cut_idx = elapsed_frames - silent_run // 2
            return True, cut_idx * frame_size

    return heard_speech, None


def _normalize_word(word: str) -> str:
    parts = _NORM_CHARS.findall(normalize("NFKC", word).casefold())
    return "".join(parts)


def trim_boundary_overlap(previous_text: str, next_text: str, *, max_words: int = 3) -> str:
    """Drop 1..max_words duplicated at a forced live window boundary."""
    prev_tokens = _WORD_TOKEN.findall(previous_text)
    if not prev_tokens or not next_text.strip():
        return next_text
    matches = list(_WORD_TOKEN.finditer(next_text))
    if not matches:
        return next_text

    prev_norm = [_normalize_word(tok) for tok in prev_tokens[-max_words:]]
    next_norm = [_normalize_word(m.group(0)) for m in matches[:max_words]]
    limit = min(len(prev_norm), len(next_norm), max_words)

    for overlap in range(limit, 0, -1):
        suffix = prev_norm[-overlap:]
        prefix = next_norm[:overlap]
        if all(suffix) and suffix == prefix:
            if overlap >= len(matches):
                return ""
            return next_text[matches[overlap].start() :].lstrip()
    return next_text


class LiveWindowPlanner:
    """Decides when to emit fast tentative previews, commit stable windows, or skip backlog."""

    def __init__(self, rate: int):
        if rate <= 0 or rate % (1000 // FRAME_MS) != 0:
            raise ValueError("Sample rate must divide cleanly into 20 ms frames.")
        self.rate = rate
        self.frame_unit = rate // (1000 // FRAME_MS)
        self.min_tentative = self._snap(MIN_TENTATIVE_SECONDS)
        self.tentative_step = self._snap(TENTATIVE_STEP_SECONDS)
        self.max_tentative = self._snap(MAX_TENTATIVE_SECONDS)
        self.min_commit = self._snap(MIN_COMMIT_SECONDS)
        self.max_commit = self._snap(MAX_COMMIT_SECONDS)
        self.silence_advance = self._snap(SILENCE_ADVANCE_SECONDS)
        self.max_backlog = self._snap(MAX_BACKLOG_SECONDS)
        self.cursor = 0
        self.last_tentative_end = 0

    def _snap(self, seconds: float) -> int:
        return max(self.frame_unit, round(seconds * self.rate / self.frame_unit) * self.frame_unit)

    def _snap_down(self, frames: int) -> int:
        return (max(0, frames) // self.frame_unit) * self.frame_unit

    def mark_committed(self, end_frame: int) -> None:
        self.cursor = max(self.cursor, end_frame)
        self.last_tentative_end = self.cursor

    def mark_tentative(self, end_frame: int) -> None:
        self.last_tentative_end = max(self.last_tentative_end, end_frame)

    def jump_to(self, frame: int) -> None:
        snapped = self._snap_down(frame)
        self.cursor = snapped
        self.last_tentative_end = snapped

    def plan(
        self,
        available_frames: int,
        read_pcm_i16: Callable[[int, int], bytes | None],
        *,
        show_live: bool,
        cut_frame: int | None = None,
        under_load: bool = False,
    ) -> LiveAction:
        if cut_frame is not None and cut_frame > self.cursor:
            needed = min(cut_frame - self.cursor, self.max_commit)
            if available_frames - self.cursor < needed:
                return WaitAction()
            return CommitAction(self.cursor, needed, forced=False)

        available = self._snap_down(available_frames)
        pending = available - self.cursor
        if pending <= 0:
            return WaitAction()

        if pending > self.max_backlog:
            target = max(self.cursor, available - self.silence_advance)
            if target > self.cursor:
                return SkipAction(target)

        if pending < self.min_tentative:
            return WaitAction()

        window = min(pending, self.max_commit)
        pcm = read_pcm_i16(self.cursor, window)
        if pcm is None:
            return WaitAction()

        heard_speech, pause_cut = analyze_pcm16_window(pcm, self.rate)

        if pause_cut is not None and pause_cut >= self.min_commit:
            return CommitAction(self.cursor, pause_cut, forced=False)

        if pending >= self.max_commit:
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) * np.float32(1.0 / 32768.0)
            raw_cut = quiet_sample_boundary(samples)
            cut = self._snap_down(raw_cut)
            cut = max(self.min_commit, min(window, cut))
            return CommitAction(self.cursor, cut, forced=heard_speech)

        if not heard_speech and pending >= self.silence_advance:
            return CommitAction(self.cursor, self.silence_advance, forced=False)

        if (
            show_live
            and not under_load
            and heard_speech
            and self.min_tentative <= pending <= self.max_tentative
            and available - self.last_tentative_end >= self.tentative_step
        ):
            return TentativeAction(self.cursor, pending)

        return WaitAction()
