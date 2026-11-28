"""LLM settings for the worker, read from environment variables (.env in dev).

Every name is listed in .env.example. Values are never logged.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from app.paths import ROOT

EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class LlmSettings:
    model: str                    # MODEL_NAME: first model tried
    fallback_model: str | None    # FALLBACK_MODEL: used on low confidence or a bad answer
    timeout_seconds: float        # LLM_TIMEOUT_SECONDS: per HTTP request
    max_retries: int              # LLM_MAX_RETRIES: retries after the first try, per model call
    retry_backoff_seconds: float  # LLM_RETRY_BACKOFF_SECONDS: base of the exponential backoff
    retry_max_backoff_seconds: float  # LLM_RETRY_MAX_BACKOFF_SECONDS: cap of one wait
    confidence_threshold: float   # LLM_CONFIDENCE_THRESHOLD: below this, use the fallback model
    max_tokens: int               # LLM_MAX_TOKENS
    effort: str | None            # LLM_EFFORT: low|medium|high|xhigh|max, or empty for the model default

    @classmethod
    def from_env(cls) -> "LlmSettings":
        load_dotenv(ROOT / ".env")
        model = os.getenv("MODEL_NAME", "").strip()
        if not model:
            raise RuntimeError("MODEL_NAME is not set (see .env.example)")
        if not os.getenv("ANTHROPIC_API_KEY", "").strip():
            raise RuntimeError("ANTHROPIC_API_KEY is not set (see .env.example)")
        effort = os.getenv("LLM_EFFORT", "").strip().lower() or None
        if effort is not None and effort not in EFFORT_LEVELS:
            raise RuntimeError(f"LLM_EFFORT must be one of {sorted(EFFORT_LEVELS)} or empty")
        threshold = _float("LLM_CONFIDENCE_THRESHOLD", 0.8)
        if not 0 <= threshold <= 1:
            raise RuntimeError("LLM_CONFIDENCE_THRESHOLD must be between 0 and 1")
        fallback = os.getenv("FALLBACK_MODEL", "").strip() or None
        return cls(
            model=model,
            fallback_model=fallback if fallback != model else None,
            timeout_seconds=_float("LLM_TIMEOUT_SECONDS", 120.0),
            max_retries=max(0, _int("LLM_MAX_RETRIES", 3)),
            retry_backoff_seconds=max(0.0, _float("LLM_RETRY_BACKOFF_SECONDS", 2.0)),
            retry_max_backoff_seconds=max(0.0, _float("LLM_RETRY_MAX_BACKOFF_SECONDS", 30.0)),
            confidence_threshold=threshold,
            max_tokens=_int("LLM_MAX_TOKENS", 16000),
            effort=effort,
        )
