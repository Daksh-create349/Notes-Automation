"""Session Manager — Isolated session directories, state.json lifecycle, and inter-process safety.

Lifecycle States:
  RECORDING -> SPLITTING -> TRANSCRIBING -> NOTE_GENERATION -> QUIZ_GENERATION -> NOTION_UPLOAD -> COMPLETE
"""

import json
import os
import re
import shutil
import time
from enum import Enum
from pathlib import Path
from typing import Any


class SessionStatus(str, Enum):
    RECORDING = "RECORDING"
    SPLITTING = "SPLITTING"
    TRANSCRIBING = "TRANSCRIBING"
    NOTE_GENERATION = "NOTE_GENERATION"
    QUIZ_GENERATION = "QUIZ_GENERATION"
    NOTION_UPLOAD = "NOTION_UPLOAD"
    COMPLETE = "COMPLETE"
    ERROR = "ERROR"


ACTIVE_SESSION_POINTER = Path("/tmp/notes_automation_active_session.json")
DEFAULT_SESSIONS_BASE = Path("/tmp/notes_sessions")


def sanitize_filename(name: str) -> str:
    """Convert arbitrary title string into a safe filesystem string."""
    cleaned = re.sub(r"[^\w\-_\. ]", "_", name.strip()).replace(" ", "_")
    return cleaned[:100] if cleaned else "lecture"


class Session:
    """Encapsulates a lecture session's directory tree, metadata, and state.json."""

    def __init__(self, session_dir: Path | str, lecture_title: str = "Lecture Notes"):
        self.session_dir = Path(session_dir).resolve()
        self.lecture_title = lecture_title or "Lecture Notes"
        
        # Subdirectories
        self.audio_dir = self.session_dir / "audio"
        self.audio_chunks_dir = self.session_dir / "audio_chunks"
        self.transcripts_dir = self.session_dir / "transcripts"
        self.sections_dir = self.session_dir / "sections"
        self.autosave_dir = self.session_dir / "autosave"
        
        # File pointers
        self.state_file = self.session_dir / "state.json"
        clean_stem = sanitize_filename(self.lecture_title)
        self.audio_file = self.audio_dir / f"{clean_stem}.wav"

    def init_dirs(self) -> None:
        """Create all required directory structures."""
        for d in (self.session_dir, self.audio_dir, self.audio_chunks_dir, 
                 self.transcripts_dir, self.sections_dir, self.autosave_dir):
            d.mkdir(parents=True, exist_ok=True)

    def load_state(self) -> dict[str, Any]:
        """Load state.json if present, or return defaults."""
        if self.state_file.exists() and self.state_file.stat().st_size > 0:
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        
        return {
            "session_id": self.session_dir.name,
            "lecture_title": self.lecture_title,
            "pid": os.getpid(),
            "status": SessionStatus.RECORDING.value,
            "audio_file": str(self.audio_file),
            "total_audio_chunks": 0,
            "transcribed_chunks": 0,
            "total_llm_blocks": 0,
            "generated_sections": 0,
            "quiz_generated": False,
            "notion_url": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }

    def save_state(self, updates: dict[str, Any] | None = None) -> dict[str, Any]:
        """Update and persist state.json."""
        current = self.load_state()
        if updates:
            current.update(updates)
        current["updated_at"] = time.time()
        self.state_file.write_text(json.dumps(current, indent=2), encoding="utf-8")
        return current

    def update_status(self, status: SessionStatus, **kwargs) -> None:
        """Convenience method to advance session status."""
        updates = {"status": status.value}
        updates.update(kwargs)
        self.save_state(updates)

    def save_active_pointer(self) -> None:
        """Save this session's location to global active session pointer."""
        pointer_data = {
            "session_id": self.session_dir.name,
            "session_dir": str(self.session_dir),
            "pid": os.getpid(),
            "lecture_title": self.lecture_title,
            "created_at": time.time(),
        }
        ACTIVE_SESSION_POINTER.write_text(json.dumps(pointer_data, indent=2), encoding="utf-8")

    def cleanup_audio(self) -> None:
        """Explicitly remove raw audio and autosave buffer from disk to free space immediately."""
        if self.audio_file.exists():
            try:
                self.audio_file.unlink(missing_ok=True)
            except Exception:
                pass
        if self.audio_dir.exists():
            try:
                shutil.rmtree(self.audio_dir, ignore_errors=True)
            except Exception:
                pass
        if self.autosave_dir.exists():
            try:
                shutil.rmtree(self.autosave_dir, ignore_errors=True)
            except Exception:
                pass
        if self.audio_chunks_dir.exists():
            try:
                shutil.rmtree(self.audio_chunks_dir, ignore_errors=True)
            except Exception:
                pass

    def cleanup(self, force: bool = False) -> None:
        """Delete temporary session directory and all ephemeral files unless preserved."""
        if self.session_dir.exists() and self.session_dir.is_dir():
            try:
                shutil.rmtree(self.session_dir, ignore_errors=True)
                print(f"🧹 Cleaned up ephemeral session directory ({self.session_dir.name}) — zero local traces remain.")
            except Exception as e:
                print(f"⚠️ Warning: could not remove session dir {self.session_dir}: {e}")
        clear_active_pointer()


def create_new_session(lecture_title: str | None = None, base_dir: Path | str | None = None) -> Session:
    """Create a brand new isolated session with unique timestamped directory."""
    title = (lecture_title.strip() if lecture_title and lecture_title.strip() else "Lecture Notes")
    clean_stem = sanitize_filename(title)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    unique_id = f"session_{clean_stem}_{int(time.time())}_{os.urandom(3).hex()}"
    
    root_base = Path(base_dir) if base_dir else DEFAULT_SESSIONS_BASE
    session_path = root_base / unique_id
    
    session = Session(session_path, lecture_title=title)
    session.init_dirs()
    session.save_state()
    session.save_active_pointer()
    return session


def load_active_session() -> Session | None:
    """Load active session from pointer file if valid."""
    if ACTIVE_SESSION_POINTER.exists() and ACTIVE_SESSION_POINTER.stat().st_size > 0:
        try:
            data = json.loads(ACTIVE_SESSION_POINTER.read_text(encoding="utf-8"))
            s_dir = data.get("session_dir")
            if s_dir and Path(s_dir).exists():
                return Session(Path(s_dir), lecture_title=data.get("lecture_title", "Lecture Notes"))
        except Exception:
            pass
    return None


def clear_active_pointer() -> None:
    """Remove active session pointer."""
    ACTIVE_SESSION_POINTER.unlink(missing_ok=True)


def find_resumable_sessions(base_dir: Path | str | None = None) -> list[Session]:
    """Scan sessions base directory for interrupted sessions that can be resumed."""
    root_base = Path(base_dir) if base_dir else DEFAULT_SESSIONS_BASE
    if not root_base.exists():
        return []
    
    sessions = []
    for p in root_base.iterdir():
        if p.is_dir() and (p / "state.json").exists():
            try:
                state = json.loads((p / "state.json").read_text(encoding="utf-8"))
                if state.get("status") != SessionStatus.COMPLETE.value:
                    sessions.append(Session(p, lecture_title=state.get("lecture_title", p.name)))
            except Exception:
                continue
    return sorted(sessions, key=lambda s: s.session_dir.stat().st_mtime, reverse=True)
