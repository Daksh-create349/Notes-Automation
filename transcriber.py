"""Transcriber — splits large audio into chunks via ffmpeg, transcribes via Groq Whisper.

Groq enforces a ~25 MB / request limit. Long lectures easily exceed this, so we:
  1. Probe the audio duration using ffprobe.
  2. Split it into CHUNK_MINUTES-minute segments using ffmpeg (no Python audio libs needed).
  3. Transcribe each chunk individually via Groq Whisper.
  4. Join all partial transcripts and return the full text.

Requires: ffmpeg installed on the system (brew install ffmpeg on macOS).
"""

import os
import subprocess
import tempfile
from pathlib import Path

from groq import Groq

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
CHUNK_MINUTES = 10          # Length of each chunk sent to Groq
CHUNK_SECONDS = CHUNK_MINUTES * 60


def _get_duration_seconds(file_path: str) -> float:
    """Return the duration of an audio file in seconds using ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            file_path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def _split_audio(file_path: str, tmp_dir: str) -> list[str]:
    """Split audio into CHUNK_SECONDS-long WAV files using ffmpeg.
    Returns a sorted list of chunk file paths."""
    duration = _get_duration_seconds(file_path)
    chunks = []
    start = 0
    index = 0

    while start < duration:
        chunk_path = str(Path(tmp_dir) / f"chunk_{index:03d}.wav")
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", str(start),
                "-t", str(CHUNK_SECONDS),
                "-i", file_path,
                "-ar", "16000",    # 16 kHz — good for speech
                "-ac", "1",        # mono
                chunk_path,
            ],
            capture_output=True,
            check=True,
        )
        chunks.append(chunk_path)
        start += CHUNK_SECONDS
        index += 1

    return chunks


import time


def _transcribe_single(client: Groq, file_path: str, max_retries: int = 4) -> str | None:
    """Translate and transcribe audio directly to English via Groq Whisper."""
    for attempt in range(max_retries):
        try:
            with open(file_path, "rb") as f:
                # Use translations.create to translate foreign speech (Hindi/Urdu/Hinglish) directly to English
                result = client.audio.translations.create(
                    file=(Path(file_path).name, f),
                    model="whisper-large-v3",
                )
            return result.text
        except Exception as e:
            err = str(e)
            if attempt < max_retries - 1:
                wait = 10 * (2 ** attempt)  # 10s, 20s, 40s
                print(f"  Transcription retry {attempt + 1}/{max_retries} in {wait}s due to: {err[:80]}...")
                time.sleep(wait)
            else:
                print(f"  Chunk transcription error: {e}")
                return None
    return None


def transcribe_audio(file_path: str) -> str | None:
    """Transcribe an audio file of any length by chunking it with ffmpeg."""
    try:
        client = Groq(api_key=os.environ["GROQ_API_KEY"])
        duration = _get_duration_seconds(file_path)

        # Short file — transcribe directly without chunking
        if duration <= CHUNK_SECONDS:
            return _transcribe_single(client, file_path)

        print(f"  Audio is {duration / 60:.1f} min — splitting into {CHUNK_MINUTES}-min chunks...")

        parts: list[str] = []
        with tempfile.TemporaryDirectory() as tmp_dir:
            chunks = _split_audio(file_path, tmp_dir)
            for i, chunk_path in enumerate(chunks):
                start_min = i * CHUNK_MINUTES
                end_min = min((i + 1) * CHUNK_MINUTES, duration / 60)
                print(f"  Transcribing chunk {i + 1}/{len(chunks)} "
                      f"({start_min:.0f}–{end_min:.0f} min)...")
                text = _transcribe_single(client, chunk_path)
                if text:
                    parts.append(text.strip())

        return " ".join(parts) if parts else None

    except Exception as e:
        print(f"Transcription error: {e}")
        return None
