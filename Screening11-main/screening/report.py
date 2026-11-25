"""Markdown memo from a saved result (version 2)."""
from __future__ import annotations

STATUS_LABEL = {"verified": "Verified (independent source)", "consistent": "Company sources only", "contradicted": "Contradicted",
                "outdated": "Outdated", "unverified": "Unverified", "not_checked": "Not checked"}


def _cell(s: str) -> str:
    return str(s).replace("|", "/").replace("\n", " ")


def to_markdown(r: dict) -> str:
    if r.get("version") != 2:
        return f"# {r.get('company', 'Unknown')} - screening memo\n\nThis result was made with an earlier version of the app; re-run the screening for a full memo."
    p, b, cf, ctx = r["profile"], r["brief"], r["confidence"], r["context"]
    out = [f"# {r['company']} - screening memo", "",
           f"**{r['decision']}** · overall {r['overall']}/10 · confidence {cf['level']} · mandate fit {r['fit']['status']}",
           "", f"_{r['decision_reason']}_", "",
           f"Website: {r['website']} · Stage: {ctx['stage'] or 'not stated'} · Business model: {ctx['model_label']}", ""]
    out += ["## Summary", b["summary"] or "-", ""]
    out += ["## Strengths"] + [f"- {x}" for x in b["strengths"]] + ["", "## Concerns"] + [f"- {x}" for x in b["concerns"]] + [""]
    flags = [c for c in r["consistency"] if c["state"] in ("warn", "fail")]
    bad = [c for c in r["claims"] if c["status"] in ("contradicted", "outdated")]
    if flags or bad:
        out += ["## Red flags and discrepancies"]
        out += [f"- **{c['id']} {STATUS_LABEL[c['status']]}:** {c['text']} - {c['note']}" for c in bad]
        out += [f"- **{c['check']}:** {c['detail']}" for c in flags] + [""]
    out += ["## Claim verification", "| # | Claim | Source | Status | Evidence |", "|---|---|---|---|---|"]
    for c in r["claims"]:
        ev = "; ".join(f"[{e['id']}]({e['url']})" if e.get("url") else e["id"] for e in c["evidence"]) or "-"
        warn = " ⚠ quote not found in source" if not c["quote_found"] else ""
        out.append(f"| {c['id']} | {_cell(c['text'])}{warn} | {_cell(c['source_label'])} | {STATUS_LABEL[c['status']]} | {ev} |")
    out += ["", "## Scorecard", "| Dimension | Score | Weight (base → applied) | Why |", "|---|---|---|---|"]
    for k, d in r["dimensions"].items():
        w = r["weights"][k]
        score = f"{d['final']:g}" + (f" (−{d['penalty']:g} for {', '.join(d['penalty_claims'])})" if d["penalty"] else "")
        out.append(f"| {k.replace('_', ' ')} | {score} | {w['base']:g} → {w['applied']:g} | {_cell(d['reasoning'])} |")
    if r.get("adjustment"):
        out += ["", f"Adjustment: {r['adjustment']} (before: {r['overall_raw']})."]
    out += ["", "## Mandate fit"] + [f"- {c['name']}: {c['state']} - {c['detail']}" for c in r["fit"]["checks"]]
    out += ["", "## Company profile (as stated by the company)",
            f"- {p['one_liner'] or '-'}", f"- HQ: {p['hq_country'] or '-'} · founded: {p['founded_year'] or '-'} · raising: {p['ask_text'] or '-'}"]
    out += [f"- Founder: {f['name']} ({f['role']}) {f['background']}" for f in p["founders"]]
    if r["questions_for_founder"]:
        out += ["", "## Questions for the founder"] + [f"- {q}" for q in r["questions_for_founder"]]
    out += ["", "## Sources", f"- Website: {r['crawl']['pages_crawled']} pages crawled, {len(r['crawl']['pages_analysed'])} analysed"]
    out += [f"  - {pg['url']}" for pg in r["crawl"]["pages_analysed"]]
    out += [f"- Web search ({r['search']['provider']}): {len(r['search']['hits'])} relevant results"]
    out += [f"  - {h['id']} [{h['kind']}] {h['title']} - {h['url']}" for h in r["search"]["hits"][:20]]
    if r["issues"]:
        out += ["", "## Data quality and workflow issues"] + [f"- [{i['severity']}] {i['stage']}: {i['message']}" for i in r["issues"]]
    out += ["", f"_Model {r['model']} · {r['llm_calls']} AI calls · {r['seconds']} s. AI-assisted screening: verify key facts before relying on them._"]
    return "\n".join(out)
