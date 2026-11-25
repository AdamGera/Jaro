import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from unicodedata import normalize

from engine_paths import WORKSPACE_ROOT, library_path

MODEL_ID = "Reson8/Rumble-2-Flash"
REVISION = "92ec9141fb1178c5e77fd0020fe86a33c355060b"
MODEL_DIR = Path(os.environ.get("JARD_MODEL_DIR") or
                 (WORKSPACE_ROOT if sys.platform == "darwin" else library_path()) / "model")
SAMPLE_RATE = 16_000
MAX_SAMPLES = 25 * SAMPLE_RATE
DIARIZATION_ID = "nvidia/Nemotron-3-Diarization"
DIARIZATION_REVISION = "52a432672732425e4c19765b9fec9b5810e6f4ba"
DIARIZATION_DIR = MODEL_DIR / "nemotron3"


def vocabulary_echo(text, vocabulary):
    if len(vocabulary) < 2:
        return False

    def normalized_words(value):
        return re.findall(r"[^\W_]+", normalize("NFKC", value).casefold())

    words = normalized_words(text)
    return bool(words) and words == normalized_words(" ".join(vocabulary))

# The model card's languages, plus Hebrew verified with this checkpoint.
LANGUAGES = {
    "af": "Afrikaans",
    "sq": "Albanian",
    "ar": "Arabic",
    "eu": "Basque",
    "bn": "Bengali",
    "bs": "Bosnian",
    "yue": "Cantonese",
    "ca": "Catalan",
    "ceb": "Cebuano",
    "zh": "Chinese",
    "hr": "Croatian",
    "cs": "Czech",
    "da": "Danish",
    "nl": "Dutch",
    "en": "English",
    "fil": "Filipino",
    "fi": "Finnish",
    "fr": "French",
    "de": "German",
    "el": "Greek",
    "he": "Hebrew",
    "hi": "Hindi",
    "hu": "Hungarian",
    "is": "Icelandic",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "lv": "Latvian",
    "lt": "Lithuanian",
    "mk": "Macedonian",
    "ms": "Malay",
    "mt": "Maltese",
    "nb": "Norwegian Bokmål",
    "fa": "Persian",
    "pl": "Polish",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "sr": "Serbian",
    "es": "Spanish",
    "sv": "Swedish",
    "th": "Thai",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "vi": "Vietnamese",
}

MODEL_FILES = (
    "LICENSE",
    "README.md",
    "added_tokens.json",
    "chat_template.jinja",
    "config.json",
    "generation_config.json",
    "merges.txt",
    "model.safetensors",
    "preprocessor_config.json",
    "processor_config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
)


def prepare_assets(repo, revision, directory, names):
    from huggingface_hub import hf_hub_download, try_to_load_from_cache

    for name in names:
        target = directory / name
        if target.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        cached = try_to_load_from_cache(repo, name, revision=revision)
        if not isinstance(cached, str):
            cached = hf_hub_download(repo, name, revision=revision)
        temporary = target.with_suffix(target.suffix + ".part")
        shutil.copyfile(cached, temporary)
        temporary.replace(target)


def prepare_model():
    prepare_assets(MODEL_ID, REVISION, MODEL_DIR, MODEL_FILES)
    (MODEL_DIR / "revision").write_text(REVISION + "\n")
    prepare_assets(MODEL_ID, REVISION, MODEL_DIR, ("ctc/ctc_head.safetensors", "ctc/ctc_vocab.json"))
    prepare_assets(
        DIARIZATION_ID, DIARIZATION_REVISION, DIARIZATION_DIR,
        ("README.md", "config.json", "processor_config.json", "model.safetensors"),
    )


def language_prompt(language_codes: tuple[str, ...]) -> str | None:
    if not language_codes:
        return None
    # A processor prompt adds context to one clip. Its language option forces a
    # single output language per clip, even when a batch passes a list.
    names = ", ".join(LANGUAGES[code] for code in language_codes)
    return f"\nExpected spoken languages: {names}. Transcribe speech in its original language."


def speech_backend(torch):
    device = os.environ.get("JARD_INFERENCE_DEVICE", "mps" if sys.platform == "darwin" else "cpu")
    if device not in ("mps", "cpu"):
        raise ValueError("JARD_INFERENCE_DEVICE must be mps or cpu.")
    if device == "mps" and not torch.backends.mps.is_available():
        raise RuntimeError("Metal speech inference requires PyTorch MPS on an Apple Silicon Mac.")

    precision = os.environ.get("JARD_INFERENCE_DTYPE", "float32" if device == "cpu" else "bfloat16")
    if precision not in ("float32", "bfloat16"):
        raise ValueError("JARD_INFERENCE_DTYPE must be float32 or bfloat16.")
    dtype = torch.float32 if precision == "float32" else torch.bfloat16
    return device, dtype


