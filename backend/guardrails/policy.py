from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class GuardrailResult:
    text: str
    blocked: bool = False
    reasons: list[str] = field(default_factory=list)
    rewrites: list[str] = field(default_factory=list)


BLOCKED_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r"\b(we|i|they)\s+(will|can|could)\s+(sue|garnish|arrest|prosecute)\b", re.I),
        "legal_threat",
        "I cannot make legal threats or claims in this demo.",
    ),
    (
        re.compile(r"\b(legal action|lawsuit|wage garnishment|jail|arrest)\b", re.I),
        "unsupported_legal_claim",
        "I cannot discuss legal consequences in this demo.",
    ),
    (
        re.compile(r"\b(deadbeat|irresponsible|shame|embarrassed|bad person|failure)\b", re.I),
        "shame_language",
        "I want to keep this respectful and focused on options.",
    ),
    (
        re.compile(r"\b(full card number|credit card number|debit card number|cvv|cvc|security code)\b", re.I),
        "payment_card_request",
        "I cannot collect card details or process payments in this demo.",
    ),
]


def _mentions_unsupported_amount(text: str, state: Any) -> bool:
    """Flag amounts that are not present in the fake account state.

    This intentionally stays simple for the PoC. A production system would use
    structured response plans instead of scanning final text.
    """

    account = getattr(state, "account", {}) or {}
    known_amounts = {
        f"{float(account.get('past_due_amount', 0)):.2f}",
        f"{float(account.get('minimum_payment_today', 0)):.2f}",
    }
    amounts = re.findall(r"\$?\b(\d{2,5}(?:\.\d{2})?)\b", text)
    return any(amount not in known_amounts and amount not in {"14"} for amount in amounts)


def validate_agent_response(text: str, state: Any) -> GuardrailResult:
    """Validate and lightly rewrite an agent response before TTS.

    The PoC uses regex/string checks because the point is to show the policy
    hook in the architecture. Keep the function deterministic and easy to test.
    """

    result = GuardrailResult(text=text.strip())
    rewritten_sentences: list[str] = []

    for pattern, reason, replacement in BLOCKED_PATTERNS:
        if pattern.search(result.text):
            result.blocked = True
            result.reasons.append(reason)
            result.text = pattern.sub(replacement, result.text)
            rewritten_sentences.append(replacement)

    if _mentions_unsupported_amount(result.text, state):
        result.blocked = True
        result.reasons.append("unsupported_amount")
        result.rewrites.append("Removed unsupported amount claim.")
        account = getattr(state, "account", {}) or {}
        past_due = account.get("past_due_amount", 420.00)
        minimum = account.get("minimum_payment_today", 50.00)
        result.text = (
            "For this demo account, I can only reference the known balance "
            f"of ${float(past_due):.2f} and the minimum option of ${float(minimum):.2f} today. "
            "Would one of the listed demo options work for you?"
        )

    if rewritten_sentences:
        result.rewrites.extend(rewritten_sentences)

    if "demo" not in result.text.lower():
        result.text = f"{result.text} Reminder: this is only a demo and no real payment is processed."
        result.rewrites.append("Added demo-only reminder.")

    return result

