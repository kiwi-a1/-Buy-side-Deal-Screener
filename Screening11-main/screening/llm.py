"""Groq (free tier) client that returns validated JSON.

Free-tier limit is ~8,000 tokens per minute, so calls are paced: before each call we wait until the
last 60 seconds have enough token budget left. 429s are retried, and if a model keeps failing we fall
back to the next one in GROQ_MODELS.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from typing import Callable, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODELS = "openai/gpt-oss-120b,openai/gpt-oss-20b,qwen/qwen3.8-27b"
CHARS_PER_TOKEN = 3.5  # rough; used only to budget before a call - real usage is recorded after


class LLMError(RuntimeError):
    """Raised with a message that is safe and useful to show to the user."""


class Pacer:
    """Sliding 60-second token budget."""

    def __init__(self, limit: int, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.limit, self.clock, self.sleep = limit, clock, sleep
        self.events: deque[tuple[float, int]] = deque()

    def wait(self, need: int, on_wait: Optional[Callable[[float], None]] = None) -> None:
        while True:
            now = self.clock()
            while self.events and now - self.events[0][0] >= 60:
                self.events.popleft()
            used = sum(t for _, t in self.events)
            # an empty window always goes ahead, even if one call alone is above the budget
            if not self.events or used + need <= self.limit * 0.9:
                return
            delay = max(60 - (now - self.events[0][0]) + 0.5, 0.5)
            if on_wait:
                on_wait(delay)
            self.sleep(delay)

    def record(self, tokens: int) -> None:
        self.events.append((self.clock(), tokens))


def parse_json(text: str) -> dict:
    """Parse a model reply that should be JSON, tolerating code fences and surrounding prose."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("reply contained no JSON object")
        data = json.loads(t[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("reply JSON was not an object")
    return data


class LLM:
    def __init__(self, api_key: Optional[str] = None, models: Optional[str] = None, tpm: Optional[int] = None,
                 client=None, on_status: Callable[[str], None] = lambda m: None,
                 sleep: Callable[[float], None] = time.sleep):
        self.models = [m.strip() for m in (models or os.getenv("GROQ_MODELS") or DEFAULT_MODELS).split(",") if m.strip()]
        self.tpm = int(tpm or os.getenv("GROQ_TPM_LIMIT") or 8000)
        self.on_status, self.sleep = on_status, sleep
        self.pacer = Pacer(self.tpm, sleep=sleep)
        self.calls = 0
        self.last_model = ""
        if client is None:
            key = api_key or os.getenv("GROQ_API_KEY")
            if not key:
                raise LLMError("GROQ_API_KEY is not set. Copy .env.example to .env and paste your free key from console.groq.com.")
            from groq import Groq
            client = Groq(api_key=key, max_retries=0)  # retries are handled here, with pacing
        self.client = client

    def budget_chars(self, max_tokens: int, overhead_chars: int) -> int:
        """How many characters of source text fit in one call under the per-minute limit."""
        usable = int(self.tpm * 0.9) - max_tokens
        return max(int(usable * CHARS_PER_TOKEN) - overhead_chars, 1000)

    def json(self, system: str, user: str, schema: Type[T], max_tokens: int = 1800, max_models: Optional[int] = None,
             reasoning: str = "low") -> T:
        """max_models: optional steps try fewer models, so a failing step doesn't burn minutes of free-tier budget."""
        errors: list[str] = []
        for model in self.models[:max_models]:
            try:
                return self._one_model(model, system, user, schema, max_tokens, reasoning)
            except LLMError as e:
                if getattr(e, "fatal", False):
                    raise
                errors.append(f"{model}: {e}")
                self.on_status(f"{model} failed ({e}); trying next model")
        raise LLMError("All models failed - " + " | ".join(errors))

    def _one_model(self, model, system, user, schema, max_tokens, reasoning="low") -> T:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for repair in (False, True):
            reply = self._call(model, messages, max_tokens, reasoning)
            try:
                return schema.model_validate(parse_json(reply))
            except (ValueError, ValidationError) as e:
                if repair:
                    raise LLMError(f"reply did not match the expected format ({str(e)[:160]})")
                messages += [{"role": "assistant", "content": reply[:3000]},
                             {"role": "user", "content": f"That reply was invalid: {str(e)[:500]}. "
                                                         "Return ONLY the corrected JSON object."}]
        raise AssertionError("unreachable")

    def _call(self, model: str, messages: list, max_tokens: int, reasoning: str = "low") -> str:
        need = int(sum(len(m["content"]) for m in messages) / CHARS_PER_TOKEN) + max_tokens
        kwargs = dict(model=model, messages=messages, temperature=0.1, max_tokens=max_tokens,
                      response_format={"type": "json_object"})
        if model.startswith("openai/gpt-oss"):
            kwargs["reasoning_effort"] = reasoning  # reasoning tokens count against the limit: "low" unless accuracy needs more
        last = "unknown error"
        for attempt in range(4):
            self.pacer.wait(need, on_wait=lambda d: self.on_status(f"Free-tier pacing: waiting {d:.0f}s for token budget..."))
            try:
                resp = self.client.chat.completions.create(**kwargs)
            except Exception as e:  # SDK errors carry status_code; connection errors do not
                status = getattr(e, "status_code", None)
                last = f"{type(e).__name__}: {str(e)[:200]}"
                if status in (401, 403):
                    err = LLMError("Groq rejected the API key. Check GROQ_API_KEY in .env.")
                    err.fatal = True
                    raise err
                network = status is None and any(w in type(e).__name__ for w in ("Connection", "Timeout"))
                if not (status == 429 or (status or 0) >= 500 or network):
                    raise LLMError(last)  # bad request, model gone, or a bug: retrying won't help - try the next model
                wait = 2.0 * (attempt + 1)
                if status == 429:
                    headers = getattr(getattr(e, "response", None), "headers", None) or {}
                    try:
                        wait = min(float(headers.get("retry-after", 20)), 65)
                    except (TypeError, ValueError):
                        wait = 20
                    self.on_status(f"Rate limited by Groq; retrying in {wait:.0f}s")
                self.sleep(wait)
                continue
            self.calls += 1
            self.last_model = model
            usage = getattr(resp, "usage", None)
            self.pacer.record(int(getattr(usage, "total_tokens", 0) or need))
            content = resp.choices[0].message.content or ""
            if not content.strip():
                raise LLMError("empty reply (the model may have used all tokens on reasoning)")
            return content
        raise LLMError(f"gave up after retries ({last})")
