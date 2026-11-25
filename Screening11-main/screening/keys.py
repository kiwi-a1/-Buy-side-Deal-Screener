"""AI keys: where they come from (environment or the app), turning them into candidates for the engine, and testing them.

Keys typed into the app are stored in the app's database on this computer/server and are never sent back to the browser
(only the last 4 characters). Keys set as environment variables stay in the environment; the app only remembers that they exist."""
from __future__ import annotations

import os
import re
import time
from typing import Optional
from urllib.parse import urlparse

import httpx

from .providers import CATALOG, Candidate, ProviderError, complete, list_models, pick_model

FRIENDLY = {
    "auth": "The key was rejected. Check that it was copied completely and belongs to this provider.",
    "quota": "The key works but has no credit or quota left. Add credit with the provider, or use another key.",
    "rate": "The provider is rate limiting this key right now. Try again in a minute.",
    "timeout": "The provider did not answer in time.",
    "network": "Could not reach the provider. Check the internet connection and the base URL.",
    "server": "The provider reported a server error. Try again shortly.",
    "not_found": "The model was not found for this key. Pick another model.",
    "bad_request": "The provider rejected the test request.",
    "internal": "Something unexpected went wrong while testing.",
    "too_large": "The provider rejected the request as too large.",
}


def env_key(provider: str, env=os.environ) -> str:
    return next((env[n].strip() for n in CATALOG[provider]["env"] if env.get(n, "").strip()), "")


def sync_env(store, env=os.environ) -> None:
    """Make every provider that has an environment variable show up in the list (once). The key itself is never copied."""
    have = {(r["provider"], r["source"]) for r in store.list_keys()}
    for pid, cfg in CATALOG.items():
        if env_key(pid, env) and (pid, "env") not in have:
            store.add_key(pid, cfg["label"], None, model=env.get("LLM_CUSTOM_MODEL", "") if pid == "custom" else "",
                          base_url=env.get("LLM_CUSTOM_BASE_URL", "") if pid == "custom" else "", source="env", priority=cfg["rank"] * 10)


def resolve_key(row: dict, env=os.environ) -> str:
    return env_key(row["provider"], env) if row["source"] == "env" else (row.get("api_key") or "")


def candidates_for(row: dict, env=os.environ) -> list[Candidate]:
    """The (key, model) pairs one saved key contributes. Empty if it is disabled or incomplete."""
    cfg, pid = CATALOG.get(row["provider"]), row["provider"]
    key = resolve_key(row, env)
    if not cfg or not row["enabled"] or not key:
        return []
    base = row["base_url"] or cfg["base_url"] or env.get("LLM_CUSTOM_BASE_URL", "")
    model = row["model"] or (env.get("LLM_CUSTOM_MODEL", "") if pid == "custom" else "")
    if cfg["needs_base_url"] and not base:
        return []
    if pid == "groq" and not row["model"]:                       # Groq: three free models share one key; fall through them in order
        models = [m.strip() for m in (env.get("GROQ_MODELS") or "").split(",") if m.strip()] or cfg["fallback_models"]
    else:
        models = [model or cfg["default"]]
    if not models[0]:
        return []
    tpm = None
    if pid == "groq":
        tpm = int(env.get("GROQ_TPM_LIMIT") or cfg["tpm_default"])
    elif env.get(f"LLM_TPM_{pid.upper()}"):
        tpm = int(env[f"LLM_TPM_{pid.upper()}"])
    return [Candidate(provider=pid, label=row["label"] or cfg["label"], kind=cfg["kind"], base_url=base, api_key=key, model=m, key_id=row["id"], tpm=tpm)
            for m in models]


def all_candidates(store, env=os.environ) -> list[Candidate]:
    return [c for row in store.list_keys() for c in candidates_for(row, env)]


def public_row(row: dict, health, env=os.environ) -> dict:
    cands = candidates_for(row, env)
    key = resolve_key(row, env)
    st = [health.status(c) for c in cands]
    cooling = [s for s in st if s["cooling"]]
    status = ("disabled" if not row["enabled"] else "missing" if not key else "incomplete" if not cands
              else "cooling" if cands and len(cooling) == len(cands) else "ready")
    cfg = CATALOG.get(row["provider"], {})
    return {"id": row["id"], "provider": row["provider"], "label": row["label"], "model": row["model"] or (cands[0].model if cands else ""),
            "base_url": row["base_url"], "source": row["source"], "enabled": bool(row["enabled"]), "priority": row["priority"],
            "key_hint": f"…{key[-4:]}" if len(key) >= 8 else ("set" if key else ""), "env_names": cfg.get("env", []) if row["source"] == "env" else [],
            "status": status, "reason": cooling[0]["reason"] if cooling else None, "seconds": max((s["seconds"] for s in cooling), default=0),
            "models_tried": [c.model for c in cands], "tpm": cands[0].tpm if cands else None}


def validate_new(provider: str, api_key: str, model: str, base_url: str) -> Optional[str]:
    """Return a message if the form is not usable, else None."""
    cfg = CATALOG.get(provider)
    if not cfg:
        return "Choose a provider."
    if len(api_key.strip()) < 8 or re.search(r"\s", api_key.strip()):
        return "That does not look like an API key (it is too short or contains spaces)."
    if cfg["needs_model"] and not model.strip():
        return f"{cfg['label']} needs a model name."
    if cfg["needs_base_url"]:
        u = urlparse(base_url.strip())
        local = (u.hostname or "") in ("localhost", "127.0.0.1", "::1")
        if u.scheme not in ("https", "http") or not u.netloc or (u.scheme == "http" and not local):
            return "Enter the service's base URL, e.g. https://api.mistral.ai/v1 (plain http is only allowed for localhost)."
    return None


def test_candidate(http: httpx.Client, cand: Candidate) -> dict:
    """Prove a key works: list its models if possible (picking one if none was chosen), then ask for a tiny JSON reply."""
    t0, model, note = time.monotonic(), cand.model, ""
    try:
        if cand.provider not in ("custom", "openrouter") and (not cand.model or cand.model == CATALOG[cand.provider]["default"]):
            try:
                ids = list_models(http, cand)
                if ids:
                    model = pick_model(cand.provider, ids)
                    cand = Candidate(**{**cand.__dict__, "model": model})
            except ProviderError as e:
                if e.kind == "auth":
                    raise
                note = "Could not list models; used the default."
        text, _ = complete(http, cand, "You reply with JSON only.", [{"role": "user", "content": 'Reply with exactly {"ok": true}'}], 300, "low", 45.0)
        return {"ok": True, "model": model, "ms": round((time.monotonic() - t0) * 1000), "note": note, "reply": text[:60]}
    except ProviderError as e:
        return {"ok": False, "kind": e.kind, "error": FRIENDLY.get(e.kind, str(e)), "detail": str(e)[:200], "model": model}
