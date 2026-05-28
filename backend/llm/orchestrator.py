from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable

import httpx

from backend.guardrails.policy import validate_agent_response
from backend.settings import Settings
from backend.tools.account_tools import (
    create_payment_plan,
    end_call,
    escalate_to_human,
    get_allowed_payment_plans,
    get_customer_account,
)


ToolLogger = Callable[[str, dict[str, Any], dict[str, Any], int, str], Awaitable[None]]


@dataclass(slots=True)
class AgentTurnResult:
    text: str
    intent: str
    llm_latency_ms: int
    guardrail_reasons: list[str] = field(default_factory=list)
    interrupted: bool = False


class LLMOrchestrator:
    """Stateful agent loop for one demo voice session.

    A turn is deliberately split into steps:
    1. classify the customer's latest utterance
    2. call deterministic fake tools when needed
    3. ask the LLM to compose the next concise response
    4. run guardrails before TTS
    5. emit tool/decision logs for the UI
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def bootstrap_session(self, state: Any, log_tool_call: ToolLogger) -> None:
        started = time.perf_counter()
        account = get_customer_account(state.customer_id)
        plans = get_allowed_payment_plans(state.customer_id)
        state.account = account
        state.allowed_payment_plans = plans
        await log_tool_call(
            "get_customer_account",
            {"customer_id": state.customer_id},
            account,
            int((time.perf_counter() - started) * 1000),
            "success",
        )
        await log_tool_call(
            "get_allowed_payment_plans",
            {"customer_id": state.customer_id},
            {"plans": plans},
            0,
            "success",
        )

    async def initial_response(self, state: Any, log_tool_call: ToolLogger) -> AgentTurnResult:
        if not state.account:
            await self.bootstrap_session(state, log_tool_call)

        text = (
            f"Hi Alex, this is a demo assistant for a local voice-agent simulation. "
            f"I am not a real debt collector and cannot process payments. "
            f"The demo account shows a past-due balance of ${state.account['past_due_amount']:.2f}. "
            f"Would you like to hear the available demo payment options?"
        )
        guarded = validate_agent_response(text, state)
        await log_tool_call(
            "guardrail_validate",
            {"text": text},
            {"blocked": guarded.blocked, "reasons": guarded.reasons, "rewrites": guarded.rewrites},
            0,
            "success",
        )
        return AgentTurnResult(text=guarded.text, intent="opening", llm_latency_ms=0, guardrail_reasons=guarded.reasons)

    async def handle_user_transcript(
        self,
        user_text: str,
        state: Any,
        log_tool_call: ToolLogger,
    ) -> AgentTurnResult:
        started = time.perf_counter()
        intent_info = await self._classify_intent(user_text, state)
        await log_tool_call(
            "classify_customer_intent",
            {"utterance": user_text},
            intent_info,
            int((time.perf_counter() - started) * 1000),
            "success",
        )

        tool_results: list[dict[str, Any]] = []
        tool_started = time.perf_counter()
        intent = intent_info.get("intent", "other")

        if intent in {"cannot_pay", "asks_options", "other"} and not state.allowed_payment_plans:
            plans = get_allowed_payment_plans(state.customer_id)
            state.allowed_payment_plans = plans
            tool_results.append({"tool": "get_allowed_payment_plans", "result": plans})
            await log_tool_call(
                "get_allowed_payment_plans",
                {"customer_id": state.customer_id},
                {"plans": plans},
                int((time.perf_counter() - tool_started) * 1000),
                "success",
            )

        if intent in {"angry", "distressed", "confused", "human"}:
            reason = intent_info.get("reason") or f"Customer intent classified as {intent}."
            result = escalate_to_human(state, reason)
            tool_results.append({"tool": "escalate_to_human", "result": result})
            await log_tool_call(
                "escalate_to_human",
                {"reason": reason},
                result,
                int((time.perf_counter() - tool_started) * 1000),
                "success",
            )

        if intent == "agrees_to_plan":
            amount = self._extract_amount(user_text) or float(state.account.get("minimum_payment_today", 50.0))
            date = self._extract_iso_date(user_text) or (datetime.now(UTC).date() + timedelta(days=14)).isoformat()
            key = f"{state.session_id}:{state.last_user_event_id}:{amount}:{date}"
            try:
                result = create_payment_plan(state, state.customer_id, amount, date, key)
                status = "success"
            except Exception as exc:
                result = {"error": str(exc)}
                status = "error"
            tool_results.append({"tool": "create_payment_plan", "result": result})
            await log_tool_call(
                "create_payment_plan",
                {"customer_id": state.customer_id, "amount": amount, "date": date, "idempotency_key": key},
                result,
                int((time.perf_counter() - tool_started) * 1000),
                status,
            )

        if intent == "end_call":
            summary = f"Customer said: {user_text}"
            result = end_call(state, summary)
            tool_results.append({"tool": "end_call", "result": result})
            await log_tool_call(
                "end_call",
                {"summary": summary},
                result,
                int((time.perf_counter() - tool_started) * 1000),
                "success",
            )

        llm_started = time.perf_counter()
        response_text = await self._generate_response(user_text, intent_info, tool_results, state)
        llm_latency_ms = int((time.perf_counter() - llm_started) * 1000)
        guarded = validate_agent_response(response_text, state)
        await log_tool_call(
            "guardrail_validate",
            {"text": response_text},
            {"blocked": guarded.blocked, "reasons": guarded.reasons, "rewrites": guarded.rewrites},
            0,
            "success",
        )
        state.decision_log.append(
            {
                "at": datetime.now(UTC).isoformat(),
                "user_text": user_text,
                "intent": intent_info,
                "tool_results": tool_results,
                "guardrails": guarded.reasons,
            }
        )
        return AgentTurnResult(
            text=guarded.text,
            intent=intent,
            llm_latency_ms=llm_latency_ms,
            guardrail_reasons=guarded.reasons,
        )

    async def _classify_intent(self, user_text: str, state: Any) -> dict[str, Any]:
        fallback = self._keyword_intent(user_text)
        if not self.settings.llm_api_key:
            return {**fallback, "source": "keyword_fallback_no_llm_key"}

        messages = [
            {
                "role": "system",
                "content": (
                    "Classify a customer's utterance for a demo payment-arrangement voice agent. "
                    "Return only compact JSON with keys intent, confidence, reason. "
                    "Allowed intents: cannot_pay, asks_options, agrees_to_plan, angry, distressed, "
                    "confused, human, end_call, ambiguous, other."
                ),
            },
            {"role": "user", "content": user_text},
        ]
        try:
            raw = await self._chat_completion(messages, temperature=0.0, max_tokens=120)
            parsed = self._parse_json_object(raw)
            if parsed.get("intent"):
                return {**fallback, **parsed, "source": "llm"}
        except Exception:
            return {**fallback, "source": "keyword_fallback_llm_error"}
        return {**fallback, "source": "keyword_fallback_unparseable"}

    async def _generate_response(
        self,
        user_text: str,
        intent_info: dict[str, Any],
        tool_results: list[dict[str, Any]],
        state: Any,
    ) -> str:
        if not self.settings.llm_api_key:
            return self._fallback_response(intent_info, state)

        history = [
            {"role": message["role"], "content": message["text"]}
            for message in state.conversation[-10:]
            if message["role"] in {"user", "assistant"}
        ]
        system = {
            "role": "system",
            "content": (
                "You are a calm demo-only voice assistant for a proof-of-concept AI phone-call agent. "
                "You are not a real debt collector. Do not threaten, shame, claim legal consequences, "
                "or collect payment card details. Do not process real payments. Ask one question at a time. "
                "Be concise enough for spoken audio. If the customer cannot pay, offer either the $50 today "
                "plus remainder in 14 days option or hardship callback escalation. If distressed or confused, "
                "offer human escalation. If transcription is ambiguous, ask for clarification."
            ),
        }
        context = {
            "role": "user",
            "content": json.dumps(
                {
                    "latest_customer_utterance": user_text,
                    "classified_intent": intent_info,
                    "fake_account": state.account,
                    "allowed_payment_plans": state.allowed_payment_plans,
                    "tool_results": tool_results,
                    "session_flags": {
                        "escalated": state.escalated,
                        "ended": state.ended,
                        "created_payment_plans": state.created_payment_plans,
                    },
                },
                default=str,
            ),
        }

        try:
            return await self._chat_completion(
                [system, *history, context],
                temperature=0.25,
                max_tokens=180,
            )
        except Exception:
            return self._fallback_response(intent_info, state)

    async def _chat_completion(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        max_attempts: int = 3,
    ) -> str:
        if not self.settings.llm_api_key:
            raise RuntimeError("OPENROUTER_API_KEY or OPENAI_API_KEY is not set.")

        url = self.settings.openai_base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.settings.openai_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:5173",
            "X-Title": "Realtime Voice Agent PoC",
        }
        delay = 0.4
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(url, json=payload, headers=headers)
                    response.raise_for_status()
                    data = response.json()
                    return data["choices"][0]["message"]["content"].strip()
            except Exception as exc:
                last_error = exc
                if attempt == max_attempts:
                    break
                await asyncio.sleep(delay)
                delay *= 2
        assert last_error is not None
        raise last_error

    def _keyword_intent(self, user_text: str) -> dict[str, Any]:
        text = user_text.lower()
        if any(word in text for word in ["angry", "mad", "furious", "ridiculous", "harassing"]):
            return {"intent": "angry", "confidence": 0.72, "reason": "anger keyword"}
        if any(word in text for word in ["stressed", "scared", "anxious", "overwhelmed", "can't deal"]):
            return {"intent": "distressed", "confidence": 0.72, "reason": "distress keyword"}
        if any(word in text for word in ["human", "person", "representative", "manager"]):
            return {"intent": "human", "confidence": 0.8, "reason": "human escalation requested"}
        if any(phrase in text for phrase in ["can't pay", "cannot pay", "no money", "not able", "unable to pay"]):
            return {"intent": "cannot_pay", "confidence": 0.76, "reason": "cannot pay keyword"}
        if any(word in text for word in ["options", "plan", "arrangement", "minimum"]):
            return {"intent": "asks_options", "confidence": 0.7, "reason": "payment options keyword"}
        if any(word in text for word in ["yes", "agree", "ok", "okay", "works", "do that", "sounds good"]):
            return {"intent": "agrees_to_plan", "confidence": 0.66, "reason": "agreement keyword"}
        if any(word in text for word in ["bye", "goodbye", "end call", "stop"]):
            return {"intent": "end_call", "confidence": 0.82, "reason": "ending keyword"}
        if len(text.split()) <= 2:
            return {"intent": "ambiguous", "confidence": 0.45, "reason": "very short utterance"}
        return {"intent": "other", "confidence": 0.5, "reason": "no keyword match"}

    def _fallback_response(self, intent_info: dict[str, Any], state: Any) -> str:
        intent = intent_info.get("intent")
        if intent == "cannot_pay":
            return (
                "I understand. In this demo, I can offer a $50 payment today with the remainder "
                "due in 14 days, or I can mark a hardship callback. Which option would you prefer?"
            )
        if intent in {"angry", "distressed", "confused", "human"}:
            return (
                "I hear that this may be frustrating. Since this is a demo, I can mark the session "
                "for human escalation instead of continuing. Would you like me to do that?"
            )
        if intent == "agrees_to_plan":
            return (
                "Thanks. I created a demo-only payment arrangement record. No real payment was taken "
                "and no card details were collected. Is there anything else you want to test?"
            )
        if intent == "end_call":
            return "Understood. I will end the demo call now. Thank you for testing the voice-agent flow."
        if intent == "ambiguous":
            return "I may not have heard that clearly. Could you say that another way for the demo?"
        return (
            f"For this demo account, the balance is ${state.account.get('past_due_amount', 420.0):.2f}. "
            "The available options are paying the full demo balance today, paying $50 today and the rest "
            "in 14 days, or requesting a hardship callback. Which would you like to explore?"
        )

    def _parse_json_object(self, raw: str) -> dict[str, Any]:
        raw = raw.strip()
        match = re.search(r"\{.*\}", raw, re.S)
        if match:
            raw = match.group(0)
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}

    def _extract_amount(self, text: str) -> float | None:
        amount_match = re.search(r"\$?\b(\d+(?:\.\d{1,2})?)\b", text)
        return float(amount_match.group(1)) if amount_match else None

    def _extract_iso_date(self, text: str) -> str | None:
        match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
        return match.group(1) if match else None
