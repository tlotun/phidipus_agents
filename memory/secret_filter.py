# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
memory/secret_filter.py — keep credentials out of long-term memory (v4.3)
═══════════════════════════════════════════════════════════════════════════

Long-term memory is injected into future prompts and can be exported, so it
must never hold API keys, tokens, passwords, OTP codes or card numbers.
``scrub(text)`` returns the text with secrets replaced by ``[ĐÃ ẨN]`` plus
the list of finding labels; ``is_mostly_secret`` tells the caller to refuse
storing a memory whose useful content was only the secret.
"""
from __future__ import annotations

import re

REDACTED = "[ĐÃ ẨN]"

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S)),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("telegram_token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}")),
    ("url_credentials", re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:[^\s@/]+@")),
    ("generic_secret", re.compile(
        r"(?i)\b(?:api[_ -]?key|secret|token|access[_ -]?key|client[_ -]?secret)\b\s*[:=]\s*\S{8,}")),
    # strong keywords may be followed by a few words ("mật khẩu wifi nhà: …")
    ("password", re.compile(
        r"(?i)\b(?:password|passwd|passcode|mật\s*khẩu|mat\s*khau)\b(?:\s+[^\s:=]+){0,3}?\s*(?:[:=]|là|la|is)\s*\S+")),
    # weak/short keywords need an explicit ':' or '=' right after them
    ("password", re.compile(r"(?i)\b(?:pwd|pass|mk|pw)\s*[:=]\s*\S+")),
    ("otp", re.compile(r"(?i)\b(?:otp|mã\s*xác\s*(?:thực|nhận)|ma\s*xac\s*(?:thuc|nhan)|mã\s*otp|pin|cvv|cvc)\b\D{0,12}\d{3,8}\b")),
    ("national_id", re.compile(r"(?i)\b(?:cccd|cmnd|căn\s*cước|can\s*cuoc|passport|hộ\s*chiếu|ho\s*chieu)\b\D{0,15}[A-Z]?\d{8,12}\b")),
]

_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def scrub(text: str) -> tuple[str, list[str]]:
    """Return (text with secrets redacted, list of finding labels)."""
    out = str(text or "")
    found: list[str] = []
    for label, pat in _PATTERNS:
        if pat.search(out):
            found.append(label)
            out = pat.sub(REDACTED, out)

    def _card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            found.append("card_number")
            return REDACTED
        return m.group(0)

    out = _CARD_RE.sub(_card, out)
    return out, sorted(set(found))


def is_mostly_secret(original: str, scrubbed: str) -> bool:
    """True when little meaningful text remains after redaction."""
    rest = re.sub(r"\s+", " ", scrubbed.replace(REDACTED, " ")).strip()
    words = re.findall(r"\w+", rest)
    return len(words) < 3 or len(rest) < max(8, int(len(original) * 0.25))
