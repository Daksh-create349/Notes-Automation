"""Audio Splitter — Silence-aware pause detection, ffmpeg splitting, and overlap boundaries.

Features:
  - 5-minute nominal chunks (AUDIO_CHUNK_MINUTES=5)
  - Silence-aware boundaries (detects pauses >= 0.5s within search window)
  - 6-second overlap window to guarantee no words cut at chunk seams
  - Resilient duration detection (ffprobe -> wave module -> byte estimation)
"""

import os
import re
import subprocess
from pathlib import Path
from typing import NamedTuple


class ChunkBoundary(NamedTuple):
    chunk_id: int
    start_seconds: float
    duration_seconds: float
    output_path: str


def sanitize_error(err: Exception | str) -> str:
    """Scrub sensitive API tokens or keys from error strings before logging."""
    msg = str(err)
    msg = re.sub(r"gsk_[A-Za-z0-9_\-]+", "gsk_***[REDACTED]***", msg)
    msg = re.sub(r"ntn_[A-Za-z0-9_\-]+", "ntn_***[REDACTED]***", msg)
    msg = re.sub(r"Bearer\s+[A-Za-z0-9_\-\.]+", "Bearer ***[REDACTED]***", msg)
    return msg


def get_duration_seconds(file_path: str | Path) -> float:
    """Return duration of audio file in seconds using ffprobe with wave and size fallbacks."""
    file_path = str(file_path)
    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        return 0.0

    # 1. Try ffprobe
    try:
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
        dur_str = result.stdout.strip()
        if dur_str:
            return float(dur_str)
    except Exception:
        pass

    # 2. Fallback to standard library wave module for WAV files
    try:
        import wave
        with wave.open(file_path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate > 0:
                return float(frames) / float(rate)
    except Exception:
        pass

    # 3. Fallback to file size estimation for 16kHz mono int16 (32,000 bytes/sec)
    try:
        size = os.path.getsize(file_path)
        if size > 44:
            return float(size - 44) / 32000.0
    except Exception:
        pass

    return 0.0


def find_silence_points(file_path: str | Path, noise_db: str = "-30dB", min_duration_sec: float = 0.5) -> list[float]:
    """Find timestamps (seconds) where natural speech pauses occur in audio via ffmpeg silencedetect."""
    file_path = str(file_path)
    try:
        cmd = [
            "ffmpeg", "-i", file_path,
            "-af", f"silencedetect=noise={noise_db}:d={min_duration_sec}",
            "-f", "null", "-",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        starts = [float(m.group(1)) for m in re.finditer(r"silence_start:\s*([0-9.]+)", res.stderr)]
        return sorted(starts)
    except Exception as e:
        print(f"  Note: silence detection fallback ({sanitize_error(e)})")
        return []


def compute_chunk_boundaries(
    duration: float,
    nominal_chunk_sec: float = 300.0,  # 5 min default
    silence_starts: list[float] | None = None,
    search_window: float = 30.0,
    overlap_sec: float = 6.0,
) -> list[tuple[float, float]]:
    """Compute (start_sec, duration_sec) for chunks, aligning splits with natural pauses and adding a safe overlap window."""
    if duration <= 0:
        return []
    if duration <= nominal_chunk_sec:
        return [(0.0, duration)]

    silences = silence_starts or []
    boundaries = [0.0]
    current_target = nominal_chunk_sec

    while current_target < duration - 15.0:
        candidates = [
            s for s in silences
            if (current_target - search_window) <= s <= (current_target + search_window)
            and s > boundaries[-1] + 15.0
        ]
        if candidates:
            best = min(candidates, key=lambda s: abs(s - current_target))
            boundaries.append(best)
            current_target = best + nominal_chunk_sec
        else:
            boundaries.append(current_target)
            current_target += nominal_chunk_sec

    boundaries.append(duration)

    chunks = []
    for i in range(len(boundaries) - 1):
        raw_start = boundaries[i]
        raw_end = boundaries[i + 1]
        actual_start = max(0.0, raw_start - (overlap_sec if i > 0 else 0.0))
        actual_dur = max(1.0, raw_end - actual_start)
        chunks.append((actual_start, actual_dur))
    return chunks


def extract_single_chunk(audio_file: str | Path, start_sec: float, duration_sec: float, output_path: str | Path) -> bool:
    """Extract a single audio chunk via ffmpeg (16kHz mono WAV)."""
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", str(start_sec),
                "-t", str(duration_sec),
                "-i", str(audio_file),
                "-ar", "16000",    # 16 kHz mono int16
                "-ac", "1",
                str(output_path),
            ],
            capture_output=True,
            check=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.decode("utf-8", errors="replace") if e.stderr else str(e)
        print(f"  ⚠️ ffmpeg extraction failed at {start_sec:.0f}s: {sanitize_error(err_msg)}")
        return False
    except Exception as e:
        print(f"  ⚠️ Chunk extraction error: {sanitize_error(e)}")
        return False


def plan_audio_chunks(
    audio_file: str | Path,
    output_dir: str | Path,
    chunk_minutes: int = 5,
    overlap_seconds: float = 6.0,
) -> list[ChunkBoundary]:
    """Compute all silence-aware chunk boundaries and destination paths without extracting yet."""
    duration = get_duration_seconds(audio_file)
    if duration < 1.0:
        return []

    chunk_sec = chunk_minutes * 60.0
    out_dir_path = Path(output_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)

    if duration <= chunk_sec:
        out_wav = str(out_dir_path / "0001.wav")
        return [ChunkBoundary(1, 0.0, duration, out_wav)]

    print(f"  Analyzing natural speech pauses in audio ({duration / 60:.1f} min)...")
    silences = find_silence_points(audio_file, min_duration_sec=0.5)
    intervals = compute_chunk_boundaries(
        duration=duration,
        nominal_chunk_sec=chunk_sec,
        silence_starts=silences,
        search_window=30.0,
        overlap_sec=overlap_seconds,
    )

    planned = []
    for i, (start_s, dur_s) in enumerate(intervals):
        chunk_num = i + 1
        out_wav = str(out_dir_path / f"{chunk_num:04d}.wav")
        planned.append(ChunkBoundary(chunk_num, start_s, dur_s, out_wav))
    
    return planned
