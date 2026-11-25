"""Rules that decide what a claim's verification status may be, and code-only consistency checks.

The AI proposes a status; code decides what can be shown:
  verified     - supported by at least one INDEPENDENT web source
  consistent   - supported only by the company's own materials or self-reported profiles
  contradicted - evidence states something different (needs cited evidence)
  outdated     - newer evidence replaces it (needs cited evidence)
  unverified   - no evidence either way
  not_checked  - low importance, or over the per-run checking limit
An AI status that cites no valid evidence is downgraded to "unverified", and every downgrade is reported."""
from __future__ import annotations

import re
import unicodedata

from .suggestions import normalize_country

ADVERSE = re.compile(r"\b(lawsuit|sued|suing|fraud|scam|layoffs?|laid off|bankrupt\w*|insolven\w*|investigation|"
                     r"indicted|scandal|data breach|class action|fined|penalty|shut(s|ting)? down)\b", re.I)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def quote_found(quote: str, source: str) -> bool:
    """Is the AI's quote really in the source? Exact after normalising; else 85% of its words in one window."""
    q, s = _norm(quote), _norm(source)
    if len(q) < 8:
        return False
    if q in s:
        return True
    words = q.split()
    swords = s.split()
    if len(words) < 4:
        return False
    need, n = int(len(words) * 0.85 + 0.5), len(words)
    qset = set(words)
    return any(sum(w in qset for w in swords[i:i + n + 3]) >= need for i in range(0, max(1, len(swords) - n + 1)))


STOP = set("""the and for with that this from have has had are was were will been into their they them than then over more
most such also its it's our your about which while where when what who whom whose these those each other some only very
company companies startup startups including include includes per year years month months new using used use based""".split())
COUNT_NOUNS = ("developers", "users", "customers", "clients", "employees", "applications", "apps", "data centers", "datacenters",
               "countries", "partners", "merchants", "downloads", "teams", "companies", "stores", "cities", "members",
               "subscribers", "installations", "hospitals", "schools", "patients", "drivers", "sellers", "buyers", "brands")
_COUNT = re.compile(r"(\d[\d,.]*)\s*(k|m|million|thousand|bn|billion)?\+?\s+(?:new |active |paying |monthly |daily )?(" +
                    "|".join(COUNT_NOUNS) + r")\b", re.I)
_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "bn": 1e9, "billion": 1e9}


def _words(text: str) -> set[str]:
    return {w.rstrip("s") for w in _norm(text).split() if (len(w) >= 4 or w.isdigit()) and w not in STOP}


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\d[\d,.]*\d|\d", text) if len(n.replace(",", "")) >= 2}


def candidates(claim: dict, claims: list[dict], hits: list[dict], name: str, k: int = 4) -> list[str]:
    """Evidence most likely to speak to this claim: other-source claims and search results sharing key terms or
    numbers. Gives the verifier a short, concrete comparison instead of a haystack."""
    skip = _words(name)
    cw, cn = _words(claim["text"]) - skip, _numbers(claim["text"])
    scored = []
    for o in claims:
        if o["source"] != claim["source"]:
            scored.append((len(cw & (_words(o["text"]) - skip)) + 2 * len(cn & _numbers(o["text"])), o["id"]))
    for h in hits:
        t = f"{h['title']} {h['snippet']}"
        scored.append((len(cw & (_words(t) - skip)) + 2 * len(cn & _numbers(t)), h["id"]))
    return [i for sc, i in sorted(scored, key=lambda x: -x[0]) if sc >= 2][:k]


def counts(text: str) -> dict[str, float]:
    """'over 170,000 developers' -> {'developers': 170000}."""
    out = {}
    for num, mult, noun in _COUNT.findall(text):
        try:
            value = float(num.replace(",", "")) * _MULT.get(mult.lower(), 1)
        except ValueError:
            continue
        out[noun.lower().replace("datacenters", "data centers")] = value
    return out


def number_mismatches(claims: list[dict]) -> list[dict]:
    """Same metric, different number, different source (e.g. deck vs website). Deterministic; for human review."""
    out, seen = [], set()
    for a in claims:
        for b in claims:
            if a["source"] == b["source"] or (b["id"], a["id"]) in seen:
                continue
            seen.add((a["id"], b["id"]))
            ca, cb = counts(a["text"]), counts(b["text"])
            for noun in ca.keys() & cb.keys():
                x, y = ca[noun], cb[noun]
                if max(x, y) > 0 and abs(x - y) / max(x, y) > 0.1:
                    out.append({"check": f"Numbers differ: {noun}", "state": "warn", "links": [],
                                "detail": f"{a['source_label']} ({a['id']}) says {x:,.0f} {noun}; {b['source_label']} ({b['id']}) says "
                                          f"{y:,.0f}. They may be from different dates - confirm which is current."})
    return out


