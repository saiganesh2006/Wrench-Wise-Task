"""AI Gateway / Control Plane (#3): input & output guardrails applied around every LLM call."""
from __future__ import annotations

import re

PII_PATTERNS = {
    "AADHAAR": re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}\b"),
    "PAN": re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
    "CARD_NUMBER": re.compile(r"\b(?:\d[ -]?){15,16}\b"),
    "OTP/PIN": re.compile(r"\b(?:otp|pin|cvv)\b\D{0,12}\d{3,6}", re.I),
}

INJECTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above) (instructions|rules)",
    r"disregard (the )?(system|previous) (prompt|instructions)",
    r"reveal (your |the )?(system )?prompt",
    r"you are now (?:dan|an? unrestricted)",
    r"act as .* without (restrictions|rules)",
    r"bypass (the )?(approval|guardrail|policy|limit)",
]

OFF_TOPIC = [r"\b(write (me )?(a )?(poem|story|essay))\b", r"\b(hack|exploit|launder)\b"]


def check_input(text: str) -> dict:
    """Returns {allowed, text (PII-masked), findings[]}."""
    findings, safe = [], text
    for label, pat in PII_PATTERNS.items():
        if pat.search(safe):
            findings.append(f"PII detected & masked: {label}")
            safe = pat.sub(f"[{label} REDACTED]", safe)
    for p in INJECTION_PATTERNS:
        if re.search(p, text, re.I):
            return dict(allowed=False, text=safe, findings=findings + ["prompt-injection attempt blocked"],
                        reply="I can't help with that request. I follow the bank's security policies and can't "
                              "bypass them. I can help with balances, transactions, fraud checks, loans or transfers.")
    for p in OFF_TOPIC:
        if re.search(p, text, re.I):
            return dict(allowed=False, text=safe, findings=findings + ["off-topic / unsafe request blocked"],
                        reply="That's outside what I can help with here. I'm a banking assistant - try asking about your "
                              "accounts, spending, suspicious activity, loans or transfers.")
    return dict(allowed=True, text=safe, findings=findings)


def check_output(text: str) -> str:
    """Mask anything that looks like a full card/account number in model output."""
    return PII_PATTERNS["CARD_NUMBER"].sub("[REDACTED]", text)
