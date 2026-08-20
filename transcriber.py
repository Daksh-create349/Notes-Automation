"""Transcriber — 5-minute silence-aligned audio chunking & Whisper Turbo transcription.

Features:
  - Strict 5-minute audio chunks (AUDIO_CHUNK_MINUTES=5)
  - Silence-aware boundaries (pauses >= 0.5s) with 6-second overlap buffer
  - Granular per-chunk JSON caching in transcript_store (0001.json, 0002.json)
  - Immediate on-the-fly chunk WAV deletion to conserve disk space
  - Quota-aware rate-limit pacing and automatic model fallback
  - Skip-if-exists resume support for long (2-8+ hour) lectures
"""

import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from groq import Groq

from audio_splitter import (
    ChunkBoundary,
    extract_single_chunk,
    get_duration_seconds,
    plan_audio_chunks,
    sanitize_error,
)
from quota_manager import call_with_retry_and_quota, global_quota
from transcript_store import TranscriptChunk, TranscriptStore


DEFAULT_AUDIO_CHUNK_MINUTES = int(os.getenv("AUDIO_CHUNK_MINUTES", os.getenv("CHUNK_MINUTES", "5")))
DEFAULT_WHISPER_MODEL = os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")


def clean_transcript_text(text: str) -> str:
    """Lightweight rule-based cleaner to remove stutter, repetitive filler loops, and ASR artifacts."""
    if not text:
        return ""
    # Remove repetitive identical adjacent phrases (e.g. "you know, you know")
    cleaned = re.sub(r"\b(\w+(?:\s+\w+){1,3})\s+\1\b", r"\1", text, flags=re.IGNORECASE)
    # Remove excessive filler sounds at start of sentences
    cleaned = re.sub(r"\b(um|uh|er|ah)\b,?\s*", "", cleaned, flags=re.IGNORECASE)
    # Normalize multiple whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _transcribe_single(
    client: Groq,
    wav_path: str,
    model: str = DEFAULT_WHISPER_MODEL,
) -> str | None:
    """Send a single 5-minute audio WAV file to Groq Whisper with quota pacing and fallback."""
    current_model = model

    def make_call():
        nonlocal current_model
        with open(wav_path, "rb") as f:
            return client.audio.transcriptions.create(
                file=(Path(wav_path).name, f),
                model=current_model,
            )

    def on_model_fallback(err_msg: str, reason: str):
        nonlocal current_model
        fallback = "whisper-large-v3" if current_model != "whisper-large-v3" else "whisper-large-v3-turbo"
        print(f"  ⚠️ Whisper model '{current_model}' unavailable. Switching to '{fallback}'...")
        current_model = fallback

    try:
        res = call_with_retry_and_quota(
            func=make_call,
            is_whisper=True,
            max_retries=5,
            model_fallback_callback=on_model_fallback,
        )
        return getattr(res, "text", None) or str(res)
    except Exception as e:
        print(f"  Chunk transcription error: {sanitize_error(e)}")
        return None