def finalize_claims(claims: list[dict], checks: list, hits: list[dict], issues: list[dict]) -> None:
    """Apply the evidence rules to the AI's verification in place. `checks` are models.Check objects."""
    by_id = {c["id"]: c for c in claims}
    hit_by_id = {h["id"]: h for h in hits}
    invalid: list[str] = []
    for chk in checks:
        c = by_id.get(chk.claim_id.upper())
        if not c or c["status"] != "pending":
            continue
        evidence, dropped = [], []
        for eid in dict.fromkeys(chk.evidence_ids):  # keep order, drop duplicates
            if eid in hit_by_id:
                h = hit_by_id[eid]
                evidence.append({"id": eid, "kind": h["kind"], "title": h["title"], "url": h["url"], "snippet": h["snippet"]})
            elif eid in by_id and eid != c["id"] and by_id[eid]["source"] != c["source"]:
                o = by_id[eid]
                evidence.append({"id": eid, "kind": "company", "title": f"{o['source_label']}", "url": o.get("url", ""),
                                 "snippet": o["text"]})
            else:
                dropped.append(eid)
        if dropped:
            invalid.append(f"{c['id']} ({', '.join(dropped)})")
        independent = any(e["kind"] == "independent" for e in evidence)
        status = chk.status
        note = chk.note
        if status == "supported":
            status = "verified" if independent else ("consistent" if evidence else "unverified")
            if not evidence and dropped:  # don't let the AI's note read as support when its evidence was rejected
                note = f"{note} - but the evidence it cited was invalid (e.g. the claim itself), so this is unverified."
        elif status in ("contradicted", "outdated") and not evidence:
            issues.append({"stage": "verification", "severity": "info",
                           "message": f"{c['id']}: the AI said '{status}' without citing evidence - shown as unverified."})
            status = "unverified"
        elif status == "unverified":
            evidence = []
        c.update(status=status, evidence=evidence, note=note)
    if invalid:
        issues.append({"stage": "verification", "severity": "info", "message": "Ignored invalid evidence references from the AI "
                       f"(a claim citing itself, its own source, or a non-existent id): {'; '.join(invalid)}."})
    for c in claims:  # claims the AI skipped
        if c["status"] == "pending":
            c.update(status="unverified", evidence=[], note="No verification result returned for this claim.")


def consistency_checks(deck: dict, site: dict, site_text: str, crawl_ok: bool, search: dict, group: str | None,
                       round_ranges: dict) -> list[dict]:
    """Plain-code cross-checks between the deck, the website and the web. state: ok | info | warn | fail."""
    out = []

    def add(check, state, detail, links=None):
        out.append({"check": check, "state": state, "detail": detail, "links": links or []})

    hits = search.get("hits", [])
    web_text = " " + _norm(" ".join(h["title"] + " " + h["snippet"] for h in hits)) + " "
    site_n = f" {_norm(site_text)} "
    name = deck.get("company_name", "")

    if not crawl_ok:
        add("Website", "fail", "The website could not be reached or had no readable pages, so nothing on it could be checked.")
    elif name:
        found = f" {_norm(name)} " in site_n
        add("Name on website", "ok" if found else "warn", f"'{name}' {'appears' if found else 'does not appear'} on the crawled website.")

    for f in deck.get("founders", [])[:4]:
        n = _norm(f["name"])
        if not n:
            continue
        where = [w for w, t in (("website", site_n), ("web search", web_text)) if f" {n} " in t]  # whole words only
        add(f"Founder: {f['name']}", "ok" if where else "warn",
            f"Found on {' and '.join(where)}." if where else "Not found on the website or in web search results.")

    d_hq, s_hq = normalize_country(deck.get("hq_country", "")), normalize_country(site.get("hq_country", ""))
    if d_hq and s_hq:
        add("HQ country", "ok" if d_hq.lower() == s_hq.lower() else "warn",
            f"Deck: {d_hq}; website: {s_hq}." if d_hq.lower() != s_hq.lower() else f"Deck and website agree: {d_hq}.")
    d_y, s_y = re.search(r"(19|20)\d\d", deck.get("founded_year", "")), re.search(r"(19|20)\d\d", site.get("founded_year", ""))
    if d_y and s_y and d_y.group() != s_y.group():
        add("Founding year", "warn", f"Deck says {d_y.group()}, website says {s_y.group()}.")

    stage, ask = deck.get("stage", ""), deck.get("ask_eur")
    if ask and stage in round_ranges:
        lo, hi = round_ranges[stage]
        ok = lo <= ask <= hi
        add("Round size vs stage", "ok" if ok else "warn",
            f"EUR {ask:,.0f} {'is typical' if ok else 'is unusual'} for {stage} (typical EUR {lo:,.0f}-{hi:,.0f}).")

    if not search.get("available"):
        add("Web search", "warn", "External search was unavailable, so claims could not be checked against the web.")
    else:
        independent = [h for h in hits if h["kind"] == "independent"]
        if independent:
            add("Independent coverage", "ok", f"{len(independent)} independent web source(s) mention the company.")
        else:
            add("Independent coverage", "info" if group in (None, "early") else "warn",
                "No independent source mentions the company" + (" - common for very early companies." if group in (None, "early") else
                                                                 " - unusual at this stage; verify the company's track record."))
        adverse = [h for h in hits if h["kind"] == "independent" and ADVERSE.search(f"{h['title']} {h['snippet']}")]
        if adverse:
            add("Possible adverse coverage", "warn", f"{len(adverse)} result(s) mention legal, financial or layoff issues - read them; "
                "they may be about another company or unrelated.", [{"title": h["title"], "url": h["url"]} for h in adverse[:4]])
    return out
