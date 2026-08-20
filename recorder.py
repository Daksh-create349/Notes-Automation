"""Audio recording via sounddevice.

Records from the default microphone at 16 kHz mono into a temporary WAV
file. Recording runs in a background thread so start_recording() returns
immediately; stop_recording() finalizes the WAV and returns its path.

Crash-Safety Features:
  - Periodic auto-save buffer every 5 minutes to /tmp/lecture_autosave/partial_audio.wav
  - Crash recovery detection on startup
  - Periodic heartbeat logging
"""

import os
import re
import tempfile
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000  # Hz — Whisper-friendly rate
CHANNELS = 1
DTYPE = "int16"

AUTOSAVE_DIR = Path(os.getenv("AUTOSAVE_DIR", "/tmp/lecture_autosave"))
AUTOSAVE_FILE = AUTOSAVE_DIR / "partial_audio.wav"
AUTOSAVE_INTERVAL_SECONDS = float(os.getenv("AUTOSAVE_INTERVAL_SECONDS", "300"))  # 5 min default

# --- Module state -----------------------------------------------------------
_is_recording = False          # global flag: prevents multiple recordings
_frames: list = []             # captured audio chunks
_thread: threading.Thread | None = None
_lock = threading.Lock()
_current_path: Path | None = None
_current_autosave_dir: Path = AUTOSAVE_DIR
_current_autosave_file: Path = AUTOSAVE_FILE
_start_time: float = 0.0
_recent_max_amp: int = 0


