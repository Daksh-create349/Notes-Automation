import os
import re
import time
from groq import Groq

SYSTEM_PROMPT = """You are a faithful, detail-oriented class notes writer. Your ONLY job is to capture EXACTLY what the teacher said in the lecture — nothing more, nothing less.

CRITICAL RULES — READ CAREFULLY:
1. **TRANSCRIPT-FAITHFUL**: Only write what the teacher ACTUALLY said. Do NOT invent explanations, add your own knowledge, or fill in gaps with generic textbook content.
2. **CAPTURE EVERYTHING THE TEACHER MENTIONED**:
   - Every concept they explained (in their words)
   - Every example they gave
   - Every analogy or comparison they used
   - Every assignment, task, or homework they announced
   - Every warning, tip, or "remember this" they said
   - Every specific code they wrote or described
   - Any dates, deadlines, or instructions mentioned
3. **PURE ENGLISH ONLY**: Translate any Hindi/Hinglish/regional speech into clean English. Never leave non-English words in the notes.
4. **DETECT THE PROGRAMMING LANGUAGE**: If teacher mentions code, use the exact language they were teaching (JavaScript, C++, Python, Java, etc.). Do NOT default to Python.
5. **ZERO INVENTED CONTENT**: If the teacher did not say it, DO NOT write it. No "additional context", no "it is also worth knowing", no generic textbook filler.

STRUCTURE YOUR NOTES LIKE THIS:

# 📚 [Lecture Title — What Was Taught Today]

## 🎯 What the Teacher Covered Today
(Brief 2-3 line summary of today's class — based only on what was said)

## 📖 Lecture Notes
(Use ### sub-headings for each topic the teacher discussed, in the ORDER they discussed it)

For each topic:
- Write what the teacher explained IN THEIR OWN WORDS (translated to English)
- Include every specific example, analogy, or story they gave
- Include any code they wrote or described — in the exact language being taught
- Include any specific numbers, formulas, or values they mentioned

## 📌 Assignments & Tasks Given
(List EVERY assignment, homework, task, or project the teacher mentioned — even if minor)
- Assignment name / description
- Any deadline or instructions mentioned

## ⚠️ Teacher's Warnings & Important Notes
(Things the teacher specifically said to remember, watch out for, or emphasized repeatedly)

## 🔑 Key Terms Mentioned by Teacher
(Only terms the teacher actually defined or used — with their definition as the teacher explained it)

## 📝 Quick Revision Points
(5-8 bullet points of the most important things from today's class — only from what was taught)"""


def _clean_non_latin(text: str | None) -> str | None:
    """Sanitize output to remove any accidental non-Latin/Arabic/Urdu/Devanagari characters."""
    if not text:
        return text
    # Remove Arabic, Urdu, Persian, Devanagari unicode blocks
    cleaned = re.sub(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF\u0900-\u097F]+", "", text)
    # Clean any leftover empty parentheses like () or ( )
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    return cleaned.strip()


# ~4 chars per token; reserve ~600 tokens for system prompt + ~4096 for response
# Safe input budget: 12000 - 600 - 4096 = ~7300 tokens -> ~29000 chars
_MAX_CHUNK_CHARS = 28_000


def _chunk_transcript(transcript: str) -> list[str]:
    if len(transcript) <= _MAX_CHUNK_CHARS:
        return [transcript]
    chunks: list[str] = []
    remaining = transcript
    while remaining:
        if len(remaining) <= _MAX_CHUNK_CHARS:
            chunks.append(remaining)
            break
        split_at = _MAX_CHUNK_CHARS
        for sep in (".\n", ". ", "?\n", "? ", "!\n", "! ", "\n\n", "\n"):
            idx = remaining.rfind(sep, int(_MAX_CHUNK_CHARS * 0.6), _MAX_CHUNK_CHARS)
            if idx != -1:
                split_at = idx + len(sep)
                break
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    return chunks


def _call_groq(client: Groq, messages: list, max_retries: int = 4) -> str | None:
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=messages,
                temperature=0.3,
                max_tokens=4096,
            )
            return _clean_non_latin(response.choices[0].message.content)
        except Exception as e:
            err = str(e)
            is_rate_limit = any(k in err for k in ("413", "rate_limit_exceeded", "tokens per minute", "tokens per day"))
            if is_rate_limit and attempt < max_retries - 1:
                wait = 30 * (2 ** attempt)  # 30s, 60s, 120s
                print(f"Rate limit hit — waiting {wait}s (retry {attempt + 2}/{max_retries})...")
                time.sleep(wait)
            else:
                raise
    return None


def generate_notes(transcript: str) -> str | None:
    try:
        client = Groq(api_key=os.environ["GROQ_API_KEY"])
        chunks = _chunk_transcript(transcript)

        if len(chunks) == 1:
            return _call_groq(client, [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Here is the full lecture transcript. Write detailed class notes capturing EXACTLY what the teacher said — their explanations, examples, analogies, assignments, and warnings. Translate everything to clean English. Do NOT add any information the teacher did not say:\n\n{transcript}"},
            ])

        print(f"Transcript is large — processing in {len(chunks)} chunks...")
        partial_notes: list[str] = []
        for i, chunk in enumerate(chunks, 1):
            print(f"  Chunk {i}/{len(chunks)}...")
            result = _call_groq(client, [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"This is part {i} of {len(chunks)} of a lecture transcript. Write class notes capturing EXACTLY what the teacher said in this portion — their specific explanations, examples, analogies, assignments, and warnings. Translate to clean English. Do NOT add anything the teacher did not say:\n\n{chunk}"},
            ])
            if result:
                partial_notes.append(result)
            if i < len(chunks):
                time.sleep(10)  # respect TPM between chunk requests

        if not partial_notes:
            return None
        if len(partial_notes) == 1:
            return partial_notes[0]

        # Synthesize all partial notes into one cohesive document
        print("Synthesizing all chunks into final notes...")
        combined = "\n\n---PART BREAK---\n\n".join(partial_notes)
        synthesis_user = (
            f"Below are notes from {len(chunks)} parts of the same lecture. "
            "Merge them into one cohesive, well-structured set of notes following the required format in pure standard English. "
            f"Remove duplicates and ensure logical flow:\n\n{combined}"
        )
        if len(synthesis_user) <= _MAX_CHUNK_CHARS + 2000:
            time.sleep(10)
            result = _call_groq(client, [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": synthesis_user},
            ])
            if result:
                return result

        # Fallback: join partial notes directly if synthesis is also too large
        return _clean_non_latin("\n\n".join(partial_notes))

    except Exception as e:
        print(f"Note generation error: {e}")
        return None
