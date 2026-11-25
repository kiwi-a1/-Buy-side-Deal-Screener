"""External web search for cross-checking. Free by default (ddgs: no key); Serper is used if SERPER_API_KEY is set.

Each hit is classified by how independent it is from the company:
  company     - the company's own domain
  profile     - self-reported profiles (LinkedIn, Crunchbase, social media, directories) - not independent
  independent - everything else (news, registries, third-party sites)
Only "independent" hits can make a claim "verified".
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

PROFILE_DOMAINS = re.compile(
    r"(^|\.)(linkedin\.com|crunchbase\.com|twitter\.com|x\.com|facebook\.com|instagram\.com|youtube\.com|tiktok\.com|"
    r"github\.com|wellfound\.com|angel\.co|pitchbook\.com|dealroom\.co|f6s\.com|producthunt\.com|glassdoor\.[a-z.]+|"
    r"g2\.com|capterra\.com|trustpilot\.com|zoominfo\.com|apollo\.io|rocketreach\.co|cbinsights\.com|tracxn\.com|"
    r"medium\.com|substack\.com|apps\.apple\.com|play\.google\.com|grokipedia\.com)$")  # last one: AI-written pages


@dataclass
class Hit:
    id: str
    title: str
    url: str
    snippet: str
    query: str
    kind: str  # company | profile | independent


def classify(url: str, company_host: str) -> str:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    own = company_host.lower().removeprefix("www.")
    if own and (host == own or host.endswith("." + own)):
        return "company"
    return "profile" if PROFILE_DOMAINS.search(host) else "independent"


def build_queries(name: str, host: str, founders: list[str], has_traction: bool) -> list[str]:
    """Queries are written in code (no AI call): identity, funding, team, traction, and adverse news."""
    q = f'"{name}"'
    brand = host.removeprefix("www.").split(".")[0] if host else ""
    queries = [f"{q} {brand}".strip(), f"{q} funding raised investors"]
    if founders:
        queries.append(f'{q} "{founders[0]}"')
    if len(founders) > 1:
        queries.append(f'{q} "{founders[1]}"')
    if has_traction:
        queries.append(f"{q} revenue customers growth")
    queries.append(f"{q} lawsuit OR fraud OR layoffs OR controversy")
    return queries[:6]


def _serper(query: str, n: int) -> list[dict]:
    import httpx
    r = httpx.post("https://google.serper.dev/search", json={"q": query, "num": n}, timeout=15,
                   headers={"X-API-KEY": os.environ["SERPER_API_KEY"], "Content-Type": "application/json"})
    r.raise_for_status()
    return [{"title": x.get("title", ""), "href": x.get("link", ""), "body": x.get("snippet", "")}
            for x in r.json().get("organic", [])]


def _ddgs(query: str, n: int) -> list[dict]:
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException
    last: Exception | None = None
    for backend in ("auto", "bing", "yahoo"):  # the free backends fail intermittently; try the next one
        try:
            return DDGS().text(query, max_results=n, backend=backend) or []
        except DDGSException as e:
            last = e
            time.sleep(0.5)
    raise last or RuntimeError("search failed")


def search(name: str, host: str, founders: list[str], has_traction: bool, per_query: int = 5,
           backend: Optional[Callable[[str, int], list[dict]]] = None, retry_delay: float = 2.0,
           on_progress: Callable[[str], None] = lambda m: None) -> dict:
    """Run the queries, keep only results that mention the company, classify them. Never raises."""
    provider = "serper" if os.getenv("SERPER_API_KEY") else "ddgs (free)"
    backend = backend or (_serper if provider == "serper" else _ddgs)
    queries = build_queries(name, host, founders, has_traction) if name and name.lower() != "unknown" else []
    brand = host.removeprefix("www.").split(".")[0].lower() if host else ""
    hits: list[Hit] = []
    seen: set[str] = set()
    irrelevant = 0

    def run(batch: list[str]) -> list[dict]:
        nonlocal irrelevant
        failed = []
        for i, q in enumerate(batch, 1):
            on_progress(f"Searching the web ({i}/{len(batch)})...")
            try:
                results = backend(q, per_query)
            except Exception as e:  # network/provider failure must not stop the screening
                failed.append({"query": q, "error": f"{type(e).__name__}: {str(e)[:120]}"})
                continue
            for r in results:
                url, title, body = r.get("href") or "", (r.get("title") or "").strip(), (r.get("body") or "").strip()
                if not url.startswith("http") or url in seen:
                    continue
                seen.add(url)
                hay = f"{title} {body} {url}".lower()
                if name.lower() not in hay and not (brand and len(brand) > 3 and brand in hay):
                    irrelevant += 1  # a different company with a similar name, or a generic page
                    continue
                hits.append(Hit(id=f"S{len(hits) + 1}", title=title[:200], url=url, snippet=body[:320], query=q,
                                kind=classify(url, host)))
        return failed

    failed = run(queries)
    if failed:  # the free backends fail intermittently: one more try for the failed queries
        time.sleep(retry_delay)
        failed = run([f["query"] for f in failed])
    return {"provider": provider, "queries": queries, "failed": failed, "irrelevant": irrelevant,
            "available": bool(queries) and len(failed) < len(queries), "hits": [h.__dict__ for h in hits]}
