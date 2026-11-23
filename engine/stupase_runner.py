"""Download verified StuPASE weights or run its local, framed PCM worker."""

import os
from pathlib import Path
import struct
import sys

from engine_paths import WORKSPACE_ROOT


REVISION = "539963f425ae201ac8c379b6480e3b182a4840ad"
CHECKPOINTS = (
    ("DeWavLM-R.pt", 1261990714, "4eb21c9e6ab9bf51fe1c24a580e6f4c8f677ad4028e3b997e462e34f17c59ccf"),
    ("CFM.pt", 758024988, "df8e77fcc31554abb1b3481f1f4bc62688e75b9bd83081fb4d336b128f760f5e"),
    ("Vocoder_Mel-16k.pt", 242750990, "2d38b6adfb29129a4c78f7ed030aa75f1a922f71e0fc87f2e59f2c58d04ee13b"),
)
HEADER = struct.Struct("<I")
MAX_PCM_BYTES = 25 * 16000 * 4


def checkpoints_ready(root):
    try:
        return ((root / ".verified").read_text().strip() == REVISION
                and all((root / name).stat().st_size == size for name, size, _ in CHECKPOINTS))
    except OSError:
        return False


def download(root):
    if os.name == "nt":
        from windows_job import own_process_tree

        own_process_tree()
    sys.path.insert(0, str(WORKSPACE_ROOT / "desktop/build-tools"))
    from model_setup import fetch, install_lock, valid

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with install_lock(root / ".lock"):
        if checkpoints_ready(root):
            return
        for name, size, digest in CHECKPOINTS:
            item = {"file": name, "size": size, "sha256": digest,
                    "url": f"https://huggingface.co/cisco-ai/stupase/resolve/{REVISION}/{name}"}
            dest = root / name
            if not valid(dest, item):
                partial = root / (name + ".partial")
                if not valid(partial, item):
                    fetch(item, partial, lambda _size: None)
                if not valid(partial, item):
                    partial.unlink(missing_ok=True)
                    raise ValueError(f"Could not verify {name}.")
                partial.replace(dest)
        marker = root / ".verified.partial"
        marker.write_text(REVISION + "\n")
        marker.replace(root / ".verified")


def serve(root):
    import numpy as np
    import torch
    from stupase_model.stupase import StuPASE

    if not checkpoints_ready(root):
        raise ValueError("StuPASE checkpoints are unavailable.")
    output = sys.stdout.buffer
    sys.stdout = sys.stderr
    torch.set_num_threads(min(6, os.cpu_count() or 1))
    # CPU also supports the reference model's complex STFT and overlap-add operations.
    model = StuPASE(*(root / name for name, _, _ in CHECKPOINTS)).eval()
    while header := sys.stdin.buffer.read(HEADER.size):
        size, = HEADER.unpack(header)
        if size > MAX_PCM_BYTES or size % 4 or size == 0:
            raise ValueError("Invalid StuPASE input length.")
        pcm = sys.stdin.buffer.read(size)
        if len(pcm) != size:
            break
        samples = np.frombuffer(pcm, dtype="<f4")
        count = len(samples)
        padded = max(count, 1600)
        padded += (80 - padded) % 320
        source = torch.from_numpy(np.pad(samples, (0, padded - count))).unsqueeze(0)
        with torch.inference_mode():
            enhanced = model(source)[0, :count].numpy()
        peak = float(np.max(np.abs(enhanced)))
        if not np.isfinite(enhanced).all() or peak < 1e-8:
            output.write(HEADER.pack(0))
        else:
            enhanced *= float(np.max(np.abs(samples))) / peak
            output.write(HEADER.pack(size) + enhanced.astype("<f4", copy=False).tobytes())
        output.flush()


if __name__ == "__main__":
    command, folder = sys.argv[1:]
    if command == "download":
        download(Path(folder))
    elif command == "serve":
        serve(Path(folder))
