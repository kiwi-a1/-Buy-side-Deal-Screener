"""The analysis pipeline: documents + website + web -> claims -> verification -> scoring -> result.

Order (each stage uses validated output from the previous one; network work runs in parallel with AI calls):
  1. read deck + extra files                      (code; an unreadable deck stops the run)
  2. crawl the website                            (starts immediately, in the background)
  3. AI: extract profile + claims from documents  (quotes are checked against the source text)
  4. web search                                   (background, needs the company name from step 3)
     AI: extract profile + claims from the website pages (if the crawl found any)
  5. AI: verify claims against cross-source claims and search results; code enforces the evidence rules
  6. AI: stage- and model-aware scorecard + brief; code computes weights, penalties, confidence, mandate fit, decision
A failure in steps 2, 4 or 5 is recorded as an issue and the run continues with less evidence."""
from __future__ import annotations

import json
import time
from datetime import date
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

from . import prompts
from .crawler import CrawlResult, crawl
from .intake import IntakeError, pdf_text, resource_text, truncate
from .llm import LLM, LLMError
from .models import Assessment, Extraction, Profile, Verification
from .scoring import DIMENSIONS, evaluate, fit_check, stage_group
from .suggestions import normalize_stage
from .verify import candidates, consistency_checks, finalize_claims, number_mismatches, quote_found
from .websearch import search

MAX_DECK_CLAIMS, MAX_SITE_CLAIMS, MAX_VERIFY = 15, 10, 16
STEPS = [("documents", "Read documents"), ("website", "Crawl website"), ("extract", "Extract claims"),
         ("search", "Search the web"), ("verify", "Verify claims"), ("score", "Score & report")]


@dataclass
class Inputs:
    deck_name: str
    deck: bytes
    website: str
    resources: list[tuple[str, bytes]] = field(default_factory=list)
    hint: str = ""


def _fit_docs(docs: list[dict], budget: int) -> tuple[str, list[str]]:
    """Give the deck most of the budget; extra files share the rest. Returns the text block and what was cut."""
    shares = [1.0] if len(docs) == 1 else [0.65] + [0.35 / (len(docs) - 1)] * (len(docs) - 1)
    parts, cut = [], []
    for d, share in zip(docs, shares):
        body, was_cut = truncate(d["text"], max(800, int(budget * share)))
        if was_cut:
            cut.append(d["name"])
        parts.append(f"=== {d['id']}: {d['kind']} '{d['name']}' ===\n{body}")
    return "\n\n".join(parts), cut


