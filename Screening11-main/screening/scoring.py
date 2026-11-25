"""Code-based scoring. The AI only scores single dimensions; everything here is plain, repeatable code driven by the
fund thesis (base weights, mandate, thresholds) and scoring_profiles.yaml (stage / business-model adjustments,
verification penalties, confidence rules). Every adjustment is returned with its reason."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent
PROFILES_PATH = ROOT / "scoring_profiles.yaml"

DIMENSIONS: dict[str, str] = {
    "timing": "why now: a market, technology or regulatory shift that opens the window",
    "problem": "how painful and urgent the problem is for a specific buyer",
    "wedge": "a sharp initial entry point; clearly better than the alternatives",
    "founder_market_fit": "founders' relevant background, credibility and domain insight",
    "product_love": "evidence users love it: retention, usage, referrals, NPS, pull",
    "execution_speed": "shipping velocity; milestones reached relative to time and money spent",
    "market": "market size and potential for category leadership",
    "moat_path": "a path to defensibility: data, network effects, switching costs, IP, brand",
    "traction": "revenue, users, pilots, growth - hard numbers",
    "business_model": "pricing, margins, unit economics",
    "distribution": "repeatable acquisition channels, CAC/payback signals, sales motion",
}


class ProfilesError(ValueError):
    pass


def load_profiles(path: Path = PROFILES_PATH) -> dict:
    """Load and validate scoring_profiles.yaml; a bad edit fails loudly with the reason instead of scoring wrongly."""
    from .models import CLAIM_CATEGORIES
    from .suggestions import STAGE_SLUGS
    try:
        p = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ProfilesError(f"scoring_profiles.yaml could not be read: {e}") from e
    errors = []

    def mults(where: str, m: dict) -> None:
        for dim, spec in (m or {}).items():
            if dim not in DIMENSIONS:
                errors.append(f"{where}: unknown dimension '{dim}'")
            elif not isinstance(spec, dict) or not isinstance(spec.get("x"), (int, float)) or not 0 < spec["x"] <= 3:
                errors.append(f"{where}.{dim}: needs x between 0 and 3")
            elif not str(spec.get("why", "")).strip():
                errors.append(f"{where}.{dim}: needs a 'why'")

    groups = p.get("stage_groups") or {}
    grouped = [s for v in groups.values() for s in v]
    for s in grouped:
        if s not in STAGE_SLUGS:
            errors.append(f"stage_groups: unknown stage '{s}'")
    for g in groups:
        mults(f"stage_multipliers.{g}", (p.get("stage_multipliers") or {}).get(g))
        if not (p.get("stage_expectations") or {}).get(g):
            errors.append(f"stage_expectations: missing '{g}'")
    if "other" not in (p.get("model_types") or {}):
        errors.append("model_types: must include 'other'")
    for k, mt in (p.get("model_types") or {}).items():
        if not mt.get("label"):
            errors.append(f"model_types.{k}: needs a label")
        mults(f"model_types.{k}", mt.get("multipliers"))
    v = p.get("verification") or {}
    for cat, dims in (v.get("category_dimensions") or {}).items():
        if cat not in CLAIM_CATEGORIES or any(d not in DIMENSIONS for d in dims or []):
            errors.append(f"verification.category_dimensions.{cat}: unknown category or dimension")
    for key in ("contradicted_penalty", "penalty_cap"):
        if not isinstance(v.get(key), (int, float)) or v[key] < 0:
            errors.append(f"verification.{key}: must be a number >= 0")
    if errors:
        raise ProfilesError("scoring_profiles.yaml is invalid: " + "; ".join(errors[:8]))
    return p


def stage_group(stage: str, prof: dict) -> Optional[str]:
    return next((g for g, stages in prof["stage_groups"].items() if stage in stages), None)


def applied_weights(base: dict[str, float], group: Optional[str], model_type: str, prof: dict) -> dict[str, dict]:
    """thesis weight x stage multiplier x business-model multiplier, with every factor and its reason."""
    out = {}
    stage_m = prof["stage_multipliers"].get(group) or {} if group else {}
    mt = prof["model_types"].get(model_type) or prof["model_types"]["other"]
    for d in DIMENSIONS:
        w, factors = float(base.get(d, 0)), []
        for source, spec in ((f"Stage: {group}", stage_m.get(d)), (f"Model: {mt['label']}", (mt.get("multipliers") or {}).get(d))):
            if spec:
                w *= spec["x"]
                factors.append({"source": source, "x": spec["x"], "why": spec["why"]})
        out[d] = {"base": round(float(base.get(d, 0)), 3), "applied": round(w, 3), "factors": factors}
    return out


def penalties(claims: list[dict], prof: dict) -> dict[str, dict]:
    """Contradicted (or outdated) high/medium claims lower the dimensions that rely on that kind of claim."""
    v = prof["verification"]
    out = {d: {"points": 0.0, "claims": []} for d in DIMENSIONS}
    pairs: list[set] = []
    for c in claims:
        if c.get("status") in ("contradicted", "outdated") and c.get("importance") in ("high", "medium"):
            ev = {e["id"] for e in c.get("evidence", [])}
            if ev and all(i.startswith("C") for i in ev):  # deck vs website disagreeing is ONE discrepancy, not two
                group = ev | {c["id"]}
                if any(group & p for p in pairs):
                    continue
                pairs.append(group)
            for d in v["category_dimensions"].get(c.get("category"), []):
                out[d]["claims"].append(c["id"])
    for d, p in out.items():
        p["points"] = min(v["penalty_cap"], v["contradicted_penalty"] * len(p["claims"]))
    return out


def overall_score(scores: dict[str, float], weights: dict[str, float]) -> float:
    total_w = sum(weights.values())
    if total_w <= 0:
        raise ValueError("weights must not all be zero")
    return round(sum(scores.get(k, 0) * w for k, w in weights.items()) / total_w, 2)


def outlier_adjustment(overall: float, scores: dict[str, float], prof: dict) -> tuple[float, Optional[str]]:
    o = prof["outlier"]
    m, t, d = (scores.get(k, 0) for k in ("market", "traction", "distribution"))
    if max(m, t, d) < o["cap_if_all_below"] and overall > o["cap_at"]:
        return o["cap_at"], f"capped at {o['cap_at']}: market, traction and distribution are all below {o['cap_if_all_below']}"
    if m >= o["boost_if_market_at_least"] and max(t, d) >= o["boost_if_traction_or_distribution_at_least"] and overall < 10:
        return min(10.0, round(overall + o["boost"], 2)), f"+{o['boost']}: strong market with strong traction or distribution"
    return overall, None


def confidence(coverage: int, claims: list[dict], search_ok: bool, website_ok: bool, prof: dict) -> dict:
    """How much to trust the result (separate from how good the deal is)."""
    c = prof["confidence"]
    checked = [x for x in claims if x.get("status") not in ("not_checked",) and x.get("importance") in ("high", "medium")]
    verified = sum(1 for x in checked if x["status"] == "verified")
    supported = sum(1 for x in checked if x["status"] in ("verified", "consistent"))
    share = verified / len(checked) if checked else 0.0
    reasons = [f"{coverage}/{len(DIMENSIONS)} dimensions had evidence",
               f"{verified}/{len(checked)} key claims verified by an independent source"]
    if not search_ok:
        reasons.append("web search was unavailable")
    if not website_ok:
        reasons.append("the website could not be analysed")
    if checked and supported / len(checked) < c["low_min_supported_share"]:
        reasons.append(f"only {supported}/{len(checked)} key claims are supported by any source")
    if (coverage <= c["low_max_coverage"] or not checked or not (search_ok or website_ok)
            or supported / len(checked) < c["low_min_supported_share"]):
        level = "Low"
    elif coverage >= c["high_min_coverage"] and share >= c["high_min_verified_share"] and search_ok and website_ok:
        level = "High"
    else:
        level = "Medium"
    return {"level": level, "verified_share": round(share, 2), "reasons": reasons}


# ---------- mandate fit (fund thesis) ----------
def _list_rule(items: list[str], what: str) -> str:
    return f"any {what}" if not items else ", ".join(items[:4]) + (f" (+{len(items) - 4} more)" if len(items) > 4 else "")


def fit_check(profile: dict, t, prof: dict, sources: dict | None = None) -> dict:
    """Compare the company with a fund thesis. Each check: ok | warn | unknown | fail, with what was compared and where the
    company's value came from. A "fail" needs a positively identified mismatch; anything unreadable is "unknown", never "fail"
    (a NO_FIT forces a PASS, so a wrong rejection costs a real deal). An empty thesis list means "no restriction"."""
    from .suggestions import find_countries, normalize_country, normalize_stage, parse_stages, term_hits
    src = sources or {}
    checks: list[dict] = []

    def add(name, state, value, rule, detail, key):
        checks.append({"name": name, "state": state, "value": value, "rule": rule, "detail": detail, "source": src.get(key, "")})

    # Geography ---------------------------------------------------------------------------------------------
    geo = [normalize_country(g) for g in t.geography]
    geo_l = {g.lower() for g in geo}
    hq_text, origin_text = profile.get("hq_country", ""), profile.get("founder_origin", "")
    hq, origin = find_countries(hq_text), find_countries(origin_text)
    rule = _list_rule(geo, "country")
    if not geo:
        add("Geography", "ok", hq_text or "not stated", rule, "The fund has no geographic restriction.", "hq_country")
    elif hq and hq[-1].lower() in geo_l:
        add("Geography", "ok", hq_text, rule, f"HQ in {hq[-1]}.", "hq_country")
    elif any(h.lower() in geo_l for h in hq):
        add("Geography", "warn", hq_text, rule, f"HQ text names several countries ({', '.join(hq)}); only one is in the fund's list. Confirm the HQ.", "hq_country")
    elif any(o.lower() in geo_l for o in origin):
        add("Geography", "ok", hq_text or "not stated", rule,
            f"HQ{' ' + ', '.join(hq) if hq else ''} is outside the fund's list, but the founders have a link to "
            f"{next(o for o in origin if o.lower() in geo_l)} ({origin_text[:70]}).", "founder_origin")
    elif hq:
        add("Geography", "fail", hq_text, rule, f"HQ in {', '.join(hq)} is not in the fund's list and no founder link to it was found.", "hq_country")
    elif hq_text:
        add("Geography", "unknown", hq_text, rule, f"'{hq_text}' does not name a country the app recognises - confirm the HQ country.", "hq_country")
    else:
        add("Geography", "unknown", "not stated", rule, "HQ country not stated in the materials.", "hq_country")

    # Stage ---------------------------------------------------------------------------------------------------
    core, check = [normalize_stage(x) for x in t.stages_core], [normalize_stage(x) for x in t.stages_check]
    stages = parse_stages(profile.get("stage", ""))
    rule = f"core: {_list_rule(core, 'stage')}" + (f"; double-check: {', '.join(check)}" if check else "")
    value = profile.get("stage") or "not stated"
    if not core and not check:
        add("Stage", "ok", value, "any stage", "The fund has no stage restriction.", "stage")
    elif not stages:
        add("Stage", "unknown", value, rule, "Stage not stated." if not profile.get("stage") else f"'{profile['stage']}' is not a funding stage the app recognises.", "stage")
    elif any(x in core for x in stages):
        add("Stage", "ok", value, rule, f"{next(x for x in stages if x in core)} is a core stage.", "stage")
    elif any(x in check for x in stages):
        add("Stage", "warn", value, rule, f"{next(x for x in stages if x in check)} is allowed but flagged for a second look.", "stage")
    else:
        add("Stage", "fail", value, rule, f"{', '.join(stages)} is outside the fund's stages.", "stage")

    # Sector ---------------------------------------------------------------------------------------------------
    text = " ".join(profile.get("sectors", [])) + " " + profile.get("one_liner", "")
    excluded, weak, strong = (term_hits(text, x) for x in (t.sectors_excluded, t.sectors_weak, t.sectors_strong))
    value = ", ".join(profile.get("sectors", [])[:4]) or "not stated"
    rule = f"target: {_list_rule(t.sectors_strong, 'sector')}" + (f"; excluded: {', '.join(t.sectors_excluded[:3])}" if t.sectors_excluded else "")
    if excluded:
        add("Sector", "fail", value, rule, f"'{excluded[0]}' is on the fund's excluded list.", "sectors")
    elif weak:
        add("Sector", "warn", value, rule, f"risky area: {', '.join(weak[:3])}" + (f" (also matches target: {strong[0]})" if strong else ""), "sectors")
    elif not t.sectors_strong:
        add("Sector", "ok", value, rule, "The fund has no sector focus.", "sectors")
    elif strong:
        add("Sector", "ok", value, rule, f"matches {', '.join(strong[:3])}.", "sectors")
    elif profile.get("sectors") or profile.get("one_liner"):
        add("Sector", "warn", value, rule, "Not in the fund's target sectors.", "sectors")
    else:
        add("Sector", "unknown", value, rule, "Sector not stated.", "sectors")

    # Round size -----------------------------------------------------------------------------------------------
    ask, lo, hi = profile.get("ask_eur"), t.ticket_min_eur, t.ticket_max_eur
    rule = "no limits" if not (lo or hi) else f"EUR {lo:,.0f} - {hi:,.0f} per ticket" if hi else f"at least EUR {lo:,.0f}"
    if not (lo or hi):
        add("Round size", "ok", f"EUR {ask:,.0f}" if ask else "not stated", rule, "The fund sets no round-size limits.", "ask_text")
    elif not ask:
        add("Round size", "unknown", "not stated", rule, "The round size is not stated.", "ask_text")
    elif lo and ask < lo:
        add("Round size", "warn", f"EUR {ask:,.0f}", rule, f"The round is smaller than the fund's minimum ticket (EUR {lo:,.0f}).", "ask_text")
    elif hi and ask > hi * 3:
        add("Round size", "warn", f"EUR {ask:,.0f}", rule, f"The round is more than 3x the fund's largest ticket (EUR {hi:,.0f}); the fund could not lead it.", "ask_text")
    else:
        add("Round size", "ok", f"EUR {ask:,.0f}", rule, "Within the fund's range.", "ask_text")

    states = {c["state"] for c in checks}
    return {"status": "NO_FIT" if "fail" in states else "CHECK" if states & {"warn", "unknown"} else "FIT", "checks": checks}


def evaluate(raw_scores: dict[str, int], coverage: int, fit: dict, thesis, context: dict, claims: list[dict],
             prof: dict, search_ok: bool = True, website_ok: bool = True) -> dict:
    """Everything that turns AI dimension scores into a decision. Also used for instant what-if re-scoring."""
    weights = applied_weights(thesis.weights, context.get("stage_group"), context.get("model_type", "other"), prof)
    pens = penalties(claims, prof)
    scores = {d: max(1.0, raw_scores[d] - pens[d]["points"]) for d in DIMENSIONS}
    raw_overall = overall_score(scores, {d: w["applied"] for d, w in weights.items()})
    overall, adjustment = outlier_adjustment(raw_overall, scores, prof)

    base = "ADVANCE" if overall >= thesis.advance_at else "MAYBE" if overall >= thesis.maybe_at else "PASS"
    reasons = [f"Overall {overall}/10 against thresholds {thesis.advance_at} (advance) / {thesis.maybe_at} (maybe)."]
    decision = base
    contradicted = [c["id"] for c in claims if c.get("status") in ("contradicted", "outdated") and c.get("importance") in ("high", "medium")]
    if fit["status"] == "NO_FIT":
        decision = "PASS"
        reasons = [f"Outside the fund's mandate ({', '.join(c['name'] for c in fit['checks'] if c['state'] == 'fail')}), whatever the score."]
    else:
        if base == "ADVANCE" and contradicted and prof["verification"].get("block_advance_on_contradiction"):
            decision = "MAYBE"
            reasons.append(f"Held at MAYBE: {len(contradicted)} key claim(s) contradicted by evidence ({', '.join(contradicted)}) - resolve first.")
        if fit["status"] == "CHECK" and decision != "PASS":
            reasons.append("Some mandate checks are open - confirm them before spending time.")
        if coverage < 6 and decision != "ADVANCE":
            reasons.append(f"Thin material: only {coverage}/{len(DIMENSIONS)} dimensions had evidence, so the score is conservative.")
    return {
        "decision": decision, "decision_reason": " ".join(reasons), "overall": overall, "overall_raw": raw_overall,
        "adjustment": adjustment, "weights": weights,
        "scores": {d: {"raw": raw_scores[d], "final": scores[d], "penalty": pens[d]["points"], "penalty_claims": pens[d]["claims"]}
                   for d in DIMENSIONS},
        "confidence": confidence(coverage, claims, search_ok, website_ok, prof),
    }