class RumbleModel:
    def __init__(self):
        if not all((MODEL_DIR / name).is_file() for name in MODEL_FILES):
            raise RuntimeError("Run Setup Jaro to install the local model.")
        if (MODEL_DIR / "revision").read_text().strip() != REVISION:
            raise RuntimeError("The local checkpoint revision does not match.")

        import numpy as np
        import torch
        from transformers import AutoModelForMultimodalLM, AutoProcessor

        self.device, dtype = speech_backend(torch)
        self.np = np
        self.torch = torch
        self.processor = AutoProcessor.from_pretrained(MODEL_DIR, local_files_only=True)
        self.model = AutoModelForMultimodalLM.from_pretrained(
            MODEL_DIR,
            local_files_only=True,
            dtype=dtype,
            attn_implementation="eager",
        ).to(self.device).eval()
        self.aligner = None
        self.reported_screen_failure = False
        warmup = np.zeros(SAMPLE_RATE * 2, dtype=np.float32)
        try:
            self._decode(warmup, max_new_tokens=4, cache_implementation="static")
        except RuntimeError as error:
            if "out of memory" not in str(error).lower():
                raise
            warmup_oom = True
        else:
            warmup_oom = False
        if warmup_oom:
            self.release_device_cache()
            self._decode(warmup, max_new_tokens=4)

    def release_device_cache(self):
        if self.device == "mps":
            self.torch.mps.empty_cache()

    def enable_alignment(self):
        from speaker_alignment import CtcAligner

        self.aligner = CtcAligner(MODEL_DIR / "ctc", self.model.model.audio_tower, self.processor.tokenizer)
        try:
            self.aligner.warm_screen()
        except (RuntimeError, ValueError):
            logging.getLogger("uvicorn.error").warning("Could not warm the native dictation speech screen.")

    def transcribe(
        self, pcm: bytes, language_codes: tuple[str, ...] = (), *, timed: bool = False,
        cache_implementation: str | None = None, vocabulary: tuple[str, ...] = (), screen_nonspeech: bool = False,
        speech_prefix_samples: int | None = None,
    ):
        audio = self.np.frombuffer(pcm, dtype="<f4").copy()
        if not self.np.isfinite(audio).all():
            raise ValueError("Audio contains invalid samples.")
        if self.np.max(self.np.abs(audio)) < 0.001:
            return "", None, []
        base_prompt = language_prompt(language_codes)
        prompt = base_prompt
        if vocabulary:
            terms = json.dumps(vocabulary, ensure_ascii=False)
            prompt = (prompt or "") + f"\nPossible names and terms in this audio: {terms}. Use them only when spoken."
        def decode(prompt):
            return self._decode(audio, prompt=prompt, cache_implementation=cache_implementation,
                                screen_nonspeech=screen_nonspeech, speech_prefix_samples=speech_prefix_samples)

        if timed and self.aligner:
            with self.aligner.capture() as states:
                text, language = decode(prompt)
            if vocabulary_echo(text, vocabulary):
                with self.aligner.capture() as states:
                    text, language = decode(base_prompt)
            try:
                words = self.aligner.words(text, states[0] if states else None)
            except (RuntimeError, ValueError, ArithmeticError):
                logging.getLogger("uvicorn.error").warning("Word timing was unavailable for an audio block.")
                words = []
            return text, language, words
        text, language = decode(prompt)
        if vocabulary_echo(text, vocabulary):
            text, language = decode(base_prompt)
        return text, language, []

    def _audio_features(self, inputs):
        return self.model.get_audio_features(inputs["input_features"], inputs["input_features_mask"], return_dict=True)

    def _screened_inputs(self, inputs):
        audio = self._audio_features(inputs)
        if self.aligner.confidently_blank(audio.last_hidden_state):
            return None
        input_ids = inputs["input_ids"]
        embeddings = self.model.get_input_embeddings()(input_ids)
        mask = self.model.model.get_placeholder_mask(input_ids, embeddings, audio.pooler_output)
        return {
            "input_ids": input_ids,
            "attention_mask": inputs["attention_mask"],
            "inputs_embeds": embeddings.masked_scatter(mask, audio.pooler_output.to(embeddings)),
        }

    def _transcription_inputs(self, audio, prompt=None):
        inputs = self.processor.apply_transcription_request(audio=audio, prompt=prompt)
        return inputs.to(device=self.model.device, dtype=self.model.dtype)

    def _decode(
        self, audio, max_new_tokens: int = 512, prompt: str | None = None,
        cache_implementation: str | None = None, *, screen_nonspeech: bool = False,
        speech_prefix_samples: int | None = None,
    ) -> tuple[str, str | None]:
        with self.torch.inference_mode():
            screen = screen_nonspeech and self.aligner is not None
            screen_prefix = screen and speech_prefix_samples is not None
            if screen_prefix:
                tail = audio[speech_prefix_samples:]
                if len(tail) == 0 or abs(tail).max() < 0.001:
                    screen_prefix = False
            # A confidently blank prefix makes the full take unnecessary.
            inputs = None if screen_prefix else self._transcription_inputs(audio, prompt)
            if screen:
                try:
                    if screen_prefix:
                        # The encoder sees its entire input. Give the screen a separate
                        # prefix so later speech cannot influence its acoustic features.
                        prefix = self._transcription_inputs(audio[:speech_prefix_samples])
                        signal = self._audio_features(prefix)
                        if self.aligner.confidently_blank(signal.last_hidden_state):
                            return "", None
                    else:
                        prepared = self._screened_inputs(inputs)
                        if prepared is None:
                            return "", None
                        inputs = prepared
                except (RuntimeError, ValueError, IndexError, KeyError):
                    if not self.reported_screen_failure:
                        self.reported_screen_failure = True
                        logging.getLogger("uvicorn.error").warning("The dictation speech screen could not check audio. Continuing transcription.")
            if inputs is None:
                inputs = self._transcription_inputs(audio, prompt)
            output = self.model.generate(
                **inputs, max_new_tokens=max_new_tokens, cache_implementation=cache_implementation,
            )
            generated = output[:, inputs["input_ids"].shape[-1]:]
            result = self.processor.decode(generated, return_format="parsed")[0]
        return result["transcription"].strip(), result["language"]