def check_crash_recovery() -> str | None:
    """Check if a leftover partial recording exists from a previous crash and surface it to user."""
    if AUTOSAVE_FILE.exists() and AUTOSAVE_FILE.stat().st_size > 44:
        try:
            with wave.open(str(AUTOSAVE_FILE), "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                dur = frames / float(rate) if rate else 0.0
                dur_str = f"{dur / 60:.1f} min ({dur:.1f}s)" if dur >= 60 else f"{dur:.1f}s"
            print(
                f"\n⚠️  Found partial recording from previous crash:\n"
                f"   Path     : {AUTOSAVE_FILE}\n"
                f"   Duration : {dur_str}\n"
                f"   -> Use 'python3 main.py transcribe-only --audio {AUTOSAVE_FILE}' to recover it manually.\n"
            )
            return str(AUTOSAVE_FILE)
        except Exception as e:
            print(f"Warning: could not inspect partial recording file: {e}")
    return None


def _write_autosave_buffer():
    """Write current in-memory audio frames to rolling partial_audio.wav safely."""
    global _frames, _current_autosave_dir, _current_autosave_file
    if not _frames:
        return
    try:
        _current_autosave_dir.mkdir(parents=True, exist_ok=True)
        audio = np.concatenate(list(_frames), axis=0)
        tmp_autosave = _current_autosave_dir / "partial_audio.tmp.wav"
        with wave.open(str(tmp_autosave), "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio.tobytes())
        tmp_autosave.replace(_current_autosave_file)
    except Exception as e:
        print(f"⚠️  Auto-save buffer write warning: {e}")


def _callback(indata, frames, time_info, status):
    """sounddevice callback — runs on the audio thread for each block."""
    global _recent_max_amp
    if status:
        print(f"⚠️  Audio input status: {status}")
    _frames.append(indata.copy())
    try:
        max_val = int(np.max(np.abs(indata)))
        if max_val > _recent_max_amp:
            _recent_max_amp = max_val
    except Exception:
        pass


def _record_loop():
    """Background thread body: keep input stream open, log heartbeats, and write rolling auto-save buffer."""
    global _is_recording, _start_time, _recent_max_amp
    last_autosave = _start_time

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            callback=_callback,
        ):
            while _is_recording:
                sd.sleep(500)
                now = time.time()
                if now - last_autosave >= AUTOSAVE_INTERVAL_SECONDS:
                    elapsed_sec = now - _start_time
                    hours = int(elapsed_sec // 3600)
                    minutes = int((elapsed_sec % 3600) // 60)
                    approx_mb = (len(_frames) * 1024 * 2) / (1024 * 1024) if _frames else 0.0
                    
                    # Auto-save buffer to disk
                    _write_autosave_buffer()
                    
                    level_msg = "healthy audio signal" if _recent_max_amp > 200 else "⚠️ LOW/SILENT SIGNAL (check microphone)"
                    print(f"🎙️  [Recording Heartbeat] {hours}h {minutes:02d}m elapsed | ~{approx_mb:.1f} MB buffered | {level_msg}")
                    _recent_max_amp = 0
                    last_autosave = now
    except Exception as e:
        print(f"\n❌ CRITICAL: Microphone recording stream interrupted or disconnected! (Error: {e})\n")
        # Attempt emergency write of captured frames
        _write_autosave_buffer()


def start_recording(
    output_path: Path | str | None = None,
    filename: str | None = None,
    autosave_dir: Path | str | None = None,
) -> bool:
    """Start recording in a separate thread and return immediately.

    Accepts an optional custom output path or filename/title and session autosave directory.
    Returns True if recording started, False if one was already running.
    """
    global _is_recording, _frames, _thread, _current_path, _start_time, _current_autosave_dir, _current_autosave_file

    with _lock:
        if _is_recording:
            print("⚠️  A recording is already in progress.")
            return False

        if output_path:
            _current_path = Path(output_path)
            _current_path.parent.mkdir(parents=True, exist_ok=True)
        elif filename and filename.strip():
            clean_name = re.sub(r"[^\w\-_\. ]", "_", filename.strip()).replace(" ", "_")
            if not clean_name.endswith(".wav"):
                clean_name += ".wav"
            _current_path = Path(tempfile.gettempdir()) / clean_name
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            _current_path = Path(tempfile.gettempdir()) / f"lecture_{timestamp}.wav"

        if autosave_dir:
            _current_autosave_dir = Path(autosave_dir)
            _current_autosave_file = _current_autosave_dir / "partial_audio.wav"
        else:
            _current_autosave_dir = AUTOSAVE_DIR
            _current_autosave_file = AUTOSAVE_FILE

        try:
            sd.check_input_settings(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE)
        except Exception as mic_err:
            print(f"\n❌ Microphone Error: Cannot access audio input device ({mic_err}).")
            print("   Please ensure microphone permissions are granted in System Settings -> Privacy & Security -> Microphone.\n")
            return False

        _frames = []
        _is_recording = True
        _start_time = time.time()
        _thread = threading.Thread(target=_record_loop, daemon=True)
        _thread.start()

    print(f"🎙️  Recording started... File: {_current_path.name} (Press Ctrl+C to stop)")
    return True


def stop_recording() -> str | None:
    """Stop recording, save the WAV file, and return its path. Deletes autosave partial file on success."""
    global _is_recording, _thread, _current_autosave_file

    with _lock:
        if not _is_recording:
            print("⚠️  No recording in progress.")
            return None
        _is_recording = False

    if _thread is not None:
        _thread.join(timeout=5)

    print("⏹️  Recording stopped.")

    if not _frames:
        print("⚠️  No audio was captured.")
        return None

    try:
        audio = np.concatenate(_frames, axis=0)
        with wave.open(str(_current_path), "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)  # int16 = 2 bytes
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio.tobytes())

        duration = len(audio) / SAMPLE_RATE
        print(f"💾 Saved {duration / 60:.1f} min ({duration:.1f}s) of audio to: {_current_path}")

        # Delete rolling autosave file on clean final write
        if _current_autosave_file and _current_autosave_file.exists():
            try:
                _current_autosave_file.unlink(missing_ok=True)
            except Exception:
                pass

        return str(_current_path)
    except Exception as e:
        print(f"Error saving recording WAV file: {e}")
        return None


def is_recording() -> bool:
    """Return True if a recording is currently in progress."""
    return _is_recording
