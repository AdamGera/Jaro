"""Bounded local decoding for command-line file transcription."""
from io import BytesIO
import stat
import subprocess
import wave

import numpy as np

from audio_file import FILE_ERROR, INPUT_OPTIONS, codec_binary, quiet_sample_boundary
from rumble_model import MAX_SAMPLES, SAMPLE_RATE

SAMPLE_BYTES = 2
MAX_PIECE_BYTES = MAX_SAMPLES * SAMPLE_BYTES


def validate_file(path):
    with path.open("rb") as source:
        header = source.read(12)
    if header[:4] == b"RIFF" and header[8:] == b"WAVE":
        riff_size = int.from_bytes(header[4:8], "little")
        if riff_size != 0xFFFFFFFF and 8 + riff_size > path.stat().st_size:
            raise ValueError(FILE_ERROR)


def quiet_cut(pcm):
    return quiet_sample_boundary(np.frombuffer(pcm, dtype="<i2")) * SAMPLE_BYTES


def audio_pieces(path):
    path = path.resolve(strict=True)
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(FILE_ERROR)
    validate_file(path)
    with subprocess.Popen([
        codec_binary("ffmpeg"), "-v", "error", "-nostdin", "-xerror", *INPUT_OPTIONS, "-i", str(path),
        "-map", "0:a:0", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1",
    ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as decoder:
        limit = MAX_PIECE_BYTES + SAMPLE_BYTES
        try:
            pending = decoder.stdout.read(limit)
            while len(pending) > MAX_PIECE_BYTES:
                cut = quiet_cut(pending[:MAX_PIECE_BYTES])
                yield pending[:cut]
                pending = pending[cut:] + decoder.stdout.read(limit - len(pending) + cut)
            if decoder.wait() or not pending or len(pending) % SAMPLE_BYTES:
                raise ValueError(FILE_ERROR)
            yield pending
        finally:
            if decoder.poll() is None:
                decoder.kill()


def has_audible_samples(pcm):
    samples = np.frombuffer(pcm, dtype="<i2")
    return int(samples.max()) - int(samples.min()) > 2


def wav_piece(pcm):
    output = BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setparams((1, SAMPLE_BYTES, SAMPLE_RATE, 0, "NONE", ""))
        writer.writeframes(pcm)
    return output.getvalue()
