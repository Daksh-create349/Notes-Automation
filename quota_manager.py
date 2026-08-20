"""Quota Manager — Proactive RPM/TPM/RPD rate-limit tracking, header inspection, and backoff retries.

Prevents 429 errors during long (2-8+ hr) lectures by proactively pausing when limits are tight,
and handles transient network errors with exponential backoff and retry-after header parsing.
"""

import os
import re
import time
from typing import Any, Callable


def sanitize_error(err: Exception | str) -> str:
    """Scrub sensitive API tokens or keys from error strings before logging."""
    msg = str(err)
    msg = re.sub(r"gsk_[A-Za-z0-9_\-]+", "gsk_***[REDACTED]***", msg)
    msg = re.sub(r"ntn_[A-Za-z0-9_\-]+", "ntn_***[REDACTED]***", msg)
    msg = re.sub(r"Bearer\s+[A-Za-z0-9_\-\.]+", "Bearer ***[REDACTED]***", msg)
    return msg


def _parse_time_str_to_seconds(time_str: str | None) -> float:
    """Parse Groq reset time string like '1m23.4s' or '240ms' or '2.5s' into seconds."""
    if not time_str:
        return 0.0
    s = time_str.strip().lower()
    try:
        if s.endswith("ms"):
            return float(s[:-2]) / 1000.0
        if "m" in s and "s" in s:
            parts = s.split("m")
            mins = float(parts[0])
            secs = float(parts[1].rstrip("s"))
            return mins * 60.0 + secs
        if s.endswith("m"):
            return float(s[:-1]) * 60.0
        if s.endswith("s"):
            return float(s[:-1])
        return float(s)
    except Exception:
        return 0.0


class QuotaTracker:
    """Maintains state of live Groq API rate-limit headers."""

    def __init__(self):
        self.remaining_requests: int | None = None
        self.remaining_tokens: int | None = None
        self.limit_requests: int | None = None
        self.limit_tokens: int | None = None
        self.reset_requests_seconds: float = 0.0
        self.reset_tokens_seconds: float = 0.0
        self.last_updated: float = 0.0

    def update_from_headers(self, headers: Any) -> None:
        """Update tracker with headers from raw response."""
        if not headers:
            return
        try:
            rem_req = headers.get("x-ratelimit-remaining-requests")
            rem_tok = headers.get("x-ratelimit-remaining-tokens")
            lim_req = headers.get("x-ratelimit-limit-requests")
            lim_tok = headers.get("x-ratelimit-limit-tokens")
            rst_req = headers.get("x-ratelimit-reset-requests")
            rst_tok = headers.get("x-ratelimit-reset-tokens")

            if rem_req is not None:
                self.remaining_requests = int(rem_req)
            if rem_tok is not None:
                self.remaining_tokens = int(rem_tok)
            if lim_req is not None:
                self.limit_requests = int(lim_req)
            if lim_tok is not None:
                self.limit_tokens = int(lim_tok)
            
            if rst_req:
                self.reset_requests_seconds = _parse_time_str_to_seconds(rst_req)
            if rst_tok:
                self.reset_tokens_seconds = _parse_time_str_to_seconds(rst_tok)
            
            self.last_updated = time.time()
        except Exception:
            pass

    def log_quota_live(self) -> None:
        """Print clean live status summary."""
        parts = []
        if self.remaining_requests is not None:
            parts.append(f"Reqs: {self.remaining_requests}" + (f"/{self.limit_requests}" if self.limit_requests else ""))
        if self.remaining_tokens is not None:
            parts.append(f"Tokens: {self.remaining_tokens}" + (f"/{self.limit_tokens}" if self.limit_tokens else ""))
        if parts:
            print(f"  [Groq Quota Live] {' | '.join(parts)}")

    def wait_if_needed(self, estimated_tokens: int = 0, is_whisper: bool = False) -> None:
        """Proactively pause if requests or tokens are near threshold to prevent 429."""
        # Check remaining requests limit (threshold <= 2)
        if self.remaining_requests is not None and self.remaining_requests <= 2:
            wait_s = max(2.0, self.reset_requests_seconds + 0.5)
            print(f"  ⏳ [Quota Proactive Pause] Requests low ({self.remaining_requests} left). Waiting {wait_s:.1f}s for reset...")
            time.sleep(wait_s)
            self.remaining_requests = None

        # Check remaining tokens limit for LLM requests
        if not is_whisper and estimated_tokens > 0 and self.remaining_tokens is not None:
            if self.remaining_tokens < (estimated_tokens + 500):
                wait_s = max(3.0, self.reset_tokens_seconds + 0.5)
                print(f"  ⏳ [Quota Proactive Pause] Tokens low ({self.remaining_tokens} left vs ~{estimated_tokens} needed). Waiting {wait_s:.1f}s for reset...")
                time.sleep(wait_s)
                self.remaining_tokens = None


