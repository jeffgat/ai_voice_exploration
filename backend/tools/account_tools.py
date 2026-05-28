from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any


FAKE_CUSTOMER_ACCOUNTS: dict[str, dict[str, Any]] = {
    "demo_123": {
        "customer_id": "demo_123",
        "name": "Alex",
        "past_due_amount": 420.00,
        "minimum_payment_today": 50.00,
        "allowed_plan_options": [
            "pay full balance today",
            "pay 50 today and remainder in 14 days",
            "hardship callback escalation",
        ],
        "compliance_notes": [
            "do not threaten",
            "do not shame",
            "do not claim legal consequences",
            "do not ask for full card details",
            "do not process real payments",
            "offer human escalation if user is distressed or confused",
        ],
    }
}


def get_customer_account(customer_id: str) -> dict[str, Any]:
    account = FAKE_CUSTOMER_ACCOUNTS.get(customer_id)
    if not account:
        raise ValueError(f"Unknown demo customer_id: {customer_id}")
    return dict(account)


def get_allowed_payment_plans(customer_id: str) -> list[dict[str, Any]]:
    account = get_customer_account(customer_id)
    today = datetime.now(UTC).date()
    return [
        {
            "plan_id": "full_today",
            "label": "Pay full balance today",
            "amount_today": account["past_due_amount"],
            "remainder_due": 0.00,
            "due_date": today.isoformat(),
        },
        {
            "plan_id": "min_then_remainder",
            "label": "Pay $50 today and the remainder in 14 days",
            "amount_today": account["minimum_payment_today"],
            "remainder_due": round(account["past_due_amount"] - account["minimum_payment_today"], 2),
            "due_date": (today + timedelta(days=14)).isoformat(),
        },
        {
            "plan_id": "hardship_callback",
            "label": "Hardship callback escalation",
            "amount_today": 0.00,
            "remainder_due": account["past_due_amount"],
            "due_date": None,
        },
    ]


def create_payment_plan(
    state: Any,
    customer_id: str,
    amount: float,
    date: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Store a demo payment arrangement in the in-memory session state.

    Idempotency is keyed by the user utterance and plan shape. Replaying the
    same turn returns the existing plan instead of creating duplicates.
    """

    if not hasattr(state, "payment_plans_by_key"):
        raise TypeError("state must expose payment_plans_by_key")

    if idempotency_key in state.payment_plans_by_key:
        plan = state.payment_plans_by_key[idempotency_key]
        return {**plan, "duplicate": True}

    account = get_customer_account(customer_id)
    amount_decimal = Decimal(str(amount))
    minimum = Decimal(str(account["minimum_payment_today"]))
    full_balance = Decimal(str(account["past_due_amount"]))
    if amount_decimal < minimum and amount_decimal < full_balance:
        raise ValueError("The demo plan amount is below the allowed minimum payment.")

    plan = {
        "plan_id": f"plan_{len(state.payment_plans_by_key) + 1}",
        "customer_id": customer_id,
        "amount": float(amount_decimal),
        "date": date,
        "status": "demo_created",
        "created_at": datetime.now(UTC).isoformat(),
        "duplicate": False,
        "note": "Demo-only record. No payment method was collected or charged.",
    }
    state.payment_plans_by_key[idempotency_key] = plan
    state.created_payment_plans.append(plan)
    return plan


def escalate_to_human(state: Any, reason: str) -> dict[str, Any]:
    state.escalated = True
    state.escalation_reason = reason
    return {
        "status": "queued",
        "reason": reason,
        "message": "Demo human escalation queued. No external contact center was called.",
    }


def end_call(state: Any, summary: str) -> dict[str, Any]:
    state.ended = True
    state.call_summary = summary
    return {"status": "ended", "summary": summary}

