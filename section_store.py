"""Section Store — Granular disk persistence for structured note sections.

Each generated 15-minute LLM block is saved immediately to disk:
  sections/section_001.json, sections/section_002.json, ...

Enables granular resumability and guarantees that partial progress in long lectures is never lost.
"""

import json
import re
import time
from pathlib import Path
from typing import Any


class NoteSection:
    """Represents a generated note section containing structured blocks and markdown representation."""

    def __init__(
        self,
        section_id: int,
        chunk_ids: list[int],
        title: str,
        blocks: list[dict[str, Any]],
        markdown: str = "",
        status: str = "completed",
        timestamp: float | None = None,
    ):
        self.section_id = section_id
        self.chunk_ids = chunk_ids
        self.title = title
        self.blocks = blocks
        self.markdown = markdown
        self.status = status
        self.timestamp = timestamp or time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "chunk_ids": self.chunk_ids,
            "title": self.title,
            "blocks": self.blocks,
            "markdown": self.markdown,
            "status": self.status,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NoteSection":
        return cls(
            section_id=int(data["section_id"]),
            chunk_ids=[int(c) for c in data.get("chunk_ids", [])],
            title=str(data.get("title", "Lecture Notes")),
            blocks=list(data.get("blocks", [])),
            markdown=str(data.get("markdown", "")),
            status=str(data.get("status", "completed")),
            timestamp=float(data.get("timestamp", time.time())),
        )


class SectionStore:
    """Manages reading and writing structured note section JSON files."""

    def __init__(self, sections_dir: Path | str):
        self.sections_dir = Path(sections_dir)
        self.sections_dir.mkdir(parents=True, exist_ok=True)

    def _section_file(self, section_id: int) -> Path:
        return self.sections_dir / f"section_{section_id:03d}.json"

    def is_section_completed(self, section_id: int) -> bool:
        """Check if section JSON exists and contains blocks."""
        f = self._section_file(section_id)
        if f.exists() and f.stat().st_size > 0:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                return data.get("status") == "completed" and bool(data.get("blocks"))
            except Exception:
                return False
        return False

    def save_section(self, section: NoteSection) -> Path:
        """Persist a single note section to disk."""
        f = self._section_file(section.section_id)
        f.write_text(json.dumps(section.to_dict(), indent=2), encoding="utf-8")
        return f

    def get_section(self, section_id: int) -> NoteSection | None:
        """Load a single section from disk."""
        f = self._section_file(section_id)
        if f.exists() and f.stat().st_size > 0:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                return NoteSection.from_dict(data)
            except Exception:
                return None
        return None

    def get_all_sections(self) -> list[NoteSection]:
        """Load all completed note sections sorted by section_id."""
        def sort_key(p: Path) -> int:
            m = re.search(r"section_(\d+)", p.stem)
            return int(m.group(1)) if m else 0

        sections: list[NoteSection] = []
        for f in sorted(self.sections_dir.glob("section_*.json"), key=sort_key):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if data.get("status") == "completed" and data.get("blocks"):
                    sections.append(NoteSection.from_dict(data))
            except Exception:
                continue
        return sorted(sections, key=lambda s: s.section_id)

    def get_all_blocks(self) -> list[dict[str, Any]]:
        """Combine all blocks across completed sections in sequential order."""
        blocks: list[dict[str, Any]] = []
        for sec in self.get_all_sections():
            blocks.extend(sec.blocks)
        return blocks

    def get_all_markdown(self) -> str:
        """Combine markdown across all sections, rendering from blocks if needed."""
        parts = []
        for sec in self.get_all_sections():
            if sec.markdown and sec.markdown.strip():
                parts.append(sec.markdown.strip())
            elif sec.blocks:
                lines = [f"# {sec.title}"]
                for b in sec.blocks:
                    b_type = b.get("type", "")
                    text = str(b.get("text", "")).strip()
                    if not text:
                        continue
                    if b_type == "heading_1":
                        lines.append(f"\n## {text}\n")
                    elif b_type in ("heading_2", "heading_3"):
                        lines.append(f"\n### {text}\n")
                    elif b_type == "callout":
                        lines.append(f"> **Note:** {text}")
                    elif b_type in ("bulleted_list_item", "numbered_list_item"):
                        lines.append(f"- {text}")
                    elif b_type == "code":
                        lang = b.get("language", "text")
                        lines.append(f"```{lang}\n{text}\n```")
                    elif b_type == "table":
                        rows = b.get("rows", [])
                        if rows:
                            header = "| " + " | ".join(rows[0]) + " |"
                            divider = "| " + " | ".join(["---"] * len(rows[0])) + " |"
                            lines.append(header + "\n" + divider)
                            for r in rows[1:]:
                                lines.append("| " + " | ".join(r) + " |")
                    else:
                        lines.append(f"{text}\n")
                parts.append("\n".join(lines))
        return "\n\n---\n\n".join(parts)

    def clear(self) -> None:
        """Delete all cached section JSON files."""
        for f in self.sections_dir.glob("section_*.json"):
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass
