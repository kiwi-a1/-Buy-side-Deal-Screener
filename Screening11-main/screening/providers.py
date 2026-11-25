"""How to talk to each AI service. Plain HTTPS (httpx), no vendor SDKs.

Three wire formats cover everything: OpenAI-style chat (OpenAI, Groq, OpenRouter, Mistral, DeepSeek, Together, xAI, local servers...),
Anthropic messages, and Google Gemini generateContent. Every failure is turned into a ProviderError with a `kind`, so the
failover engine (llm.py) can decide what to do without knowing which vendor it was."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

log = logging.getLogger("screening.providers")


class ProviderError(Exception):
    """kind: auth | quota | rate | timeout | network | server | not_found | bad_request | too_large | internal"""
    def __init__(self, kind: str, message: str, status: Optional[int] = None, retry_after: Optional[float] = None):
        super().__init__(message)
        self.kind, self.status, self.retry_after = kind, status, retry_after


# id -> how it works. `prefer` are regexes tried in order against the account's model list; `default` is used if listing fails.
CATALOG: dict[str, dict] = {
    "anthropic": {"label": "Anthropic (Claude)", "kind": "anthropic", "base_url": "https://api.anthropic.com", "env": ["ANTHROPIC_API_KEY"],
                  "default": "claude-sonnet-5-5", "prefer": [r"^claude-sonnet-5", r"^claude-haiku-5", r"^claude-sonnet", r"^claude-haiku"],
                  "hint": "Starts with sk-ant-", "needs_model": False, "needs_base_url": False, "rank": 0},
    "openai": {"label": "OpenAI", "kind": "openai", "base_url": "https://api.openai.com/v1", "env": ["OPENAI_API_KEY"],
               "default": "gpt-4o-mini", "prefer": [r"^gpt-5(\.\d+)?-mini$", r"^gpt-4\.1-mini$", r"^gpt-4o-mini$", r"^gpt-4o$"],
               "hint": "Starts with sk-", "needs_model": False, "needs_base_url": False, "rank": 1},
    "gemini": {"label": "Google Gemini", "kind": "gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta",
               "env": ["GEMINI_API_KEY", "GOOGLE_API_KEY"], "default": "gemini-2.0-flash",
               "prefer": [r"^gemini-[\d.]+-flash$", r"^gemini-flash-latest$", r"^gemini-[\d.]+-flash-lite$", r"^gemini-[\d.]+-pro$"],
               "hint": "Starts with AIza", "needs_model": False, "needs_base_url": False, "rank": 2},
    "openrouter": {"label": "OpenRouter", "kind": "openai", "base_url": "https://openrouter.ai/api/v1", "env": ["OPENROUTER_API_KEY"],
                   "default": "", "prefer": [], "hint": "Starts with sk-or-. Model is required, e.g. openai/gpt-4o-mini",
                   "needs_model": True, "needs_base_url": False, "rank": 3},
    "groq": {"label": "Groq (free tier)", "kind": "openai", "base_url": "https://api.groq.com/openai/v1", "env": ["GROQ_API_KEY"],
             "default": "openai/gpt-oss-120b", "prefer": [r"^openai/gpt-oss-120b$", r"^openai/gpt-oss-20b$"], "hint": "Starts with gsk_",
             "needs_model": False, "needs_base_url": False, "rank": 4, "tpm_env": "GROQ_TPM_LIMIT", "tpm_default": 8000,
             "fallback_models": ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]},
    "custom": {"label": "Other (OpenAI-compatible)", "kind": "openai", "base_url": "", "env": ["LLM_CUSTOM_API_KEY"], "default": "", "prefer": [],
               "hint": "Any service with an OpenAI-style /chat/completions API (Mistral, DeepSeek, Together, xAI, a local server...). Needs a base URL and a model.",
               "needs_model": True, "needs_base_url": True, "rank": 5},
}


@dataclass
class Candidate:
    """One (key, model) the engine may call."""
    provider: str
    label: str
    kind: str
    base_url: str
    api_key: str
    model: str
    key_id: Optional[int] = None
    tpm: Optional[int] = None          # tokens per minute the account allows (None = not limiting for us)
    extra: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.key_id}:{self.model}"

    @property
    def name(self) -> str:
        return f"{self.label} / {self.model}"


# ---------- errors ----------
def _message(r: httpx.Response) -> str:
    try:
        body = r.json()
        err = body.get("error", body) if isinstance(body, dict) else body
        if isinstance(err, list) and err:
            err = err[0]
        text = err.get("message") if isinstance(err, dict) else str(err)
        return str(text or r.text)[:240]
    except ValueError:
        return r.text[:240]


def error_from_response(r: httpx.Response) -> ProviderError:
    s, text = r.status_code, _message(r)
    low = text.lower()
    try:
        retry = float(r.headers.get("retry-after", ""))
    except ValueError:
        retry = None
    if s in (401, 403):
        kind = "auth"
    elif s == 402 or (s in (400, 429) and re.search(r"insufficient[_ ]quota|billing|credit balance|exceeded your current quota|out of credits", low)):
        kind = "quota"
    elif s == 429:
        kind = "rate"
    elif s == 404:
        kind = "not_found"
    elif s == 413:
        kind = "too_large"
    elif s == 408:
        kind = "timeout"
    elif s >= 500 or s == 529:
        kind = "server"
    else:
        kind = "bad_request"
    return ProviderError(kind, f"HTTP {s}: {text}", status=s, retry_after=retry)


def _send(http: httpx.Client, method: str, url: str, timeout: float, **kw) -> httpx.Response:
    try:
        r = http.request(method, url, timeout=httpx.Timeout(timeout, connect=10.0), **kw)
    except httpx.TimeoutException:
        raise ProviderError("timeout", f"no answer within {timeout:.0f}s") from None
    except httpx.TransportError as e:
        raise ProviderError("network", f"could not connect ({type(e).__name__})") from None
    except Exception as e:  # a bug in a transport must fail over, not kill the screening
        log.exception("unexpected error calling a provider")
        raise ProviderError("internal", f"{type(e).__name__}: {str(e)[:120]}") from None
    if r.status_code >= 400:
        raise error_from_response(r)
    return r


def _json(r: httpx.Response) -> dict:
    try:
        data = r.json()
    except ValueError:
        raise ProviderError("server", "the provider returned something that is not JSON") from None
    if not isinstance(data, dict):
        raise ProviderError("server", "unexpected response shape")
    return data


# ---------- the three wire formats ----------
_OPTIONAL_PARAMS = ("reasoning_effort", "response_format", "temperature", "max_completion_tokens", "max_tokens")


def _post_with_downgrade(http, url, headers, body, timeout) -> httpx.Response:
    """Some models reject optional parameters (temperature, reasoning_effort...). If the error names one, drop it and retry."""
    for _ in range(4):
        try:
            return _send(http, "POST", url, timeout, headers=headers, json=body)
        except ProviderError as e:
            if e.kind != "bad_request":
                raise
            low = str(e).lower()
            culprit = next((p for p in _OPTIONAL_PARAMS if p in low and p in body), None)
            if not culprit:
                raise
            if culprit == "max_tokens" and "max_completion_tokens" not in body:      # newer models want the other name
                body["max_completion_tokens"] = body.pop("max_tokens")
            elif culprit == "max_completion_tokens" and "max_tokens" not in body:
                body["max_tokens"] = body.pop("max_completion_tokens")
            else:
                body.pop(culprit)
    raise ProviderError("bad_request", "the provider rejected the request parameters")


def _openai(http, c: Candidate, system: str, messages: list, max_tokens: int, reasoning: str, timeout: float) -> tuple[str, int]:
    official = "api.openai.com" in c.base_url
    body = {"model": c.model, "messages": [{"role": "system", "content": system}, *messages], "response_format": {"type": "json_object"},
            "max_completion_tokens" if official else "max_tokens": max_tokens}
    if not (official and re.match(r"^(o\d|gpt-5)", c.model)):                          # reasoning models only accept the default temperature
        body["temperature"] = 0.1
    if reasoning and re.search(r"gpt-oss|^o\d|gpt-5", c.model):
        body["reasoning_effort"] = reasoning
    headers = {"Authorization": f"Bearer {c.api_key}"}
    if "openrouter.ai" in c.base_url:
        headers.update({"HTTP-Referer": "http://localhost", "X-Title": "Deal Screening"})
    data = _json(_post_with_downgrade(http, f"{c.base_url.rstrip('/')}/chat/completions", headers, body, timeout))
    try:
        text = data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ProviderError("server", "the provider's reply had no message") from None
    return text, int((data.get("usage") or {}).get("total_tokens") or 0)


def _anthropic(http, c: Candidate, system: str, messages: list, max_tokens: int, reasoning: str, timeout: float) -> tuple[str, int]:
    body = {"model": c.model, "max_tokens": max_tokens, "system": system, "messages": messages, "temperature": 0.1}
    headers = {"x-api-key": c.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    data = _json(_post_with_downgrade(http, f"{c.base_url.rstrip('/')}/v1/messages", headers, body, timeout))
    text = "".join(b.get("text", "") for b in data.get("content") or [] if isinstance(b, dict) and b.get("type") == "text")
    u = data.get("usage") or {}
    return text, int(u.get("input_tokens", 0)) + int(u.get("output_tokens", 0))


def _gemini(http, c: Candidate, system: str, messages: list, max_tokens: int, reasoning: str, timeout: float) -> tuple[str, int]:
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]} for m in messages],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": max_tokens + 1500, "responseMimeType": "application/json"}}  # headroom: thinking tokens count
    r = _send(http, "POST", f"{c.base_url.rstrip('/')}/models/{c.model}:generateContent", timeout,
              headers={"x-goog-api-key": c.api_key, "content-type": "application/json"}, json=body)
    data = _json(r)
    cands = data.get("candidates") or []
    if not cands:
        why = (data.get("promptFeedback") or {}).get("blockReason", "no answer")
        raise ProviderError("bad_request", f"Gemini returned no answer ({why})")
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
    return text, int((data.get("usageMetadata") or {}).get("totalTokenCount") or 0)


_TRANSPORTS = {"openai": _openai, "anthropic": _anthropic, "gemini": _gemini}


def complete(http: httpx.Client, c: Candidate, system: str, messages: list, max_tokens: int, reasoning: str, timeout: float) -> tuple[str, int]:
    return _TRANSPORTS[c.kind](http, c, system, messages, max_tokens, reasoning, timeout)


# ---------- discovery ----------
def list_models(http: httpx.Client, c: Candidate, timeout: float = 20.0) -> list[str]:
    """Models this key can use (also proves the key works without spending tokens)."""
    base = c.base_url.rstrip("/")
    if c.kind == "anthropic":
        r = _send(http, "GET", f"{base}/v1/models", timeout, params={"limit": 100}, headers={"x-api-key": c.api_key, "anthropic-version": "2023-06-01"})
        return [m["id"] for m in _json(r).get("data", []) if isinstance(m, dict) and "id" in m]
    if c.kind == "gemini":
        r = _send(http, "GET", f"{base}/models", timeout, params={"pageSize": 200}, headers={"x-goog-api-key": c.api_key})
        return [m["name"].removeprefix("models/") for m in _json(r).get("models", [])
                if isinstance(m, dict) and "generateContent" in (m.get("supportedGenerationMethods") or [])]
    r = _send(http, "GET", f"{base}/models", timeout, headers={"Authorization": f"Bearer {c.api_key}"})
    ids = [m["id"] for m in _json(r).get("data", []) if isinstance(m, dict) and "id" in m]
    if "api.openai.com" in base:  # the OpenAI list also holds embeddings, audio, images...
        ids = [i for i in ids if re.match(r"^(gpt|o\d)", i) and not re.search(r"audio|realtime|transcribe|tts|image|search|instruct|embedding|moderation|codex", i)]
    return ids


def pick_model(provider: str, available: list[str]) -> str:
    """Best available model by the catalog's preference order; newest name wins within a pattern."""
    for pat in CATALOG[provider]["prefer"]:
        hits = sorted((m for m in available if re.search(pat, m)), reverse=True)
        if hits:
            return hits[0]
    return CATALOG[provider]["default"]
