from collections import deque

import numpy as np
import torch
from transformers import AutoModelForAudioFrameClassification, AutoProcessor
from transformers.models.nemotron3_diarization.modeling_nemotron3_diarization import (
    Nemotron3DiarizationSpeakerCache,
)

from rumble_model import DIARIZATION_DIR, SAMPLE_RATE
from speaker_alignment import word_records


class Diarizer:
    def __init__(self, device):
        self.model = AutoModelForAudioFrameClassification.from_pretrained(
            DIARIZATION_DIR, local_files_only=True,
        ).to(device).eval()
        self.live = AutoProcessor.from_pretrained(DIARIZATION_DIR, local_files_only=True)
        self.final = AutoProcessor.from_pretrained(DIARIZATION_DIR, local_files_only=True)
        config = self.model.config
        self.final.streaming_modes["offline"] = (config.chunk_length, config.chunk_right_context)
        self.final.set_streaming_mode("offline")

    def session(self, live=False):
        processor = self.live if live else self.final
        cache = None
        if not live:
            config = self.model.config
            cache = Nemotron3DiarizationSpeakerCache(
                config.streaming_config,
                fifo_length=config.fifo_length,
                speaker_cache_update_period=config.speaker_cache_update_period,
            )
        return SpeakerSession(self.model, processor, cache)


class SpeakerSession:
    def __init__(self, model, processor, cache):
        self.model = model
        self.processor = processor
        self.cache = cache
        self.buffer = np.empty(0, dtype=np.float32)
        self.received = 0
        self.mel_frame = 0
        self.origin_frame = 0
        self.frame_duration = processor.feature_extractor.hop_length / SAMPLE_RATE
        self.blocks = deque()
        self.offset = -1
        self.previous_speaker = None
        self.last_speaker = None

    @torch.inference_mode()
    def push(self, pcm, offset=None, *, final=False):
        audio = np.frombuffer(pcm, dtype="<f4")
        if offset is not None:
            if offset > self.received:
                # Live was off during the gap. Keep the voice cache and start a new
                # acoustic window at the current recording time.
                self.buffer = np.empty(0, dtype=np.float32)
                self.blocks.clear()
                self.mel_frame = 0
                self.origin_frame = round(offset / SAMPLE_RATE / self.frame_duration)
                self.received = offset
            audio = audio[self.received - offset:]
        self.received += len(audio)
        self.buffer = np.concatenate((self.buffer, audio))
        processor = self.processor
        while True:
            first = self.mel_frame == 0
            size = processor.num_samples_first_audio_chunk if first else processor.num_samples_per_audio_chunk
            if len(self.buffer) < size:
                break
            self._infer(self.buffer[:size], first=first, final=False)
            next_frame = self.mel_frame + processor.num_mel_frames_per_step
            start = 0 if first else processor.audio_chunk_start(self.mel_frame)
            self.buffer = self.buffer[processor.audio_chunk_start(next_frame) - start:]
            self.mel_frame = next_frame
        if final and self.buffer.size:
            minimum = processor.feature_extractor.n_fft
            audio = np.pad(self.buffer, (0, max(0, minimum - len(self.buffer))))
            self._infer(audio, first=self.mel_frame == 0, final=True)
            self.buffer = np.empty(0, dtype=np.float32)

        cutoff = (self.received / SAMPLE_RATE - 100) / self.frame_duration
        while self.blocks and self.blocks[0][0] + len(self.blocks[0][1]) <= cutoff:
            self.blocks.popleft()

    def _infer(self, audio, *, first, final):
        inputs = self.processor(
            audio.copy(), sampling_rate=SAMPLE_RATE, is_streaming=True,
            is_first_audio_chunk=first, is_last_audio_chunk=final,
        ).to(self.model.device, dtype=self.model.dtype)
        output = self.model(**inputs, speaker_cache=self.cache)
        self.cache = output.speaker_cache
        self.blocks.append((self.origin_frame + self.mel_frame, output.logits[0].sigmoid().float().cpu().numpy()))

    def covers(self, end_sample):
        if not self.blocks:
            return False
        start, activity = self.blocks[-1]
        return round((start + len(activity)) * self.frame_duration * SAMPLE_RATE) >= end_sample

    def intervals(self, offset, duration):
        """Threshold each speaker independently so simultaneous voices survive."""
        start = offset / SAMPLE_RATE
        end = start + duration
        turns = []
        for frame, activity in self.blocks:
            left = max(0, int(np.floor(start / self.frame_duration)) - frame)
            right = min(len(activity), int(np.ceil(end / self.frame_duration)) - frame)
            if right <= left:
                continue
            mask = activity[left:right] > 0.5
            edges = np.diff(np.pad(mask, ((1, 1), (0, 0))), axis=0)
            for channel in range(mask.shape[1]):
                for onset, stop in np.flatnonzero(edges[:, channel]).reshape(-1, 2):
                    first = max(start, (frame + left + int(onset)) * self.frame_duration)
                    last = min(end, (frame + left + int(stop)) * self.frame_duration)
                    if last > first:
                        turns.append({"speaker": channel + 1, "start": round(first, 4), "end": round(last, 4)})
        return turns

    def _speaker(self, start, end, previous=None):
        first = max(0, round(start / self.frame_duration))
        last = max(first + 1, round(end / self.frame_duration))
        scores = np.zeros(8, dtype=np.float32)
        for block_start, activity in self.blocks:
            left = max(0, first - block_start)
            right = min(len(activity), last - block_start)
            if right > left:
                frames = activity[left:right]
                scores += np.where(frames > 0.5, frames, 0).sum(axis=0)
        if not scores.any():
            return None
        best = int(scores.argmax())
        if previous and scores[previous - 1] >= scores[best] * 0.9:
            return previous
        return best + 1

    def label(self, text, words, offset, duration, *, include_word_times=False):
        if not text:
            return [], []
        if offset > self.offset:
            self.previous_speaker = self.last_speaker
            self.offset = offset

        start = offset / SAMPLE_RATE
        records = word_records(text, words, start, duration)
        previous = self.previous_speaker
        timed_speakers = []
        for word in records:
            speaker = self._speaker(word["start"], word["end"], previous) if word["start"] is not None else None
            timed_speakers.append(speaker)
            previous = speaker or previous

        first_timed = next((item for item in timed_speakers if item), None)
        if first_timed:
            speaker = self.previous_speaker or first_timed
        else:
            # With no word anchors, the audio can still identify the voice in this block.
            speaker = self._speaker(start, start + duration, self.previous_speaker) or self.previous_speaker or 1
        pieces = []
        for word, timed_speaker in zip(records, timed_speakers, strict=True):
            next_speaker = timed_speaker or speaker
            word_start, word_end = word["start"], word["end"]
            if not pieces or next_speaker != speaker:
                pieces.append({
                    "speaker": next_speaker,
                    "start": word_start if word_start is not None else round(start, 3),
                    "end": None,
                    "text": "",
                })
            pieces[-1]["text"] += word["text"] + word["separator"]
            if word_end is not None:
                pieces[-1]["end"] = word_end
            word["speaker"] = next_speaker
            speaker = next_speaker
        self.last_speaker = speaker
        for piece in pieces:
            piece["text"] = piece["text"].strip()
        return pieces, records if include_word_times else []
