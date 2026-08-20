"""Context Manager — Compact rolling context carry-over between LLM generation blocks.

Prevents prompt token bloat across long (2–8+ hr) lectures by tracking only essential
continuity state (current topic, recent topic list, key technical terms, 1-sentence continuation)
rather than repeatedly resending previous full lecture notes.
"""

import json
import re
from typing import Any


COLOR_ROTATION = ["blue", "purple", "green"]


class LectureContext:
    """Encapsulates the compact rolling context passed from one LLM block to the next."""

    def __init__(
        self,
        current_topic: str | None = None,
        previous_topics: list[str] | None = None,
        important_terms: list[str] | None = None,
        continuation: str | None = None,
        last_color: str = "blue",
    ):
        self.current_topic = current_topic
        self.previous_topics: list[str] = previous_topics or []
        self.important_terms: list[str] = important_terms or []
        self.continuation = continuation
        self.last_color = last_color

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_topic": self.current_topic,
            "previous_topics": self.previous_topics[-8:],
            "important_terms": self.important_terms[-15:],
            "continuation": self.continuation,
            "last_color": self.last_color,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LectureContext":
        return cls(
            current_topic=data.get("current_topic"),
            previous_topics=data.get("previous_topics", []),
            important_terms=data.get("important_terms", []),
            continuation=data.get("continuation"),
            last_color=data.get("last_color", "blue"),
        )

    def get_next_heading_color(self) -> str:
        """Rotate to the next distinct color in the sequence."""
        try:
            idx = COLOR_ROTATION.index(self.last_color)
            return COLOR_ROTATION[(idx + 1) % len(COLOR_ROTATION)]
        except ValueError:
            return "blue"

    def format_for_llm_prompt(self) -> str:
        """Produce a clean, compact ~80-token prompt header for the LLM."""
        lines = []
        if self.previous_topics:
            lines.append(f"[PREVIOUS TOPICS COVERED: {', '.join(self.previous_topics[-6:])}]")
        if self.current_topic:
            lines.append(f"[CURRENT OPEN TOPIC: '{self.current_topic}'. If this segment continues it, maintain this section flow without redundant duplicate H1 tags.]")
        if self.last_color:
            next_col = self.get_next_heading_color()
            lines.append(f"[HEADING COLOR: Last used was '{self.last_color}'. Use '{next_col}' for new H1 sections.]")
        if self.important_terms:
            lines.append(f"[ESTABLISHED TERMINOLOGY: {', '.join(self.important_terms[-10:])}]")
        if self.continuation and self.continuation.strip():
            lines.append(f"[CONTINUITY CONTEXT: {self.continuation.strip()}]")

        return "\n".join(lines) + ("\n\n" if lines else "")

    def update_from_section_blocks(self, blocks: list[dict[str, Any]], continuation_text: str | None = None) -> None:
        """Extract latest topic, heading colors, and key technical identifiers from synthesized note blocks."""
        if not blocks:
            return

        for b in blocks:
            if not isinstance(b, dict):
                continue
            b_type = b.get("type")
            text = str(b.get("text", "")).strip()

            if b_type == "heading_1" and text:
                if self.current_topic and self.current_topic != text:
                    if self.current_topic not in self.previous_topics:
                        self.previous_topics.append(self.current_topic)
                self.current_topic = text
                color = str(b.get("color", ""))
                if color in COLOR_ROTATION:
                    self.last_color = color

            elif b_type in ("heading_2", "heading_3") and text:
                # Track sub-topics
                pass

            # Extract code identifiers / capitalized terms
            if text:
                found_terms = re.findall(r"`([A-Za-z0-9_]{2,25})`|\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b", text)
                for code_t, cap_t in found_terms:
                    t = (code_t or cap_t).strip()
                    if t and len(t) > 2 and t not in self.important_terms and t not in ("The", "This", "Here", "Note", "Warning"):
                        self.important_terms.append(t)

        if continuation_text:
            self.continuation = continuation_text.strip()[:200]
        else:
            # Fallback continuation from the last paragraph block
            last_para = next((str(b.get("text", "")) for b in reversed(blocks) if b.get("type") in ("paragraph", "bulleted_list_item")), None)
            if last_para:
                self.continuation = last_para.strip()[:180]
