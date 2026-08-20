"""Quiz Generator — Single-pass 5-question conceptual MCQ generator and Notion appender.

Generates 5 multiple-choice questions once at the end of the lecture based on the complete
set of synthesized notes (or conceptual summary for ultra-long lectures).
"""

import os
import re
from typing import Any

from groq import Groq

from quota_manager import call_with_retry_and_quota, sanitize_error


SYSTEM_PROMPT = """You are a quiz creator for lecture study notes. Given lecture notes, generate exactly 5 multiple-choice questions that test genuine understanding of what was actually taught — not generic textbook trivia.

SOURCE OF TRUTH:
Only ask questions whose answer is directly supported by the provided notes. Never invent facts, numbers, or concepts not present in the material.

QUESTION QUALITY RULES:
- Each question must test a real concept, reasoning, code behavior, or specific detail actually covered.
- Mix question types across the 5: at least 1 conceptual/reasoning question ("why does X happen"), at least 1 specific-detail question (number, name, formula, exact value), and if code was covered, at least 1 code-behavior question ("what does this output").
- Wrong options (distractors) must be plausible, not random.
- Exactly 4 options per question, exactly 1 correct answer.
- After each question, include a 1-sentence explanation for why the correct answer is correct, grounded in the notes.
- Tag each question's difficulty as Easy, Medium, or Hard.

ABSOLUTE RULE — ENGLISH ONLY:
Every question, option, and explanation must be in standard English. Never write Hindi, Urdu, or non-Latin script.

Format each question as:
**Q1. [Difficulty: Easy/Medium/Hard] Question text**
A) option
B) option
C) option
D) option
*Explanation: 1 sentence grounded in the notes.*

At the end, include an ## Answer Key section listing Q1: A, Q2: B, etc.
Use only Markdown."""


def generate_quiz(notes: str, model: str | None = None) -> str | None:
    """Generate 5 conceptual review questions from lecture notes in a single final pass."""
    if not notes or not notes.strip():
        return None

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("❌ Error: GROQ_API_KEY is not set.")
        return None

    try:
        from note_generator import get_groq_llm_model
        client = Groq(api_key=api_key)
        quiz_model = model or os.getenv("GROQ_QUIZ_MODEL") or get_groq_llm_model(client)

        # Truncate input if lecture notes are excessively long to fit within token limit
        notes_text = notes.strip()
        words = notes_text.split()
        if len(words) > 6000:
            notes_text = " ".join(words[:6000]) + "\n\n[... Remaining notes omitted for quiz scope ...]"

        def make_call():
            return client.chat.completions.create(
                model=quiz_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"Lecture Notes for Quiz Generation:\n\n{notes_text}"},
                ],
                temperature=0.3,
            )

        response = call_with_retry_and_quota(
            func=make_call,
            estimated_tokens=len(words) + 1500,
            is_whisper=False,
            max_retries=5,
        )
        content = response.choices[0].message.content or ""
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        return content
    except Exception as e:
        print(f"Quiz generation error: {sanitize_error(e)}")
        return None


def append_quiz_to_notion(page_id: str, quiz_markdown: str) -> bool:
    """Append quiz blocks to Notion page safely in batches of 80 blocks with retry and fallback."""
    if not quiz_markdown or not quiz_markdown.strip():
        return False
    notion_token = os.environ.get("NOTION_TOKEN")
    if not notion_token:
        return False
    try:
        from notion_client import Client
        from notion_saver import _md_to_blocks
        import time

        client = Client(auth=notion_token)
        raw_blocks = _md_to_blocks(quiz_markdown)
        if not raw_blocks:
            return False

        divider = [{"object": "block", "type": "divider", "divider": {}}]
        blocks = divider + raw_blocks

        # Batch append in blocks of 80 to strictly respect Notion's 100-block limit
        batch_size = 80
        for i in range(0, len(blocks), batch_size):
            batch = blocks[i:i + batch_size]
            for attempt in range(3):
                try:
                    client.blocks.children.append(block_id=page_id, children=batch)
                    break
                except Exception as batch_err:
                    if attempt == 2:
                        print(f"Quiz batch append retry fallback: {sanitize_error(batch_err)}")
                        for single_b in batch:
                            try:
                                client.blocks.children.append(block_id=page_id, children=[single_b])
                            except Exception:
                                pass
                    time.sleep(2 * (attempt + 1))
        return True
    except Exception as e:
        print(f"Quiz Notion append error: {sanitize_error(e)}")
        return False
