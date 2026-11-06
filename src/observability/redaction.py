"""对落库的 trace 和记忆做 PII 脱敏与身份哈希。"""

from __future__ import annotations

import hashlib
import hmac
import re

from src.config import settings

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    # 中国大陆居民身份证号（18 位，末位可为 X）。
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[ID_NUMBER]"),
    # 支付卡号：13-19 位数字，可带分组分隔。
    (re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"), "[CARD_NUMBER]"),
    # 手机号（国内 11 位，或国际 +NN... 格式）。
    (re.compile(r"(?<!\d)(?:\+\d{1,3}[ -]?)?1[3-9]\d{9}(?!\d)"), "[PHONE]"),
    (re.compile(r"(?i)\b(?:sk|pk|api|key|token)[-_][A-Za-z0-9]{16,}\b"), "[SECRET]"),
]


def redact(text: str | None) -> str:
    """屏蔽邮箱、证件/银行卡/手机号以及形似 API key 的令牌。"""
    if not text:
        return ""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def contains_pii(text: str) -> bool:
    """返回 ``redact`` 是否会改动该文本。"""
    return redact(text) != text


def store_content(text: str | None, limit: int = 4_000) -> str | None:
    """应用已配置的 TRACE_STORE_CONTENT 策略，并限制长度。"""
    mode = settings.TRACE_STORE_CONTENT.lower()
    if mode == "none" or text is None:
        return None
    value = text if mode == "full" else redact(text)
    return value[:limit]


def hash_user(user_id: str) -> str:
    """返回稳定的带密钥哈希，使 trace 中不会出现原始用户 ID。"""
    secret = settings.TRACE_HASH_SECRET or settings.NEXRAG_USER_JWT_SECRET or "nexrag-local"
    digest = hmac.new(secret.encode(), user_id.encode(), hashlib.sha256).hexdigest()
    return digest[:32]