# Global singleton tracker
global_quota = QuotaTracker()


def call_with_retry_and_quota(
    func: Callable[[], Any],
    estimated_tokens: int = 0,
    is_whisper: bool = False,
    max_retries: int = 5,
    backoff_factor: float = 2.0,
    model_fallback_callback: Callable[[str, str], None] | None = None,
) -> Any:
    """Execute API call with proactive quota pacing, live header updates, and exponential backoff on errors."""
    global_quota.wait_if_needed(estimated_tokens=estimated_tokens, is_whisper=is_whisper)

    for attempt in range(max_retries):
        try:
            raw_response = func()
            if hasattr(raw_response, "headers"):
                global_quota.update_from_headers(raw_response.headers)
            return raw_response
        except Exception as e:
            # Check response headers from exception if available
            resp = getattr(e, "response", None)
            if resp is not None and hasattr(resp, "headers"):
                global_quota.update_from_headers(getattr(resp, "headers", None))

            err_msg = sanitize_error(e)
            err_lower = err_msg.lower()

            # 1. Model missing / decommissioned / request size TPM error
            if any(term in err_lower for term in ["model_not_found", "model_decommissioned", "404", "does not exist", "no longer supported", "request too large", "413", "tokens per minute"]):
                if model_fallback_callback:
                    model_fallback_callback(err_msg, "model_or_tpm_error")
                    time.sleep(1.0)
                    continue

            # 2. Rate limit / 429 error
            if "429" in err_msg or "rate_limit" in err_lower:
                if attempt == max_retries - 1:
                    raise Exception(f"Rate limit exceeded (exhausted {max_retries} retries): {err_msg}") from None
                
                # Check if retry-after is provided
                retry_after_s = None
                if resp is not None and hasattr(resp, "headers"):
                    h = getattr(resp, "headers", {})
                    ra = h.get("retry-after") or h.get("x-ratelimit-reset-requests")
                    if ra:
                        retry_after_s = _parse_time_str_to_seconds(ra)

                sleep_time = max((backoff_factor ** attempt) + 1, retry_after_s or 0.0)
                print(f"⚠️ Groq rate limit (429). Retrying in {sleep_time:.1f}s... (Info: {err_msg})")
                time.sleep(sleep_time)
                continue

            # 3. Transient network drops, timeouts, 5xx server issues
            if any(term in err_lower for term in ["timeout", "connection", "500", "502", "503", "504", "server error", "temporarily unavailable", "overloaded"]):
                if attempt == max_retries - 1:
                    raise Exception(f"Server error (exhausted {max_retries} retries): {err_msg}") from None
                sleep_time = (backoff_factor ** attempt) + 1.5
                print(f"⚠️ Groq transient network/server error. Retrying in {sleep_time:.1f}s... (Info: {err_msg})")
                time.sleep(sleep_time)
                continue

            # Non-retryable error
            raise Exception(err_msg) from None
