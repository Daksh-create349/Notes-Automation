import os
from groq import Groq

SYSTEM_PROMPT = """You are a quiz creator. Given lecture notes in Markdown, generate exactly 5 multiple-choice questions.

IMPORTANT: Always write the questions and answers in ENGLISH only.

Format each question as:
**Q1. Question text**
A) option
B) option
C) option
D) option

At the end, include an ## Answer Key section listing Q1: A, Q2: B, etc.

Use only Markdown. No extra commentary."""


def generate_quiz(notes: str) -> str | None:
    try:
        client = Groq(api_key=os.environ["GROQ_API_KEY"])
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Notes:\n\n{notes}"},
            ],
        )
        return response.choices[0].message.content
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
