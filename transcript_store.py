"""Transcript Store — Per-chunk JSON transcript cache and resume validation.

Each transcribed 5-minute chunk is immediately saved as a JSON metadata object:
  transcripts/0001.json, transcripts/0002.json, ...

Enables granular crash recovery: if a run fails at chunk 48, chunks 1..47 are loaded directly from disk.
"""

import json
import re
import time
from pathlib import Path
from typing import Any


class TranscriptChunk:
    """Represents a cached 5-minute transcript chunk with time bounds and metadata."""

    def __init__(
        self,
        chunk_id: int,
        start_seconds: float,
        end_seconds: float,
        transcript: str,
        word_count: int | None = None,
        model: str = "whisper-large-v3-turbo",
        status: str = "completed",
        timestamp: float | None = None,
    ):
        self.chunk_id = chunk_id
        self.start_seconds = start_seconds
        self.end_seconds = end_seconds
        self.duration_seconds = max(0.0, end_seconds - start_seconds)
        self.transcript = transcript.strip()
        self.word_count = word_count if word_count is not None else len(self.transcript.split())
        self.model = model
        self.status = status
        self.timestamp = timestamp or time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "start_seconds": round(self.start_seconds, 2),
            "end_seconds": round(self.end_seconds, 2),
            "duration_seconds": round(self.duration_seconds, 2),
            "transcript": self.transcript,
            "word_count": self.word_count,
            "model": self.model,
            "status": self.status,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranscriptChunk":
        return cls(
            chunk_id=int(data["chunk_id"]),
            start_seconds=float(data.get("start_seconds", 0.0)),
            end_seconds=float(data.get("end_seconds", 0.0)),
            transcript=str(data.get("transcript", "")),
            word_count=int(data.get("word_count", 0)),
            model=str(data.get("model", "whisper-large-v3-turbo")),
            status=str(data.get("status", "completed")),
            timestamp=float(data.get("timestamp", time.time())),
        )


class TranscriptStore:
    """Manages reading and writing per-chunk transcript JSON files in a session directory."""

    def __init__(self, transcripts_dir: Path | str):
        self.transcripts_dir = Path(transcripts_dir)
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)

    def _chunk_file(self, chunk_id: int) -> Path:
        return self.transcripts_dir / f"{chunk_id:04d}.json"

    def is_chunk_completed(self, chunk_id: int) -> bool:
        """Check if chunk exists, is completed, and has non-empty transcript."""
        f = self._chunk_file(chunk_id)
        if f.exists() and f.stat().st_size > 0:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                return data.get("status") == "completed" and bool(data.get("transcript", "").strip())
            except Exception:
                return False
        return False

    def save_chunk(self, chunk: TranscriptChunk) -> Path:
        """Save a single transcript chunk to disk as JSON."""
        f = self._chunk_file(chunk.chunk_id)
        f.write_text(json.dumps(chunk.to_dict(), indent=2), encoding="utf-8")
        return f

    def get_chunk(self, chunk_id: int) -> TranscriptChunk | None:
        """Load a single chunk from disk."""
        f = self._chunk_file(chunk_id)
        if f.exists() and f.stat().st_size > 0:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                return TranscriptChunk.from_dict(data)
            except Exception:
                return None
        return None

    def get_all_completed(self) -> list[TranscriptChunk]:
        """Load all completed transcript chunks sorted by chunk_id."""
        def sort_key(p: Path) -> int:
            m = re.search(r"(\d+)", p.stem)
            return int(m.group(1)) if m else 0

        chunks: list[TranscriptChunk] = []
        for f in sorted(self.transcripts_dir.glob("*.json"), key=sort_key):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if data.get("status") == "completed" and data.get("transcript"):
                    chunks.append(TranscriptChunk.from_dict(data))
            except Exception:
                continue
        return sorted(chunks, key=lambda c: c.chunk_id)

    def get_full_transcript_text(self) -> str:
        """Concatenate all completed transcript chunk texts into a single string."""
        chunks = self.get_all_completed()
        return " ".join(c.transcript for c in chunks if c.transcript)

    def total_words(self) -> int:
        """Return total word count across all completed chunks."""
        return sum(c.word_count for c in self.get_all_completed())

    def clear(self) -> None:
        """Delete all cached transcript JSON files."""
        for f in self.transcripts_dir.glob("*.json"):
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass
