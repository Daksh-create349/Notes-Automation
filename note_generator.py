"""Note Generator — GPT-OSS-120B Knowledge Synthesis Engine with Decoupled LLM Block Grouping.

Features:
  - Groups 3 transcript chunks (~15 mins) per LLM generation block (LLM_CHUNKS_PER_BLOCK=3)
  - Uses compact rolling context carry-over (context_manager.py) to prevent token bloat on long (2-8+ hr) lectures
  - Strict anti-summarization prompt engineering: definitions, mechanisms, derivations, code, tables, callouts
  - Auto-healing dynamic model fallback on 404/decommissioned errors
  - Immediate section disk persistence via section_store.py
  - Content retention quality audit against transcript
"""

import json
import math
import os
import re
import time
from typing import Any, Callable

from groq import Groq

from context_manager import LectureContext
from quota_manager import call_with_retry_and_quota, global_quota, sanitize_error
from section_store import NoteSection, SectionStore
from transcript_store import TranscriptChunk


DEFAULT_LLM_CHUNKS_PER_BLOCK = int(os.getenv("LLM_CHUNKS_PER_BLOCK", os.getenv("CHUNKS_PER_BLOCK", "3")))
DEFAULT_LLM_MODEL = os.getenv("GROQ_NOTE_MODEL") or os.getenv("GROQ_LLM_MODEL") or os.getenv("GROQ_MODEL") or "openai/gpt-oss-120b"
DEFAULT_TEMPERATURE = float(os.getenv("GROQ_TEMPERATURE", "0.2"))

_FALLBACK_MODELS = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.6-27b",
    "openai/gpt-oss-20b",
    "groq/compound",
    "llama-3.3-70b-versatile",
]


def _clean_non_latin(text: str | None) -> str | None:
    """Sanitize output to remove any accidental non-Latin/Arabic/Urdu/Devanagari characters."""
    if not text:
        return text
    cleaned = re.sub(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF\u0900-\u097F]+", "", text)
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    return cleaned.strip()


def get_groq_llm_model(client: Groq | None = None, preferred: str | None = None) -> str:
    """Resolve active Groq LLM model from preferences or fallbacks."""
    if preferred:
        return preferred
    env_model = os.getenv("GROQ_NOTE_MODEL") or os.getenv("GROQ_LLM_MODEL") or os.getenv("GROQ_MODEL")
    if env_model:
        return env_model
    return DEFAULT_LLM_MODEL


SYSTEM_PROMPT = """You are a faithful, detail-oriented class notes writer creating comprehensive, highly structured lecture study notes from a transcript — written so a student who missed the lecture entirely can learn the topic from these notes alone.

NO LENGTH LIMIT:
There is no target word count, no minimum, no maximum, no ratio to hit. Notes should be as long as the lecture actually requires — if the teacher spent 10 minutes explaining one concept in depth, your notes for that concept should be equally thorough. Let the actual teaching content decide length. Never cut something short to "keep notes concise," and never pad something to "look detailed."

SOURCE OF TRUTH & ZERO INVENTED CONTENT:
The transcript is the only source of information. Never invent explanations, add outside knowledge, or fill gaps with generic textbook content. If the teacher didn't say it, it doesn't go in the notes. If something is unclear or incomplete in the transcript, write "(unclear in recording)" instead of guessing.

CONCEPT-FIRST SYNTHESIS:
Explain concepts properly — definition, how it works, step-by-step reasoning, why it matters, examples — as if writing a textbook section on the topic.

DIRECT EXPLANATORY VOICE (NO LECTURER NARRATION):
Strip lecturer-narration voice entirely. Write in direct explanatory voice like a study guide.
- Never use phrases like "the lecturer mentions", "the professor states", or "the instructor describes".
- Write as direct instructional content.

WHAT COMPREHENSIVE MEANS HERE:
Preserve the full instructional substance — every concept explained, every example, analogy, or comparison used, every code snippet shown or described, every specific number/date/formula/instruction mentioned, every meaningful student question and its answer.
- Do not compress a multi-step explanation into one line. Do not remove intermediate reasoning or steps.
- Preserve exact technical values, variable names, syntax, numbers, names, and outputs.

DEPTH REQUIREMENT (PER TOPIC, MINIMUM):
For each topic/concept covered, ensure notes provide:
- Definition / what it is
- Mechanism / how it works
- Worked example with real numbers, scenarios, or code where applicable
- Edge cases, gotchas, or common mistakes (using a "Note:" callout)
- Strict verbatim preservation for: definitions, formulas, equations, code, and theorems

PURE ENGLISH ONLY — ABSOLUTE RULE:
Every single word in the output must be standard English. Translate all Hindi/Hinglish/regional speech to academic English. Never output non-Latin script.

DOMAIN-SPECIFIC CONTENT:
- Code: reproduce verbatim in a "code" block with the correct language tag (Python, JavaScript, C++, SQL, Bash, etc.).
- Formulas & equations: reproduce exactly as given.
- Tables: format multi-attribute comparisons as 2D tables.
- Student Q&A: format as callout blocks (color: "gray_background", icon: "❓").

Output ONLY valid JSON:
{
  "title": "string",
  "blocks": [
    {"type": "heading_1", "text": "Topic Name", "color": "blue"},
    {"type": "callout", "text": "Preview: ...", "color": "blue_background", "icon": "🎯"},
    {"type": "callout", "text": "Warning: ...", "color": "yellow_background", "icon": "⚠️"},
    {"type": "heading_2", "text": "Subtopic Name", "color": "default"},
    {"type": "paragraph", "text": "..."},
    {"type": "bulleted_list_item", "text": "..."},
    {"type": "code", "text": "...", "language": "python"},
    {"type": "table", "rows": [["Header 1", "Header 2"], ["Cell 1", "Cell 2"]]}
  ]
}
"""