def _pick_pages(pages: list, budget: int) -> list[dict]:
    """Homepage first, then the most relevant pages, each trimmed so several pages fit in one call."""
    ordered = sorted(pages, key=lambda p: (p.depth != 0, -p.priority, p.depth))
    picked, used = [], 0
    per_page = max(2800, budget // 10)       # a provider with room to spare reads each page more fully
    for p in ordered:
        room = budget - used
        if room < 600:
            break
        body = p.text[:min(per_page, room)]
        picked.append({"id": f"P{len(picked) + 1}", "url": p.url, "title": p.title, "text": body})
        used += len(body) + 120
    return picked


def analyse(inp: Inputs, llm: LLM, thesis, prof: dict, step: Callable[[str, str, str], None] = lambda k, s, d: None,
            crawl_fn: Callable = crawl, search_fn: Callable = search) -> dict:
    t0 = time.time()
    issues: list[dict] = []

    def issue(stage: str, severity: str, message: str) -> None:
        issues.append({"stage": stage, "severity": severity, "message": message})

    # 1. documents --------------------------------------------------------------------------------------------
    step("documents", "running", "Reading the pitch deck...")
    docs = [{"id": "D0", "name": inp.deck_name, "kind": "pitch deck", "source": "deck", "text": pdf_text(inp.deck)}]
    for name, data in inp.resources:
        try:
            docs.append({"id": f"D{len(docs)}", "name": name, "kind": "extra file", "source": "resource",
                         "text": resource_text(name, data)})
        except IntakeError as e:
            issue("documents", "warning", f"{e} The file was skipped.")
    for d in docs[1:]:
        if len(d["text"]) < 50:
            issue("documents", "info", f"{d['name']}: almost no text; it adds little.")
    step("documents", "done", f"{len(docs)} document(s), {sum(len(d['text']) for d in docs):,} characters")

    host = urlparse(inp.website).hostname or ""
    model_types = prof["model_types"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        step("website", "running", f"Crawling {host}...")
        crawl_job = pool.submit(crawl_fn, inp.website, on_progress=lambda m: step("website", "running", m))

        # 2. extract from documents (AI 1) ------------------------------------------------------------------
        step("extract", "running", "Extracting facts and claims from your documents...")
        max_doc_claims = min(MAX_DECK_CLAIMS + 4 * (len(docs) - 1), 24)  # extra files get their own share of the limit
        system = prompts.extract_system(model_types, max_doc_claims)
        block, cut = _fit_docs(docs, llm.budget_chars(max_tokens=2600, overhead_chars=len(system) + 200))
        if cut:
            issue("documents", "warning", f"Too long for one free-tier request, so the middle was skipped in: {', '.join(cut)}. "
                  "The start and end were read.")
        deck_ex = llm.json(system, f"Company name hint: {inp.hint or 'none'}\n<UNTRUSTED>\n{block}\n</UNTRUSTED>",
                           Extraction, max_tokens=2600)
        deck_p = deck_ex.profile
        name = deck_p.company_name or inp.hint or host.removeprefix("www.").split(".")[0].title()
        if not deck_p.company_name:
            deck_p.company_name = name
            issue("extract", "info", f"The deck does not state a company name; using '{name}'.")

        # 3. web search in the background (AI 2 runs meanwhile) ---------------------------------------------
        step("search", "running", "Searching the web...")
        has_traction = bool(deck_p.traction) or any(c.category in ("traction", "revenue", "customers") for c in deck_ex.claims)
        search_job = pool.submit(search_fn, name, host, [f.name for f in deck_p.founders], has_traction,
                                 on_progress=lambda m: step("search", "running", m))

        try:
            cr = crawl_job.result()
        except Exception as e:  # a bug or odd page in the crawler must cost evidence, not the whole screening
            cr = CrawlResult(start_url=inp.website, stopped_reason="crawler error",
                             errors=[{"url": inp.website, "error": f"internal error: {type(e).__name__}"}])
        site_ok = cr.reachable and bool(cr.pages)
        crawl_info = {"start_url": inp.website, "final_url": cr.final_url, "pages_crawled": len(cr.pages),
                      "errors": cr.errors[:25], "skipped": cr.skipped, "sitemap_urls": cr.sitemap_urls,
                      "stopped_reason": cr.stopped_reason, "not_visited": cr.not_visited, "pages_analysed": []}
        if not cr.reachable:
            issue("website", "error", f"The website {inp.website} could not be reached ({cr.errors[0]['error'] if cr.errors else 'no response'}). "
                  "Website analysis was skipped; the result relies on your documents and web search only.")
            step("website", "error", "Website unreachable")
        else:
            if cr.errors:
                issue("website", "info", f"{len(cr.errors)} page(s) failed to load (broken links, timeouts or refusals).")
            if cr.not_visited:
                issue("website", "info", f"Stopped at the {cr.stopped_reason}; {cr.not_visited} lower-priority links were not visited.")
            step("website", "done" if site_ok else "warning", f"{len(cr.pages)} pages read, {len(cr.errors)} failed")

        site_p, pages = Profile(), []
        site_claims_raw = []
        if site_ok:
            step("extract", "running", "Extracting claims from the website...")
            sys2 = prompts.site_system(model_types, MAX_SITE_CLAIMS)
            pages = _pick_pages(cr.pages, llm.budget_chars(max_tokens=2200, overhead_chars=len(sys2) + 200))
            crawl_info["pages_analysed"] = [{"id": p["id"], "url": p["url"], "title": p["title"]} for p in pages]
            if len(pages) < len(cr.pages):
                issue("website", "info", f"{len(pages)} of {len(cr.pages)} crawled pages were analysed in depth (free-tier limit); "
                      "the most relevant pages were chosen.")
            pblock = "\n\n".join(f"=== {p['id']}: {p['url']} ({p['title']}) ===\n{p['text']}" for p in pages)
            try:
                site_ex = llm.json(sys2, f"<UNTRUSTED>\n{pblock}\n</UNTRUSTED>", Extraction, max_tokens=2200, max_providers=2)
                site_p, site_claims_raw = site_ex.profile, site_ex.claims
            except LLMError as e:
                issue("extract", "error", f"Website extraction failed ({e}); continuing with the documents only.")
        try:
            sr = search_job.result()
        except Exception as e:
            sr = {"provider": "unknown", "queries": [], "failed": [{"query": "-", "error": f"internal error: {type(e).__name__}"}],
                  "irrelevant": 0, "available": False, "hits": []}

    # 4. assemble claims (quotes checked against their source) --------------------------------------------------
    claims: list[dict] = []
    doc_by_id = {d["id"]: d for d in docs}
    page_by_id = {p["id"]: p for p in pages}

    def add(raw, origin: dict, source: str, label: str) -> None:
        claims.append({"id": f"C{len(claims) + 1}", "text": raw.text, "category": raw.category, "importance": raw.importance,
                       "source": source, "source_label": label + (f", {raw.ref}" if raw.ref and raw.ref not in label else ""),
                       "url": origin.get("url", ""), "quote": raw.quote, "quote_found": quote_found(raw.quote, origin["text"]),
                       "status": "pending", "evidence": [], "note": ""})

    for raw in deck_ex.claims[:max_doc_claims]:
        d = doc_by_id.get(raw.doc.upper(), docs[0])
        add(raw, d, d["source"], "Pitch deck" if d["source"] == "deck" else d["name"])
    for raw in site_claims_raw[:MAX_SITE_CLAIMS]:
        p = page_by_id.get(raw.doc.upper()) or (pages[0] if pages else {"text": "", "url": ""})
        add(raw, p, "website", "Website " + (urlparse(p.get("url", "")).path or "/"))
    unquoted = [c["id"] for c in claims if not c["quote_found"]]
    if unquoted:
        issue("extract", "warning", f"The AI's supporting quote could not be found in the source for {', '.join(unquoted)}; "
              "these claims may be paraphrased or inaccurate - they are marked in the claim table.")
    step("extract", "done", f"{len(claims)} claims ({sum(c['source'] != 'website' for c in claims)} from documents, "
         f"{sum(c['source'] == 'website' for c in claims)} from the website)")

    # profile: documents first, website fills gaps
    deck_d, site_d = deck_p.model_dump(), site_p.model_dump()
    profile, profile_sources = {}, {}
    for k, v in deck_d.items():
        use_site = v in ("", [], None, "other") and site_d.get(k) not in ("", [], None, "other")
        profile[k] = site_d[k] if use_site else v
        if profile[k] not in ("", [], None):
            profile_sources[k] = "website" if use_site else "documents"
    profile["website"] = cr.final_url or inp.website

    hits = sr.get("hits", [])
    if not sr.get("available"):
        issue("search", "warning", "Web search was unavailable" + (f" ({sr['failed'][0]['error']})" if sr.get("failed") else "") +
              ". Claims could only be compared with the company's own materials.")
        step("search", "warning", "Search unavailable")
    else:
        if sr.get("failed"):
            issue("search", "info", f"{len(sr['failed'])} of {len(sr['queries'])} searches failed; results are from the rest.")
        indep = sum(h["kind"] == "independent" for h in hits)
        step("search", "done", f"{len(hits)} relevant results ({indep} independent), {sr.get('irrelevant', 0)} off-topic dropped")

    # 5. verify (AI 3) - evidence rules enforced in code -------------------------------------------------------
    step("verify", "running", "Checking claims against evidence...")
    order = {"high": 0, "medium": 1, "low": 2}
    to_check = [c for c in sorted(claims, key=lambda c: order[c["importance"]]) if c["importance"] != "low"][:MAX_VERIFY]
    check_ids = {c["id"] for c in to_check}
    for c in claims:
        if c["id"] not in check_ids:
            c.update(status="not_checked", note="Low importance or over the per-run checking limit.")
    sources = {c["source"] for c in claims}
    if to_check and (hits or len(sources) > 1):
        claim_lines = "\n".join(f"{c['id']} [{c['source_label']}] {c['text']}" for c in claims)
        cand_lines = "\n".join(f"{c['id']}: candidates {', '.join(candidates(c, claims, hits, profile['company_name'])) or 'none found'}"
                               for c in to_check)
        budget = llm.budget_chars(max_tokens=3000, overhead_chars=len(prompts.VERIFY_SYSTEM) + len(claim_lines) + len(cand_lines) + 300)
        ranked = sorted(hits, key=lambda h: {"independent": 0, "profile": 1, "company": 2}[h["kind"]])
        hit_lines, used = [], 0
        for h in ranked:
            line = f"{h['id']} [{h['kind']}] {h['title']} - {h['snippet']} ({urlparse(h['url']).hostname})"
            if used + len(line) > budget:
                break
            hit_lines.append(line)
            used += len(line)
        if len(hit_lines) < len(hits):
            issue("verify", "info", f"{len(hit_lines)} of {len(hits)} search results fit in the verification request (independent ones first).")
        user = (f"Today's date: {date.today().isoformat()}\nCLAIMS TO CHECK, with likely evidence:\n{cand_lines}\n\n<UNTRUSTED>\n"
                f"COMPANY CLAIMS (C ids):\n{claim_lines}\n\nWEB SEARCH RESULTS (S ids):\n{chr(10).join(hit_lines) or '(none)'}\n</UNTRUSTED>")
        try:  # medium reasoning: comparing numbers and dates across sources is where accuracy matters most
            checks = llm.json(prompts.VERIFY_SYSTEM, user, Verification, max_tokens=3000, max_providers=2, reasoning="medium").checks
        except LLMError as e:
            checks = []
            issue("verify", "error", f"The verification step failed ({e}); claims are shown as unverified.")
        finalize_claims(claims, checks, hits, issues)
    else:
        for c in to_check:
            c.update(status="unverified", note="No independent or cross-source evidence was available.")
        if to_check:
            issue("verify", "warning", "No evidence was available to check claims against (no search results, no website claims).")
    counts = {s: sum(c["status"] == s for c in claims) for s in ("verified", "consistent", "contradicted", "outdated", "unverified", "not_checked")}
    step("verify", "warning" if counts["contradicted"] + counts["outdated"] else "done",
         f"{counts['verified']} verified, {counts['consistent']} company-only, {counts['contradicted'] + counts['outdated']} contradicted/outdated, "
         f"{counts['unverified']} unverified")

    # 6. score (AI 4) + code-based decision --------------------------------------------------------------------
    step("score", "running", "Scoring and writing the report...")
    stage = normalize_stage(profile.get("stage", ""))
    group = stage_group(stage, prof)
    mtype = profile.get("business_model_type") or "other"
    if mtype not in model_types:
        issue("score", "info", f"Unknown business model type '{mtype}' from the AI; no model-specific weighting applied.")
        mtype = "other"
    context = {"stage": stage, "stage_group": group, "model_type": mtype, "model_label": model_types[mtype]["label"]}
    if not group:
        issue("score", "warning", "The funding stage is not stated, so no stage-specific weighting was applied.")
    expectation = prof["stage_expectations"].get(group) if group else "Stage unknown: judge conservatively and list the stage as missing."
    sys4 = prompts.ASSESS_SYSTEM.format(stage_expectations=expectation, model_label=context["model_label"])
    claim_table = "\n".join(f"{c['id']} [{c['status']}; {c['source']}] {c['text']}" for c in claims)
    pjson = json.dumps({k: v for k, v in profile.items() if v not in ("", [], None)}, ensure_ascii=False, separators=(",", ":"))
    room = llm.budget_chars(max_tokens=3200, overhead_chars=len(sys4) + len(pjson) + 200)
    if len(claim_table) > room:
        claim_table = claim_table[:room].rsplit("\n", 1)[0]
        issue("score", "info", "Some low-priority claims were left out of the scoring request (free-tier limit).")
    assess = llm.json(sys4, f"<UNTRUSTED>\nPROFILE:\n{pjson}\n\nCLAIMS:\n{claim_table or '(none)'}\n</UNTRUSTED>",
                      Assessment, max_tokens=3200)

    known = {c["id"] for c in claims}
    dims, bad_refs = {}, 0
    for k, d in assess.dimensions.items():
        ids = [i for i in d.claim_ids if i in known]
        bad_refs += len(d.claim_ids) - len(ids)
        dims[k] = {"reasoning": d.reasoning, "claim_ids": ids, "evidence": d.evidence, "missing": d.missing}
    if bad_refs:
        issue("score", "info", f"{bad_refs} claim reference(s) from the scoring AI did not exist and were removed.")
    coverage = sum(1 for d in dims.values() if d["claim_ids"] or d["evidence"])
    raw_scores = {k: assess.dimensions[k].score for k in DIMENSIONS}
    fit = fit_check(profile, thesis, prof, profile_sources)
    ev = evaluate(raw_scores, coverage, fit, thesis, context, claims, prof, search_ok=bool(sr.get("available")), website_ok=site_ok)
    for k in DIMENSIONS:
        dims[k].update(ev["scores"][k])
    checks = consistency_checks(deck_d, site_d, " ".join(p.text for p in cr.pages), site_ok, sr, group, prof["round_size_eur"])
    checks += number_mismatches(claims)
    seen = set()
    for e in llm.events:                                       # provider switches are part of the story: show each distinct one once
        key = (e["from"], e["to"], e["reason"])
        if key not in seen:
            seen.add(key)
            issue("ai", "info", f"{e['from']} {e['reason']}, so {e['to']} was used instead.")
    step("score", "done", f"{ev['decision']} - {ev['overall']}/10 ({ev['confidence']['level']} confidence)")

    return {
        "version": 2, "company": profile["company_name"], "website": profile["website"], "fund": thesis.fund_name,
        "inputs": {"deck": inp.deck_name, "resources": [d["name"] for d in docs[1:]], "website": inp.website},
        "thesis": {"name": thesis.fund_name, "data": thesis.model_dump()},
        "thresholds": {"advance_at": thesis.advance_at, "maybe_at": thesis.maybe_at},
        "decision": ev["decision"], "decision_reason": ev["decision_reason"], "overall": ev["overall"],
        "overall_raw": ev["overall_raw"], "adjustment": ev["adjustment"], "confidence": ev["confidence"],
        "weights": ev["weights"], "context": context, "fit": fit, "evidence_coverage": coverage,
        "dimensions": dims, "raw_scores": raw_scores, "claims": claims, "claim_counts": counts, "consistency": checks,
        "red_flag_count": counts["contradicted"] + counts["outdated"] + sum(c["state"] in ("warn", "fail") for c in checks),
        "profile": profile, "profile_sources": profile_sources,
        "brief": {"summary": assess.summary, "strengths": assess.strengths, "concerns": assess.concerns, "next_steps": assess.next_steps},
        "questions_for_founder": assess.questions_for_founder, "missing_critical_data": assess.missing_critical_data,
        "crawl": crawl_info,
        "search": {"provider": sr.get("provider"), "queries": sr.get("queries", []), "failed": sr.get("failed", []),
                   "available": bool(sr.get("available")), "irrelevant": sr.get("irrelevant", 0), "hits": hits},
        "issues": issues, "model": llm.last_model, "llm_calls": llm.calls, "seconds": round(time.time() - t0, 1),
        "llm": {"used": llm.used, "switches": llm.events},
    }
