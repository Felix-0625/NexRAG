"""Redact PII and hash identities before trace and memory storage."""

from __future__ import annotations

import hashlib
import hmac
import re

from src.config import settings

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    # Mainland Chinese identity numbers have 18 characters and may end in X.
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[ID_NUMBER]"),
    # Payment cards have 13-19 digits with optional grouping separators.
    (re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"), "[CARD_NUMBER]"),
    # Support domestic 11-digit and international phone-number forms.
    (re.compile(r"(?<!\d)(?:\+\d{1,3}[ -]?)?1[3-9]\d{9}(?!\d)"), "[PHONE]"),
    (re.compile(r"(?i)\b(?:sk|pk|api|key|token)[-_][A-Za-z0-9]{16,}\b"), "[SECRET]"),
]


def redact(text: str | None) -> str:
    """Mask emails, identity numbers, cards, phones, and key-like tokens."""
    if not text:
        return ""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def contains_pii(text: str) -> bool:
    """Check whether redaction would alter text."""
    return redact(text) != text


def store_content(text: str | None, limit: int = 4_000) -> str | None:
    """Apply TRACE_STORE_CONTENT policy and bound stored length."""
    mode = settings.TRACE_STORE_CONTENT.lower()
    if mode == "none" or text is None:
        return None
    value = text if mode == "full" else redact(text)
    return value[:limit]


def hash_user(user_id: str) -> str:
    """Return a stable keyed hash so traces contain no raw user IDs."""
    secret = settings.TRACE_HASH_SECRET or settings.NEXRAG_USER_JWT_SECRET or "nexrag-local"
    digest = hmac.new(secret.encode(), user_id.encode(), hashlib.sha256).hexdigest()
    return digest[:32]
