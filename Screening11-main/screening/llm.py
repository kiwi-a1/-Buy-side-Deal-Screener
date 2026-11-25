"""AI engine with automatic failover across providers.

Providers are tried in the order given. A provider that is slow, rate-limited, out of quota, rejecting the key or erroring is
skipped and put on a short cool-down (shared across screenings via `Health`), so the next call does not wait on it again.
Every switch is recorded in `events` so the report can say what happened. A reply that is not valid JSON is asked for again once
on the same provider, then the next provider is tried."""
from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from typing import Callable, Optional, Type, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .providers import Candidate, ProviderError, complete

T = TypeVar("T", bound=BaseModel)

CHARS_PER_TOKEN = 3.5       # rough; used only to budget before a call - real usage is recorded after
MAX_INPUT_CHARS = int(os.getenv("MAX_INPUT_CHARS") or 48_000)   # how much source text one call carries when the provider has no tight token limit
COOLDOWN = {"rate": 60, "quota": 3600, "auth": 86400, "timeout": 120, "network": 60, "server": 90, "internal": 120,
            "not_found": 600, "bad_request": 300, "too_large": 300, "format": 0}
MAX_WAIT = 90               # longest we will sit and wait for a rate-limited provider when nothing else is available
WHY = {"auth": "the API key was rejected", "quota": "out of quota or credit", "rate": "rate limited", "timeout": "too slow",
       "network": "could not be reached", "server": "a server error", "not_found": "the model was not available", "bad_request": "rejected the request",
       "too_large": "the request was too large", "internal": "an unexpected error", "format": "returned an invalid answer"}


class LLMError(RuntimeError):
    """Raised with a message that is safe and useful to show to the user."""