def _clean_and_parse_json(content: str | None) -> dict | None:
    """Clean reasoning tags, markdown fences, non-latin scripts, and extract valid JSON dictionary safely."""
    if not content or not content.strip():
        return None
    raw = content.strip()
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    raw = _clean_non_latin(raw) or ""
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    raw = raw.strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        match = re.search(r"(\{.*\})", raw, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1))
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
    return None


def _content_retention_check(transcript: str, blocks: list[dict]) -> tuple[bool, list[str]]:
    """Scan synthesized blocks against transcript to ensure no numbers, identifiers, or Q&A were dropped."""
    if not transcript or not blocks:
        return True, []

    note_text = " ".join(str(b.get("text", "")) for b in blocks if isinstance(b, dict))
    note_text_lower = note_text.lower()
    gaps = []

    # Check key numbers
    numbers = set(re.findall(r"\b\d{2,}\b", transcript))
    for num in numbers:
        if num not in note_text:
            gaps.append(f"Number '{num}' present in transcript but missing from notes")

    # Check code identifiers
    identifiers = set(re.findall(r"`([A-Za-z0-9_]{3,20})`", transcript))
    for ident in identifiers:
        if ident.lower() not in note_text_lower:
            gaps.append(f"Identifier '{ident}' missing from notes")

    # Check student Q&A indicators
    if any(q in transcript.lower() for q in ["sir what if", "can we do", "question sir", "why does", "doubt"]):
        has_qa_callout = any(
            b.get("type") == "callout" and ("?" in str(b.get("text", "")) or b.get("icon") in ("?", "❓"))
            for b in blocks if isinstance(b, dict)
        )
        if not has_qa_callout:
            gaps.append("Student question/answer exchange was discussed in transcript but not captured as a Q&A callout")

    return (len(gaps) == 0), gaps[:5]


def generate_notes_for_block(
    transcript_block: str,
    block_label: str,
    context: LectureContext,
    model: str | None = None,
    retry_strict: bool = False,
) -> dict[str, Any] | None:
    """Send a ~15-minute transcript block to Groq GPT-OSS-120B with compact context and auto-healing fallback."""
    if not transcript_block or not transcript_block.strip():
        return None

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("❌ Error: GROQ_API_KEY is not set.")
        return None

    client = Groq(api_key=api_key)
    active_model = get_groq_llm_model(client, preferred=model)

    user_prompt = context.format_for_llm_prompt()
    user_prompt += f"Transcript for segment ({block_label}):\n\n{transcript_block}"

    if retry_strict:
        user_prompt += (
            "\n\n[RETRY: Previous output was missing structure or dropped specific transcript details "
            "(numbers, identifiers, or Q&A). Re-check the transcript and ensure every detail appears in output.]"
        )

    user_prompt += (
        "\n\n[MANDATORY PEDAGOGICAL DEPTH & ANTI-SUMMARY INSTRUCTION]:\n"
        "- Write EXHAUSTIVE, deeply detailed conceptual study notes. DO NOT write a high-level summary.\n"
        "- Explain concepts thoroughly: definition, underlying mechanism, step-by-step reasoning, examples, and edge cases.\n"
        "- Preserve all formulas, code snippets, numbers, technical terminology, and student Q&A in full detail."
    )

    estimated_tokens = int(len(transcript_block.split()) * 1.4) + 1200
    safe_max_tokens = min(4096, max(1500, 7000 - estimated_tokens))

    def make_call():
        nonlocal active_model
        return client.chat.completions.create(
            model=active_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=safe_max_tokens,
            temperature=0.2 if retry_strict else DEFAULT_TEMPERATURE,
        )

    def on_model_fallback(err_msg: str, reason: str):
        nonlocal active_model
        for cand in _FALLBACK_MODELS:
            if cand != active_model:
                print(f"  ⚠️ LLM fallback triggered ({reason}: '{active_model}'). Switching to '{cand}'...")
                active_model = cand
                break

    try:
        response = call_with_retry_and_quota(
            func=make_call,
            estimated_tokens=estimated_tokens,
            is_whisper=False,
            max_retries=5,
            model_fallback_callback=on_model_fallback,
        )
        content = response.choices[0].message.content
        parsed = _clean_and_parse_json(content)

        if not retry_strict and parsed:
            retention_ok, gaps = _content_retention_check(transcript_block, parsed.get("blocks", []))
            if not retention_ok:
                print(f"  ⚠️ Note retention check found gaps ({len(gaps)} items). Running precision recovery pass...")
                return generate_notes_for_block(
                    transcript_block=transcript_block,
                    block_label=block_label,
                    context=context,
                    model=active_model,
                    retry_strict=True,
                )

        return parsed
    except Exception as e:
        print(f"  Note generation error for block {block_label}: {sanitize_error(e)}")
        return None


