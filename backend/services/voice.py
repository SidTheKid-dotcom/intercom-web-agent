import io
import os
import re
import wave
from typing import List

from groq import Groq
from groq import BadRequestError

STT_MODEL = os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo")
TTS_MODEL = os.getenv("GROQ_TTS_MODEL", "canopylabs/orpheus-v1-english")
VALID_TTS_VOICES = {"autumn", "diana", "hannah", "austin", "daniel", "troy"}
TTS_VOICE = os.getenv("GROQ_TTS_VOICE", "autumn").lower()
TTS_MAX_CHARS = 180

_client = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    return _client


def transcribe_audio(audio_bytes: bytes, filename: str = "recording.webm") -> str:
    if not audio_bytes:
        raise ValueError("Empty audio payload")

    response = _get_client().audio.transcriptions.create(
        file=(filename, audio_bytes),
        model=STT_MODEL,
        response_format="text",
    )
    if isinstance(response, str):
        return response.strip()
    return getattr(response, "text", "").strip()


def _is_probably_english(text: str) -> bool:
    if not text.strip():
        return True
    ascii_letters = sum(1 for char in text if ("a" <= char.lower() <= "z") or char in " .,!?'-")
    return ascii_letters / max(len(text), 1) >= 0.65


def _split_for_tts(text: str) -> List[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []

    parts = re.split(r"(?<=[.!?])\s+", text)
    chunks: List[str] = []
    current = ""
    for part in parts:
        if not part:
            continue
        candidate = f"{current} {part}".strip() if current else part
        if len(candidate) <= TTS_MAX_CHARS:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(part) <= TTS_MAX_CHARS:
            current = part
            continue
        words = part.split(" ")
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip() if current else word
            if len(candidate) <= TTS_MAX_CHARS:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = word
        if current and len(current) > TTS_MAX_CHARS:
            chunks.append(current[:TTS_MAX_CHARS])
            current = current[TTS_MAX_CHARS:]
    if current:
        chunks.append(current)
    return chunks


def _merge_wav_segments(segments: List[bytes]) -> bytes:
    if not segments:
        return b""

    frames = []
    params = None
    for segment in segments:
        with wave.open(io.BytesIO(segment), "rb") as wav_file:
            if params is None:
                params = wav_file.getparams()
            frames.append(wav_file.readframes(wav_file.getnframes()))

    if params is None:
        return b""

    output = io.BytesIO()
    with wave.open(output, "wb") as wav_out:
        wav_out.setparams(params)
        for frame_chunk in frames:
            wav_out.writeframes(frame_chunk)
    return output.getvalue()


def _safe_join_segments(segments: List[bytes]) -> bytes:
    if not segments:
        return b""
    if len(segments) == 1:
        return segments[0]
    try:
        return _merge_wav_segments(segments)
    except Exception:
        # Some provider responses are not consistently mergeable as PCM WAV.
        # Falling back to the first segment keeps reply playback working.
        return segments[0]


def synthesize_speech(text: str) -> bytes:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        raise ValueError("Empty text")
    if not _is_probably_english(cleaned):
        raise ValueError("TTS currently supports English replies only.")

    voice = TTS_VOICE if TTS_VOICE in VALID_TTS_VOICES else "autumn"
    segments = []
    for chunk in _split_for_tts(cleaned):
        try:
            response = _get_client().audio.speech.create(
                model=TTS_MODEL,
                voice=voice,
                input=chunk,
                response_format="wav",
            )
        except BadRequestError as exc:
            message = str(exc)
            if "model_terms_required" in message or "requires terms acceptance" in message:
                raise ValueError(
                    "Groq TTS is not enabled for this account yet. Accept the Orpheus model terms in the Groq console or disable reply playback."
                )
            raise
        segments.append(bytes(response.read()))

    return _safe_join_segments(segments)
