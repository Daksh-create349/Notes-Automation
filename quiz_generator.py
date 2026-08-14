import os
from groq import Groq

SYSTEM_PROMPT = """You are a quiz creator. Given lecture notes in Markdown, generate exactly 5 multiple-choice questions.

ABSOLUTE RULE — ENGLISH ONLY:
- Every question, every answer option, every word MUST be in standard English.
- NEVER write Hindi, Urdu, Arabic, Hinglish, or any non-Latin characters.
- If the notes contain regional words, translate them to English in your questions.
- Non-Latin characters are STRICTLY FORBIDDEN in the output.

Format each question as:
**Q1. Question text**
A) option
B) option
C) option
D) option

At the end, include an ## Answer Key section listing Q1: A, Q2: B, etc.

Use only Markdown. No extra commentary."""


import re
import time


def _clean_non_latin(text: str | None) -> str | None:
    if not text:
        return text
    cleaned = re.sub(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF\u0900-\u097F]+", "", text)
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    return cleaned.strip()


def generate_quiz(notes: str, max_retries: int = 3) -> str | None:
    try:
        client = Groq(api_key=os.environ["GROQ_API_KEY"])
        for attempt in range(max_retries):
            try:
                response = client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": f"Notes:\n\n{notes}"},
                    ],
                    temperature=0.3,
                    max_tokens=2048,
                )
                return _clean_non_latin(response.choices[0].message.content)
            except Exception as e:
                err = str(e)
                if attempt < max_retries - 1:
                    wait = 20 * (attempt + 1)
                    print(f"  Quiz generation retry {attempt + 1}/{max_retries} in {wait}s due to: {err[:80]}...")
                    time.sleep(wait)
                else:
                    raise
    except Exception as e:
        print(f"Quiz generation error: {e}")
        return None


def append_quiz_to_notion(page_id: str, quiz_markdown: str) -> bool:
    try:
        from notion_client import Client
        from notion_saver import _md_to_blocks
        client = Client(auth=os.environ["NOTION_TOKEN"])
        divider = [{"object": "block", "type": "divider", "divider": {}}]
        blocks = divider + _md_to_blocks(quiz_markdown)
        client.blocks.children.append(block_id=page_id, children=blocks)
        return True
    except Exception as e:
        print(f"Quiz Notion append error: {e}")
        return False