def generate_all_notes(
    transcript_chunks: list[TranscriptChunk],
    section_store: SectionStore,
    chunks_per_block: int | None = None,
    model: str | None = None,
    resume: bool = False,
    progress_callback: Any = None,
) -> dict[str, Any] | None:
    """Group 5-minute transcript chunks into ~15-minute LLM blocks and synthesize exhaustive notes."""
    valid_chunks = [c for c in transcript_chunks if c.transcript and not c.transcript.startswith("[Audio segment")]
    if not valid_chunks:
        print("⚠️ No valid transcript chunks provided for note generation.")
        return None

    c_per_block = chunks_per_block or DEFAULT_LLM_CHUNKS_PER_BLOCK
    total_chunks = len(valid_chunks)
    num_blocks = math.ceil(total_chunks / c_per_block)

    print(f"\n🧠 Generating comprehensive conceptual study notes across {num_blocks} blocks "
          f"({c_per_block} chunks ≈ {c_per_block * 5} min / block)...")

    context = LectureContext()
    combined_blocks: list[dict[str, Any]] = []
    overall_title = "Lecture Notes"

    for block_idx in range(num_blocks):
        section_id = block_idx + 1
        start_chunk_idx = block_idx * c_per_block
        end_chunk_idx = min(start_chunk_idx + c_per_block, total_chunks)
        current_chunk_group = valid_chunks[start_chunk_idx:end_chunk_idx]
        chunk_ids = [c.chunk_id for c in current_chunk_group]

        # 1. Skip-if-exists resume check
        if resume and section_store.is_section_completed(section_id):
            cached_section = section_store.get_section(section_id)
            if cached_section:
                print(f"  [Resume] Section {section_id:02d}/{num_blocks:02d} (Chunks {chunk_ids}) already cached ({len(cached_section.blocks)} blocks) -> Skipping")
                context.update_from_section_blocks(cached_section.blocks)
                combined_blocks.extend(cached_section.blocks)
                overall_title = cached_section.title or overall_title
                if progress_callback:
                    progress_callback(section_id, num_blocks)
                continue

            # 2. Combine chunk texts for this ~15-min block
        merged_text = "\n\n".join(c.transcript for c in current_chunk_group)
        block_label = f"Section {section_id}/{num_blocks} (Chunks {chunk_ids[0]}–{chunk_ids[-1]})"
        
        print(f"\n  📝 Synthesizing {block_label} using {model or DEFAULT_LLM_MODEL}...")
        parsed_result = generate_notes_for_block(
            transcript_block=merged_text,
            block_label=block_label,
            context=context,
            model=model,
        )

        if parsed_result:
            sec_title = parsed_result.get("title") or overall_title
            overall_title = sec_title
            sec_blocks = parsed_result.get("blocks", [])

            # Update rolling context
            context.update_from_section_blocks(sec_blocks)
            combined_blocks.extend(sec_blocks)

            # Persist section to disk
            section_obj = NoteSection(
                section_id=section_id,
                chunk_ids=chunk_ids,
                title=sec_title,
                blocks=sec_blocks,
            )
            section_store.save_section(section_obj)
            print(f"  -> Saved Section {section_id:02d} to JSON cache ({len(sec_blocks)} blocks)")
        else:
            print(f"  ⚠️ Warning: Section {section_id} produced no structured blocks.")

        if progress_callback:
            progress_callback(section_id, num_blocks)

        if block_idx < num_blocks - 1:
            time.sleep(1.5)

    if not combined_blocks:
        print("❌ Note generation produced no valid blocks.")
        return None

    print(f"\n✅ All {num_blocks} note sections synthesized successfully ({len(combined_blocks)} total blocks).")
    return {
        "title": overall_title,
        "blocks": combined_blocks,
    }