def transcribe_audio_pipeline(
    audio_path: str | Path,
    transcript_store: TranscriptStore,
    chunk_minutes: int | None = None,
    model: str | None = None,
    resume: bool = False,
    progress_callback: Any = None,
) -> list[TranscriptChunk]:
    """Execute complete 5-minute audio chunking, transcription, and JSON persistence pipeline."""
    audio_file = Path(audio_path).resolve()
    if not audio_file.exists():
        print(f"❌ Error: Audio file not found: {audio_file}")
        return []

    duration_sec = get_duration_seconds(audio_file)
    if duration_sec < 1.0:
        print(f"⚠️ Audio duration is too short ({duration_sec:.1f}s). Skipping transcription.")
        return []

    c_mins = chunk_minutes or DEFAULT_AUDIO_CHUNK_MINUTES
    whisper_model = model or DEFAULT_WHISPER_MODEL

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("❌ Error: GROQ_API_KEY is not set in environment or .env file.")
        return []

    client = Groq(api_key=api_key)

    with tempfile.TemporaryDirectory(prefix="notes_audio_chunks_") as tmp_chunk_dir:
        planned_chunks = plan_audio_chunks(
            audio_file=audio_file,
            output_dir=tmp_chunk_dir,
            chunk_minutes=c_mins,
            overlap_seconds=6.0,
        )
        total_chunks = len(planned_chunks)
        print(f"  Audio is {duration_sec / 60:.1f} min ({duration_sec:.0f}s) — processing {total_chunks} silence-aligned chunks (~{c_mins} min each)...")

        for boundary in planned_chunks:
            chunk_id = boundary.chunk_id
            start_min = boundary.start_seconds / 60.0
            end_min = (boundary.start_seconds + boundary.duration_seconds) / 60.0

            # 1. Skip if already completed and resume=True
            if resume and transcript_store.is_chunk_completed(chunk_id):
                existing = transcript_store.get_chunk(chunk_id)
                if existing:
                    print(f"  [Resume] Chunk {chunk_id:03d}/{total_chunks:03d} already cached ({existing.word_count} words) -> Skipping")
                    if progress_callback:
                        progress_callback(chunk_id, total_chunks)
                    continue

            # 2. Extract 5-minute chunk WAV on-the-fly
            print(f"  Extracting chunk {chunk_id:03d}/{total_chunks:03d} ({start_min:.1f}–{end_min:.1f} min, pause-aligned)...")
            extracted = extract_single_chunk(
                audio_file=audio_file,
                start_sec=boundary.start_seconds,
                duration_sec=boundary.duration_seconds,
                output_path=boundary.output_path,
            )

            if not extracted or not os.path.exists(boundary.output_path):
                gap_msg = f"[Audio segment {start_min:.1f}m–{end_min:.1f}m could not be extracted]"
                print(f"  ⚠️ Noting gap for chunk {chunk_id}: {gap_msg}")
                transcript_store.save_chunk(TranscriptChunk(
                    chunk_id=chunk_id,
                    start_seconds=boundary.start_seconds,
                    end_seconds=boundary.start_seconds + boundary.duration_seconds,
                    transcript=gap_msg,
                    status="error",
                ))
                continue

            # 3. Transcribe via Whisper
            print(f"  Transcribing chunk {chunk_id:03d}/{total_chunks:03d} using {whisper_model}...")
            raw_text = _transcribe_single(client, boundary.output_path, model=whisper_model)

            # 4. Immediately delete chunk WAV to free disk space
            try:
                os.unlink(boundary.output_path)
            except Exception:
                pass

            # 5. Clean and persist JSON
            if raw_text and raw_text.strip():
                clean_text = clean_transcript_text(raw_text)
                chunk_obj = TranscriptChunk(
                    chunk_id=chunk_id,
                    start_seconds=boundary.start_seconds,
                    end_seconds=boundary.start_seconds + boundary.duration_seconds,
                    transcript=clean_text,
                    model=whisper_model,
                    status="completed",
                )
                transcript_store.save_chunk(chunk_obj)
                print(f"  -> Saved chunk {chunk_id:03d} to JSON cache ({chunk_obj.word_count} words)")
            else:
                gap_msg = f"[Audio segment {start_min:.1f}m–{end_min:.1f}m transcription returned empty]"
                transcript_store.save_chunk(TranscriptChunk(
                    chunk_id=chunk_id,
                    start_seconds=boundary.start_seconds,
                    end_seconds=boundary.start_seconds + boundary.duration_seconds,
                    transcript=gap_msg,
                    status="completed",
                ))

            if progress_callback:
                progress_callback(chunk_id, total_chunks)

    completed = transcript_store.get_all_completed()
    total_words = sum(c.word_count for c in completed if not c.transcript.startswith("[Audio segment"))
    dur_min = duration_sec / 60.0
    wpm = (total_words / dur_min) if dur_min > 0 else 0.0
    print(f"\n🎙️  [Transcript Summary] {total_words} total words captured across {len(completed)} chunks (~{wpm:.0f} words/min).")
    
    return completed


def transcribe_audio(
    file_path: str,
    chunk_minutes: int | None = None,
    chunks_dir: str | None = None,
    model: str | None = None,
    resume: bool = False,
) -> list[str]:
    """Compatibility wrapper returning list of transcript text strings."""
    store_dir = chunks_dir or tempfile.mkdtemp(prefix="lecture_transcripts_")
    store = TranscriptStore(store_dir)
    chunks = transcribe_audio_pipeline(
        audio_path=file_path,
        transcript_store=store,
        chunk_minutes=chunk_minutes,
        model=model,
        resume=resume,
    )
    return [c.transcript for c in chunks if c.transcript]
