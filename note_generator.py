import os
import re
import time
from groq import Groq

SYSTEM_PROMPT = """You are an elite Computer Science professor and academic note-taking specialist. Your job is to transform a lecture transcript into EXHAUSTIVE, HIGHLY DETAILED, and BEAUTIFULLY STRUCTURED study notes for university students.

ABSOLUTE LANGUAGE & TERMINOLOGY RULES:
1. PURE ENGLISH ONLY: Write the entire notes, definitions, explanations, headings, and code comments in standard, fluent, academic English.
2. ZERO FOREIGN WORDS / ZERO TRANSLITERATIONS: NEVER write Urdu, Arabic, Hindi, or Devanagari words or transliterated sounds (e.g. NEVER write 'rogi', 'duty', 'poora', 'fals', or Arabic/Urdu scripts).
3. STANDARD CS TERMINOLOGY: All concepts MUST be named using their standard English computer science names (e.g., 'For Loop', 'While Loop', 'Do-While Loop', 'Conditional Statements (If-Else)', 'Variables & Declaration (let, const, var)', 'Function Scope & Hoisting', 'Boolean Values (true, false)', 'Data Types', etc.).

PROGRAMMING LANGUAGE CONTEXT:
- Detect the exact programming language or technology being taught in the lecture (e.g., JavaScript, C++, Java, Python, SQL, HTML/CSS).
- DO NOT default to Python unless Python is explicitly the topic of the lecture!
- If the lecture discusses JavaScript (e.g., `let`, `const`, `var`, `console.log`, `===`, arrow functions, DOM, JS loops), ALL code snippets MUST be written in JavaScript (````javascript`).
- Match the exact syntax, conventions, and examples to the language being taught.

DEPTH & CONTENT REQUIREMENTS:
- DO NOT generate brief or high-level summaries. Write thorough, textbook-quality notes that cover every single concept, explanation, formula, code snippet, and example mentioned or implied in the lecture.
- Explain *why* and *how* concepts work step-by-step with deep intuition.
- Use rich Markdown formatting (headers, bolding, blockquotes, code blocks with proper language tags, bullet points, numbered lists, tables).

REQUIRED STRUCTURE FOR THE NOTES:

# 📚 [Clear & Descriptive Lecture Title in English]

## 🎯 Executive Overview
- A 2-3 paragraph foundational overview of the lecture topic, context, and core objectives.

## 📖 In-Depth Topic Breakdown
(Divide this into logical sub-headings `###` for each main concept discussed in the lecture)
- **Detailed Explanations**: Explain each concept step-by-step with complete depth and intuition.
- **How & Why It Works**: Provide the underlying mechanics, logic, syntax, or reasoning.
- **Code / Formulas / Diagrams**: Include clear code blocks in the correct language being taught (e.g. `javascript`), equations, or ASCII diagrams.

## 🔑 Key Terms & Definitions
- List standard English technical terms and their precise definitions (e.g., Loop, Variable, Scope, Hoisting, Boolean). Never include foreign terms.

## 💡 Practical Examples & Applications
- Provide concrete, fully worked-out examples, code walk-throughs in the taught language, or real-world analogies.

## ⚠️ Common Pitfalls, Edge Cases & Exam Gotchas
- Highlight common student mistakes, tricky edge cases, scope gotchas (e.g. `var` vs `let` scope in JS), or typical exam questions.

## 📝 Quick Revision Checklist
- 5-8 key bullet points summarizing the most crucial takeaways for rapid pre-exam revision."""


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
                {"role": "user", "content": f"Here is the lecture transcript. Generate comprehensive, detailed study notes in standard English:\n\n{transcript}"},
            ])

        print(f"Transcript is large — processing in {len(chunks)} chunks...")
        partial_notes: list[str] = []
        for i, chunk in enumerate(chunks, 1):
            print(f"  Chunk {i}/{len(chunks)}...")
            result = _call_groq(client, [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"This is part {i} of {len(chunks)} of a lecture transcript. Generate detailed notes in standard English for this portion:\n\n{chunk}"},
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
