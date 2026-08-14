"""Audio recording via sounddevice.

Records from the default microphone at 16 kHz mono into a temporary WAV
file. Recording runs in a background thread so start_recording() returns
immediately; stop_recording() finalizes the WAV and returns its path.
"""

import tempfile
import threading
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000  # Hz — Whisper-friendly rate
CHANNELS = 1
DTYPE = "int16"

# --- Module state -----------------------------------------------------------
_is_recording = False          # global flag: prevents multiple recordings
_frames: list = []             # captured audio chunks
_thread: threading.Thread | None = None
_lock = threading.Lock()
_current_path: Path | None = None


def _callback(indata, frames, time_info, status):
    """sounddevice callback — runs on the audio thread for each block."""
    if status:
        print(f"⚠️  Audio status: {status}")
    _frames.append(indata.copy())


def _record_loop():
    """Background thread body: keep the input stream open while recording."""
    global _is_recording
    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype=DTYPE,
        callback=_callback,
    ):
        while _is_recording:
            sd.sleep(100)


def start_recording() -> bool:
    """Start recording in a separate thread and return immediately.

    Returns True if recording started, False if one was already running.
    """
    global _is_recording, _frames, _thread, _current_path

    with _lock:
        if _is_recording:
            print("⚠️  A recording is already in progress.")
            return False

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        _current_path = Path(tempfile.gettempdir()) / f"lecture_{timestamp}.wav"

        _frames = []
        _is_recording = True
        _thread = threading.Thread(target=_record_loop, daemon=True)
        _thread.start()

    print("🎙️  Recording started...")
    return True


def stop_recording() -> str | None:
    """Stop recording, save the WAV file, and return its path.

    Returns None if no recording was in progress.
    """
    global _is_recording, _thread

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

    audio = np.concatenate(_frames, axis=0)
    with wave.open(str(_current_path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)  # int16 = 2 bytes
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())

    duration = len(audio) / SAMPLE_RATE
    print(f"💾 Saved {duration:.1f}s of audio to: {_current_path}")
    return str(_current_path)


def is_recording() -> bool:
    """Return True if a recording is currently in progress."""
    return _is_recording