class Pacer:
    """Sliding 60-second token budget (only used for providers with a tight per-minute limit, e.g. the Groq free tier)."""

    def __init__(self, limit: int, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.limit, self.clock, self.sleep = limit, clock, sleep
        self.events: deque[tuple[float, int]] = deque()

    def wait(self, need: int, on_wait: Optional[Callable[[float], None]] = None) -> None:
        while True:
            now = self.clock()
            while self.events and now - self.events[0][0] >= 60:
                self.events.popleft()
            used = sum(t for _, t in self.events)
            if not self.events or used + need <= self.limit * 0.9:      # an empty window always goes ahead
                return
            delay = max(60 - (now - self.events[0][0]) + 0.5, 0.5)
            if on_wait:
                on_wait(delay)
            self.sleep(delay)

    def record(self, tokens: int) -> None:
        self.events.append((self.clock(), tokens))


class Health:
    """Which providers are cooling down and why. One instance is shared by all screenings in the process."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock, self.until, self.reason = clock, {}, {}

    def cooling(self, c: Candidate) -> bool:
        return self.until.get(c.id, 0) > self.clock()

    def remaining(self, c: Candidate) -> float:
        return max(self.until.get(c.id, 0) - self.clock(), 0)

    def mark(self, c: Candidate, kind: str, retry_after: Optional[float] = None) -> None:
        seconds = min(retry_after, 600) if kind == "rate" and retry_after else COOLDOWN.get(kind, 60)
        if seconds:
            self.until[c.id], self.reason[c.id] = self.clock() + seconds, kind

    def clear(self, c: Optional[Candidate] = None) -> None:
        if c is None:
            self.until.clear(); self.reason.clear()
        else:
            self.until.pop(c.id, None); self.reason.pop(c.id, None)

    def status(self, c: Candidate) -> dict:
        return {"cooling": self.cooling(c), "reason": self.reason.get(c.id) if self.cooling(c) else None, "seconds": round(self.remaining(c))}


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


class _Invalid(Exception):
    pass


class LLM:
    def __init__(self, candidates: list[Candidate], on_status: Callable[[str], None] = lambda m: None,
                 sleep: Callable[[float], None] = time.sleep, http: Optional[httpx.Client] = None, health: Optional[Health] = None,
                 timeout: Optional[float] = None, soft_timeout: Optional[float] = None, clock: Callable[[], float] = time.monotonic):
        if not candidates:
            raise LLMError("No AI key is set up yet. Add one under AI keys (Anthropic, OpenAI, Gemini, OpenRouter, Groq or any OpenAI-compatible service).")
        self.candidates, self.on_status, self.sleep = candidates, on_status, sleep
        self.http = http or httpx.Client()
        self.clock = clock
        self.health = health or Health(clock)
        self.timeout = float(timeout or os.getenv("LLM_TIMEOUT") or 120)                      # the last provider left gets all of this
        self.soft_timeout = float(soft_timeout or os.getenv("LLM_SOFT_TIMEOUT") or 60)       # a provider with a fallback behind it gets less
        self.calls = 0
        self.last_model = ""
        self.events: list[dict] = []     # provider switches, for the report
        self.used: dict[str, int] = {}   # provider/model -> calls that succeeded
        self._pacers: dict = {}

    # ---------- sizing ----------
    @property
    def primary(self) -> Candidate:
        return next((c for c in self.candidates if not self.health.cooling(c)), self.candidates[0])

    def budget_chars(self, max_tokens: int, overhead_chars: int) -> int:
        """How many characters of source text one call can carry: bounded by the first provider's per-minute token limit, if it has one."""
        c = self.primary
        if c.tpm:
            usable = int(c.tpm * 0.9) - max_tokens
            return max(int(usable * CHARS_PER_TOKEN) - overhead_chars, 1000)
        return max(MAX_INPUT_CHARS - overhead_chars, 4000)

    # ---------- main entry ----------
    def json(self, system: str, user: str, schema: Type[T], max_tokens: int = 1800, max_providers: Optional[int] = None,
             reasoning: str = "low") -> T:
        """Return a validated `schema` object. max_providers: optional steps try fewer providers so they cannot burn minutes."""
        failures: list[str] = []
        for round_ in range(2):
            tried = 0
            for i, cand in enumerate(self.candidates):
                if max_providers is not None and tried >= max_providers:
                    break
                if self.health.cooling(cand):
                    continue
                tried += 1
                later = any(not self.health.cooling(c) for c in self.candidates[i + 1:])
                try:
                    return self._attempt(cand, system, user, schema, max_tokens, reasoning, self.soft_timeout if later else self.timeout)
                except ProviderError as e:
                    self.health.mark(cand, e.kind, e.retry_after)
                    why, detail = WHY.get(e.kind, e.kind), str(e)
                except _Invalid as e:
                    self.health.mark(cand, "format")
                    why, detail = WHY["format"], str(e)
                failures.append(f"{cand.name}: {why}")
                nxt = next((c for c in self.candidates[i + 1:] if not self.health.cooling(c)), None)
                if nxt:
                    self.events.append({"from": cand.name, "to": nxt.name, "reason": why, "detail": detail[:160]})
                    self.on_status(f"{cand.label} {why}; switching to {nxt.label}")
            # A whole pass produced no answer. A rate limit says exactly how long to wait, so if the soonest one is back within
            # a short wait, sit it out once and try again. Other failures (bad key, no credit, server errors) are not worth waiting for.
            cooling = [self.health.remaining(c) for c in self.candidates if self.health.cooling(c) and self.health.reason.get(c.id) == "rate"]
            wait = min(cooling, default=None)
            if wait is None or wait > MAX_WAIT or round_:
                break
            self.on_status(f"All AI providers are busy; waiting {wait:.0f}s")
            self.sleep(wait + 0.5)
            for c in self.candidates:
                if self.health.cooling(c) and self.health.remaining(c) <= wait + 1:
                    self.health.clear(c)                      # the wait is over (clears it explicitly so a mocked clock cannot strand it)
        if not failures:
            failures = [f"{c.name}: {WHY.get(self.health.reason.get(c.id, ''), 'unavailable')} (retry in {self.health.remaining(c):.0f}s)" for c in self.candidates]
        raise LLMError("No AI provider could answer. " + " | ".join(failures[:6]) + ". Check your keys under AI keys, or try again in a minute.")

    # ---------- one provider ----------
    def _attempt(self, cand: Candidate, system: str, user: str, schema: Type[T], max_tokens: int, reasoning: str, timeout: float) -> T:
        messages = [{"role": "user", "content": user}]
        for repair in (False, True):
            text = self._call(cand, system, messages, max_tokens, reasoning, timeout)
            try:
                result = schema.model_validate(parse_json(text))
                self.used[cand.name] = self.used.get(cand.name, 0) + 1
                self.last_model = cand.name
                return result
            except (ValueError, ValidationError) as e:
                if repair:
                    raise _Invalid(f"reply did not match the expected format ({str(e)[:140]})")
                messages += [{"role": "assistant", "content": text[:3000] or "{}"},
                             {"role": "user", "content": f"That reply was invalid: {str(e)[:500]}. Return ONLY the corrected JSON object."}]
        raise AssertionError("unreachable")

    def _call(self, cand: Candidate, system: str, messages: list, max_tokens: int, reasoning: str, timeout: float) -> str:
        need = int(sum(len(m["content"]) for m in messages) / CHARS_PER_TOKEN) + max_tokens
        pacer = None
        if cand.tpm:
            pacer = self._pacers.setdefault(cand.key_id, Pacer(cand.tpm, clock=self.clock, sleep=self.sleep))
            pacer.wait(need, on_wait=lambda d: self.on_status(f"Free-tier pacing ({cand.label}): waiting {d:.0f}s for token budget..."))
        text, tokens = complete(self.http, cand, system, messages, max_tokens, reasoning, timeout)
        self.calls += 1
        if pacer:
            pacer.record(tokens or need)
        if not text.strip():
            raise ProviderError("server", "empty reply (the model may have used all tokens on reasoning)")
        return text
