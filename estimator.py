"""Estimator — Long-lecture quota feasibility analyzer and dry-run calculations.

Calculates:
  - Audio chunk count & Whisper audio seconds vs daily quota (e.g. 28,800s)
  - LLM block count (LLM_CHUNKS_PER_BLOCK=3), estimated tokens vs daily allowance
  - Formatted ASCII feasibility dashboard with recommendations
"""

import math
import os
from typing import Any
from quota_manager import global_quota, sanitize_error


# Constants for Groq free-tier limits (configurable via env)
WHISPER_DAILY_AUDIO_SEC_LIMIT = float(os.getenv("GROQ_WHISPER_DAILY_SEC_LIMIT", "28800"))  # 8 hours audio/day
LLM_DAILY_TOKEN_LIMIT = int(os.getenv("GROQ_LLM_DAILY_TOKEN_LIMIT", "500000"))
AVG_WORDS_PER_MINUTE = 130
AVG_TOKENS_PER_WORD = 1.35


def calculate_lecture_metrics(
    duration_minutes: float,
    audio_chunk_minutes: int = 5,
    llm_chunks_per_block: int = 3,
) -> dict[str, Any]:
    """Compute detailed call counts, audio durations, and token projections."""
    dur_min = max(0.1, duration_minutes)
    
    # 1. Audio & Whisper
    audio_chunk_sec = audio_chunk_minutes * 60.0
    total_audio_sec = dur_min * 60.0
    num_audio_chunks = max(1, math.ceil(dur_min / audio_chunk_minutes))
    whisper_calls = num_audio_chunks
    
    # 2. LLM Blocks
    block_duration_minutes = audio_chunk_minutes * llm_chunks_per_block
    num_llm_blocks = max(1, math.ceil(num_audio_chunks / llm_chunks_per_block))
    
    # 3. Token Estimations
    total_words_spoken = dur_min * AVG_WORDS_PER_MINUTE
    total_transcript_tokens = total_words_spoken * AVG_TOKENS_PER_WORD
    
    # Per LLM block: input transcript tokens + system prompt tokens (~1500)
    tokens_per_llm_block_input = (total_transcript_tokens / num_llm_blocks) + 1500
    tokens_per_llm_block_output = 2000  # Detailed pedagogical notes output
    total_llm_tokens = (tokens_per_llm_block_input + tokens_per_llm_block_output) * num_llm_blocks
    
    # Quiz tokens
    quiz_input_tokens = 3000
    quiz_output_tokens = 800
    total_quiz_tokens = quiz_input_tokens + quiz_output_tokens
    
    total_projected_tokens = total_llm_tokens + total_quiz_tokens
    total_api_calls = whisper_calls + num_llm_blocks + 1

    # Feasibility status
    whisper_fits = total_audio_sec <= WHISPER_DAILY_AUDIO_SEC_LIMIT
    llm_fits = total_projected_tokens <= LLM_DAILY_TOKEN_LIMIT

    return {
        "duration_minutes": dur_min,
        "duration_hours": dur_min / 60.0,
        "audio_chunk_minutes": audio_chunk_minutes,
        "llm_chunks_per_block": llm_chunks_per_block,
        "block_duration_minutes": block_duration_minutes,
        "total_audio_sec": total_audio_sec,
        "num_audio_chunks": num_audio_chunks,
        "whisper_calls": whisper_calls,
        "num_llm_blocks": num_llm_blocks,
        "total_api_calls": total_api_calls,
        "total_projected_tokens": int(total_projected_tokens),
        "whisper_fits": whisper_fits,
        "llm_fits": llm_fits,
    }


def check_live_groq_quota() -> dict[str, Any] | None:
    """Perform a minimal test request to inspect live rate-limit headers."""
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return None
    try:
        from groq import Groq
        from note_generator import get_groq_llm_model
        client = Groq(api_key=api_key)
        model = get_groq_llm_model(client)
        raw = client.chat.completions.with_raw_response.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
        global_quota.update_from_headers(raw.headers)
        return {
            "rem_req": raw.headers.get("x-ratelimit-remaining-requests"),
            "rem_tokens": raw.headers.get("x-ratelimit-remaining-tokens"),
            "limit_req": raw.headers.get("x-ratelimit-limit-requests"),
            "limit_tokens": raw.headers.get("x-ratelimit-limit-tokens"),
            "reset_req": raw.headers.get("x-ratelimit-reset-requests"),
            "reset_tokens": raw.headers.get("x-ratelimit-reset-tokens"),
        }
    except Exception:
        return None


def print_lecture_estimate(
    duration_minutes: float,
    audio_chunk_minutes: int = 5,
    llm_chunks_per_block: int = 3,
) -> None:
    """Display comprehensive visual estimation dashboard for the specified lecture duration."""
    m = calculate_lecture_metrics(duration_minutes, audio_chunk_minutes, llm_chunks_per_block)
    
    hours_int = int(m["duration_hours"])
    mins_rem = int(m["duration_minutes"] % 60)
    time_display = f"{hours_int}h {mins_rem:02d}m" if hours_int > 0 else f"{mins_rem}m"

    w = 68
    border = "═" * w
    div = "─" * w

    print(f"\n{border}")
    print("             LECTURE QUOTA & DURATION ESTIMATE")
    print(border)
    print(f" Duration:              {time_display} ({m['duration_minutes']:.1f} minutes)")
    print(f" Audio Chunks:          {m['num_audio_chunks']} chunks ({audio_chunk_minutes} min each)")
    print(f" Whisper Requests:      {m['whisper_calls']} calls (whisper-large-v3-turbo)")
    print(f" LLM Blocks:            {m['num_llm_blocks']} blocks (~{m['block_duration_minutes']} min transcript / block)")
    print(f" Total API Calls:       {m['total_api_calls']} calls")
    print(div)
    
    # Whisper analysis
    whisper_status = "✅ Fits in daily allowance" if m["whisper_fits"] else "⚠️ Exceeds standard daily allowance"
    print(f" 🎙️  Whisper Audio:       {m['total_audio_sec']:,.0f} sec (Daily limit: {WHISPER_DAILY_AUDIO_SEC_LIMIT:,.0f} sec)")
    print(f"    Status:             {whisper_status}")
    print(div)

    # LLM analysis
    llm_status = "✅ Fits in daily allowance" if m["llm_fits"] else "⚠️ High token volume — paced rate-limiting active"
    print(f" 🧠 LLM Token Estimate:  ~{m['total_projected_tokens']:,} tokens (Model: openai/gpt-oss-120b)")
    print(f"    Status:             {llm_status}")
    print(div)

    # Live Quota
    live = check_live_groq_quota()
    if live and (live.get("rem_req") or live.get("rem_tokens")):
        print(f" 🌐 Live Groq Quota:")
        if live.get("rem_req"):
            print(f"    • Requests Remaining: {live['rem_req']}/{live.get('limit_req', '1000')} (resets in {live.get('reset_req', 'N/A')})")
        if live.get("rem_tokens"):
            print(f"    • Tokens Remaining:   {live['rem_tokens']}/{live.get('limit_tokens', 'N/A')}")
        print(div)

    # Recommendations
    if not m["whisper_fits"] or not m["llm_fits"]:
        print(" 💡 Recommendation for Long Lecture:")
        print("    Run './lec transcribe-only' first to cache all audio transcripts,")
        print("    then run './lec generate-only' to generate notes.")
    else:
        print(" ✅ Quota is sufficient. Fully automated live run supported.")
    print(f"{border}\n")
