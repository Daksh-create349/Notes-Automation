import os
from groq import Groq

SYSTEM_PROMPT = """You are a note-taking assistant. Given a lecture transcript, produce structured Markdown notes.

IMPORTANT RULES:
- Always write the notes in ENGLISH only, regardless of the language of the transcript.
- If the transcript is in Hindi, Hinglish, or any other language, translate everything to English first, then write the notes.

Structure the notes with:
- A concise **title** (# heading)
- A **bullet summary** of main points (## Summary)
- **Key definitions** for important terms (## Key Definitions)
- **Examples** mentioned or implied (## Examples)

Use clean Markdown formatting."""


def generate_notes(transcript: str) -> str | None:
    try:
        client = Groq(api_key=os.environ["GROQ_API_KEY"])
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Transcript:\n\n{transcript}"},
            ],
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"Note generation error: {e}")
        return None
