import io
import zipfile

import httpx
import pytest

from screening import intake, suggestions as sg
from screening.crawler import crawl, priority_of
from screening.llm import LLMError, Pacer, parse_json
from screening.models import Assessment, Dim, Extraction
from screening.pipeline import Inputs, analyse
from screening.prompts import extract_system
from screening.scoring import (DIMENSIONS, ProfilesError, applied_weights, confidence, evaluate, fit_check, load_profiles,
                               overall_score, penalties)
from screening.thesis import Thesis, ThesisError, build_thesis, check_thesis, parse_thesis
from screening.verify import consistency_checks, finalize_claims, quote_found
from screening.websearch import classify, search
from screening.models import Check
from tests.fakes import (DECK_TEXT, HttpError, assessment, fake_crawl, fake_search, make_llm, make_pdf, reply, router)

T = build_thesis(sg.TEMPLATES[0]['data'])  # CEE seed fund
P = load_profiles()


def claim(id, status="pending", category="traction", importance="high", source="deck"):
    return {"id": id, "text": f"claim {id}", "category": category, "importance": importance, "source": source,
            "source_label": source, "status": status, "evidence": [], "note": ""}


# ---------- scoring engine ----------
def test_weights_adapt_to_stage_and_model_with_reasons():
    early = applied_weights(T.weights, "early", "b2b_saas", P)
    growth = applied_weights(T.weights, "growth", "b2b_saas", P)
    assert early["founder_market_fit"]["applied"] == pytest.approx(T.weights["founder_market_fit"] * 1.4)
    assert early["traction"]["applied"] < growth["traction"]["applied"]            # traction matters more later
    assert all(f["why"] for f in early["founder_market_fit"]["factors"])            # every change is explained
    plain = applied_weights(T.weights, None, "other", P)
    assert all(w["applied"] == w["base"] and not w["factors"] for w in plain.values())


def test_contradicted_claims_penalise_related_dimensions_with_a_cap():
    claims = [claim("C1", "contradicted", "revenue"), claim("C2", "contradicted", "traction"), claim("C3", "contradicted", "customers"),
              claim("C4", "contradicted", "traction", importance="low"), claim("C5", "verified", "team")]
    p = penalties(claims, P)
    assert p["traction"]["points"] == 4 and set(p["traction"]["claims"]) == {"C1", "C2", "C3"}  # capped at 4
    assert p["business_model"]["points"] == 2 and p["founder_market_fit"]["points"] == 0


def test_a_deck_vs_website_disagreement_is_penalised_once():
    a = {**claim("C2", "contradicted", "traction"), "evidence": [{"id": "C15"}]}
    b = {**claim("C15", "contradicted", "traction", source="website"), "evidence": [{"id": "C2"}]}
    ext = {**claim("C7", "contradicted", "traction"), "evidence": [{"id": "S4"}]}
    assert penalties([a, b], P)["traction"]["points"] == 2
    assert penalties([a, b, ext], P)["traction"]["points"] == 4


def test_evaluate_decisions():
    scores = {k: 8 for k in DIMENSIONS}
    fit_ok = {"status": "FIT", "checks": []}
    ctx = {"stage_group": "early", "model_type": "b2b_saas"}
    assert evaluate(scores, 11, fit_ok, T, ctx, [], P)["decision"] == "ADVANCE"
    held = evaluate(scores, 11, fit_ok, T, ctx, [claim("C1", "contradicted", "funding")], P)
    assert held["decision"] == "MAYBE" and "C1" in held["decision_reason"]            # contradiction blocks ADVANCE
    no_fit = {"status": "NO_FIT", "checks": [{"name": "Stage", "state": "fail"}]}
    assert evaluate(scores, 11, no_fit, T, ctx, [], P)["decision"] == "PASS"
    low = evaluate({k: 3 for k in DIMENSIONS}, 2, fit_ok, T, ctx, [], P)
    assert low["decision"] == "PASS" and "Thin material" in low["decision_reason"]


def test_confidence_levels():
    verified = [claim(f"C{i}", "verified") for i in range(5)]
    assert confidence(10, verified, True, True, P)["level"] == "High"
    assert confidence(10, verified, False, False, P)["level"] == "Low"
    assert confidence(3, verified, True, True, P)["level"] == "Low"
    assert confidence(7, [claim("C1", "consistent"), claim("C2", "unverified")], True, True, P)["level"] == "Medium"
    assert confidence(9, [claim(f"C{i}", "unverified") for i in range(6)], True, True, P)["level"] == "Low"  # nothing supported


def test_profiles_file_is_validated(tmp_path):
    bad = tmp_path / "p.yaml"
    bad.write_text((sg.__file__ and open(load_profiles.__globals__["PROFILES_PATH"]).read()).replace("founder_market_fit: {x: 1.4", "founderz: {x: 1.4"))
    with pytest.raises(ProfilesError, match="founderz"):
        load_profiles(bad)


def test_overall_and_fit():
    assert overall_score({"a": 10, "b": 0}, {"a": 1, "b": 1}) == 5.0
    us = T.model_copy(update={"geography": ["United States"], "stages_core": ["series-b"], "stages_check": []})
    f = fit_check({"hq_country": "Germany", "founder_origin": "from the USA", "stage": "Series B", "sectors": ["ai"]}, us, P)
    assert [c["state"] for c in f["checks"][:2]] == ["ok", "ok"]                         # audit: alias in founder origin
    f = fit_check({"hq_country": "USA", "stage": "later", "sectors": ["retail logistics"]}, T, P)
    assert f["status"] == "NO_FIT"                                                       # a recognised country outside the list


# ---------- models ----------
def test_dimension_without_score_is_rejected():
    with pytest.raises(ValueError):
        Dim(reasoning="x")                                                               # audit: no silent score of 1


def test_extraction_is_lenient_about_format_strict_about_meaning():
    ex = Extraction(**{"profile": {"company_name": "unknown", "founders": ["Ann", "N/A"], "ask_eur": "-5"},
                       "claims": [{"text": "x", "category": "weird", "importance": "huge"}, {"text": ""}, "junk"]})
    assert ex.profile.company_name == "" and [f.name for f in ex.profile.founders] == ["Ann"] and ex.profile.ask_eur is None
    assert len(ex.claims) == 1 and ex.claims[0].category == "other" and ex.claims[0].importance == "medium"
    with pytest.raises(ValueError, match="missing dimensions"):
        Assessment(dimensions={"timing": {"score": 5}})


# ---------- verification rules ----------
def test_quote_check():
    src = "Founders: Anna Nowak (CEO, ex-ABB). We have 40 paying customers and EUR 1.2M ARR."
    assert quote_found("We have 40 paying customers", src)
    assert quote_found("we have 40 PAYING customers!", src)
    assert not quote_found("We have 400 paying customers worldwide today", src)
    assert not quote_found("", src)


def test_evidence_rules_never_overstate():
    claims = [claim("C1"), claim("C2"), claim("C3"), claim("C4"), claim("C5", source="website"), claim("C6")]
    hits = [{"id": "S1", "kind": "independent", "title": "News", "url": "u1", "snippet": "s"},
            {"id": "S2", "kind": "profile", "title": "LinkedIn", "url": "u2", "snippet": "s"}]
    checks = [Check(claim_id="C1", status="supported", evidence_ids=["S1"]),
              Check(claim_id="C2", status="supported", evidence_ids=["S2", "C5"]),     # profile + company -> consistent only
              Check(claim_id="C3", status="contradicted", evidence_ids=[]),            # no evidence -> unverified
              Check(claim_id="C4", status="supported", evidence_ids=["C4", "C6", "S9"]),  # self / same source / unknown -> dropped
              Check(claim_id="C5", status="supported", evidence_ids=["C1"])]
    issues = []
    finalize_claims(claims, checks, hits, issues)
    st = {c["id"]: c["status"] for c in claims}
    assert st == {"C1": "verified", "C2": "consistent", "C3": "unverified", "C4": "unverified", "C5": "consistent", "C6": "unverified"}
    assert any("C3" in i["message"] for i in issues) and any("S9" in i["message"] for i in issues)


def test_consistency_checks():
    deck = {"company_name": "Acme", "founders": [{"name": "Anna Nowak"}, {"name": "Ghost Person"}], "hq_country": "Poland",
            "founded_year": "2021", "stage": "seed", "ask_eur": 90_000_000}
    site = {"hq_country": "Germany", "founded_year": "2019"}
    search_res = {"available": True, "hits": [{"kind": "independent", "title": "Acme sued over fraud", "snippet": "", "url": "x"}]}
    out = {c["check"]: c for c in consistency_checks(deck, site, "Acme. Team: Anna Nowak.", True, search_res, "growth", P["round_size_eur"])}
    assert out["Founder: Anna Nowak"]["state"] == "ok" and out["Founder: Ghost Person"]["state"] == "warn"
    assert out["HQ country"]["state"] == "warn" and out["Founding year"]["state"] == "warn"
    assert out["Round size vs stage"]["state"] == "warn" and out["Possible adverse coverage"]["state"] == "warn"
    early = {c["check"]: c for c in consistency_checks(deck, {}, "", True, {"available": True, "hits": []}, "early", {})}
    assert early["Independent coverage"]["state"] == "info"                              # normal for early companies
    dead = {c["check"]: c for c in consistency_checks(deck, {}, "", False, {"available": False, "hits": []}, None, {})}
    assert dead["Website"]["state"] == "fail" and dead["Web search"]["state"] == "warn"


def test_evidence_candidates_and_number_mismatches():
    from screening.verify import candidates, counts, number_mismatches
    deck = {**claim("C1"), "text": "GroqCloud is used by over 170,000 developers.", "source_label": "Pitch deck"}
    site = {**claim("C2", source="website"), "text": "More than 70,000 developers use GroqCloud today.", "source_label": "Website"}
    other = {**claim("C3", source="website"), "text": "Groq operates 13 data centers.", "source_label": "Website"}
    hits = [{"id": "S1", "title": "GroqCloud developers", "snippet": "GroqCloud passed 70,000 developers", "kind": "independent"},
            {"id": "S2", "title": "Weather today", "snippet": "rain", "kind": "independent"}]
    cands = candidates(deck, [deck, site, other], hits, "Groq")
    assert "C2" in cands and "S1" in cands and "S2" not in cands and "C1" not in cands
    assert counts("over 170,000 developers and 1.5 million users") == {"developers": 170000, "users": 1500000}
    mism = number_mismatches([deck, site, other])
    assert len(mism) == 1 and "170,000" in mism[0]["detail"] and "70,000" in mism[0]["detail"]
    assert number_mismatches([deck, {**site, "text": "over 171,000 developers"}]) == []  # within 10%: same figure


# ---------- intake ----------
def test_documents():
    assert "Acme Robotics" in intake.pdf_text(make_pdf(DECK_TEXT))
    with pytest.raises(intake.IntakeError, match="scanned"):
        intake.pdf_text(make_pdf("hi"))
    with pytest.raises(intake.IntakeError, match="not a PDF"):
        intake.pdf_text(b"hello")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   "<w:body><w:p><w:r><w:t>Revenue 2025: EUR 2M</w:t></w:r></w:p></w:body></w:document>")
    assert intake.resource_text("plan.docx", buf.getvalue()) == "Revenue 2025: EUR 2M"
    assert "ARR" in intake.resource_text("notes.md", b"# Notes\nARR 1M")
    with pytest.raises(intake.IntakeError, match="unsupported"):
        intake.resource_text("x.exe", b"MZ")


def test_upload_and_url_validation():
    with pytest.raises(intake.IntakeError, match="PDF"):
        intake.check_upload("deck.docx", b"PK..", deck=True)
    with pytest.raises(intake.IntakeError, match="empty"):
        intake.check_upload("deck.pdf", b"", deck=True)
    assert intake.normalize_url("acme.ai") == "https://acme.ai"
    for bad in ("", "not a url", "localhost"):
        with pytest.raises(intake.IntakeError):
            intake.normalize_url(bad)


def test_truncation_keeps_start_and_end():
    text = "START " + "x" * 5000 + " TEAM SLIDE AT THE END"
    out, cut = intake.truncate(text, 1000)
    assert cut and out.startswith("START") and out.endswith("TEAM SLIDE AT THE END") and len(out) < 1100


# ---------- crawler ----------
SITE = {
    "/": '<html><title>Acme</title><body>Acme robots for factories. ' + "Lots of text about the product. " * 5 +
         '<a href="/about">About</a> <a href="/pricing">Pricing</a> <a href="/missing">Old</a> <a href="/de/about">DE</a>'
         '<a href="/private/x">Secret</a> <a href="https://other.com/x">Ext</a> <a href="/blog?page=1">b1</a>'
         '<a href="/blog?page=2">b2</a> <a href="/blog?page=3">b3</a> <a href="/blog?page=4">b4</a> <a href="/logo.png">img</a></body></html>',
    "/about": "<html><title>About</title><body>" + "Founded in 2021 by Anna Nowak and Piotr Zielinski in Warsaw. " * 3 + "</body></html>",
    "/pricing": "<html><title>Pricing</title><body>" + "Plans start at EUR 99 per month per robot. " * 3 + "</body></html>",
    "/team": "<html><title>Team</title><body>" + "Our team of 25 engineers builds inspection robots. " * 3 + "</body></html>",
    "/robots.txt": "User-agent: *\nDisallow: /private\nSitemap: https://acme.test/sitemap.xml",
    "/sitemap.xml": "<urlset><url><loc>https://acme.test/team</loc></url></urlset>",
}


def mock_client():
    def handler(request: httpx.Request):
        path = request.url.path + (("?" + request.url.query.decode()) if request.url.query else "")
        body = SITE.get(request.url.path)
        if request.url.path == "/blog":
            body = "<html><title>Blog</title><body>" + f"Blog page {path} with enough text to keep. " * 3 + "</body></html>"
        if body is None:
            return httpx.Response(404, text="not found")
        ctype = "text/plain" if path.endswith((".txt", ".xml")) else "text/html"
        return httpx.Response(200, text=body, headers={"content-type": ctype})
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_crawler_follows_rules():
    r = crawl("https://acme.test/", client=mock_client(), check_host=lambda h: True, max_pages=10)
    urls = {p.url for p in r.pages}
    assert r.reachable and {"https://acme.test/", "https://acme.test/about", "https://acme.test/pricing", "https://acme.test/team"} <= urls
    assert "https://acme.test/team" in urls and r.sitemap_urls == 1                    # found via sitemap
    assert not any("/private" in u or "/de/" in u or "other.com" in u for u in urls)
    assert any(e["url"].endswith("/missing") and "404" in e["error"] for e in r.errors)  # broken link reported
    assert r.skipped.get("disallowed by robots.txt") == 1 and r.skipped.get("other-language copy") == 1
    assert sum("/blog" in u for u in urls) <= 3                                           # pagination / section caps
    assert priority_of("https://x.com/blog/team-management") == 1 and priority_of("https://x.com/about") == 6


def test_crawler_refuses_private_addresses_and_handles_dead_sites():
    r = crawl("https://acme.test/", client=mock_client(), check_host=lambda h: False)
    assert not r.reachable and "not a public internet address" in r.errors[0]["error"]
    dead = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(503)))
    r = crawl("https://acme.test/", client=dead, check_host=lambda h: True)
    assert not r.reachable and r.stopped_reason == "homepage unreachable"


def test_crawler_blocks_redirects_to_private_hosts_and_reports_oversize_pages():
    def redirect(req):
        if req.url.host == "acme.test":
            return httpx.Response(302, headers={"location": "http://internal.test/admin"})
        return httpx.Response(200, text="<html>secret " * 50, headers={"content-type": "text/html"})
    r = crawl("https://acme.test/", client=httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=True),
              check_host=lambda h: h == "acme.test")
    assert not r.reachable and not r.pages and "public internet address" in r.errors[0]["error"]   # the second hop is checked
    big = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"<html>" + b"x" * 3_000_000,
                                                                                headers={"content-type": "text/html"})))
    r = crawl("https://acme.test/", client=big, check_host=lambda h: True)
    assert not r.reachable and "larger than 2 MB" in r.errors[0]["error"]


def test_founder_names_match_whole_words_only():
    deck = {"company_name": "Acme", "founders": [{"name": "Ann"}], "hq_country": "", "founded_year": "", "stage": ""}
    out = {c["check"]: c for c in consistency_checks(deck, {}, "annual revenue grew", True, {"available": True, "hits": []}, None, {})}
    assert out["Founder: Ann"]["state"] == "warn"                                       # "ann" is inside "annual": not a match
    out = {c["check"]: c for c in consistency_checks(deck, {}, "Our CEO Ann leads", True, {"available": True, "hits": []}, None, {})}
    assert out["Founder: Ann"]["state"] == "ok"


# ---------- web search ----------
def test_search_classifies_filters_and_survives_failures():
    assert classify("https://careers.acme.io/x", "acme.io") == "company"
    assert classify("https://www.linkedin.com/in/x", "acme.io") == "profile"
    assert classify("https://techcrunch.com/acme", "acme.io") == "independent"

    def backend(q, n):
        if "lawsuit" in q:
            raise RuntimeError("blocked")
        return [{"title": "Acme Robotics raises EUR 3M", "href": "https://news.example/a", "body": "..."},
                {"title": "Unrelated ACMEX corp", "href": "https://other.example/b", "body": "nothing"}]
    r = search("Acme Robotics", "acmerobotics.io", ["Anna Nowak"], True, backend=backend, retry_delay=0)
    assert r["available"] and len(r["hits"]) == 1 and r["irrelevant"] >= 1 and len(r["failed"]) == 1
    down = search("Acme", "acme.io", [], False, backend=lambda q, n: (_ for _ in ()).throw(RuntimeError("x")), retry_delay=0)
    assert not down["available"] and down["hits"] == []


# ---------- AI engine: failover across providers ----------
from pydantic import BaseModel  # noqa: E402


class S(BaseModel):
    summary: str


OK = lambda text="x": reply('{"summary": "%s"}' % text)  # noqa: E731


def test_a_rate_limited_provider_is_skipped_and_stays_skipped():
    llm, sleeps = make_llm([HttpError(429, {"retry-after": "30"}), OK("from B"), OK("B again")], n=2)
    assert llm.json("s", "u", S).summary == "from B"
    assert llm.last_model == "AI-2 / model-2" and llm.events[0]["from"] == "AI-1 / model-1" and llm.events[0]["reason"] == "rate limited"
    assert llm.json("s", "u", S).summary == "B again"
    assert [c["model"] for c in llm.http_calls] == ["model-1", "model-2", "model-2"]          # the second call did not even try A
    assert llm.health.status(llm.candidates[0]) == {"cooling": True, "reason": "rate", "seconds": 30} and sleeps == []


@pytest.mark.parametrize("failure,why", [(HttpError(401), "key was rejected"), (HttpError(402), "out of quota"), (HttpError(500), "server error"),
                                          (HttpError(529), "server error"), (HttpError(404), "not available"),
                                          (httpx.ReadTimeout("slow"), "too slow"), (httpx.ConnectError("down"), "could not be reached"),
                                          (RuntimeError("bug in a transport"), "unexpected error"),
                                          (HttpError(400, message="You exceeded your current quota"), "out of quota")])
def test_every_kind_of_failure_fails_over(failure, why):
    llm, _ = make_llm([failure, OK("B")], n=2)
    assert llm.json("s", "u", S).summary == "B" and why in llm.events[0]["reason"]


def test_a_slow_provider_gets_a_shorter_timeout_only_while_a_fallback_exists():
    llm, _ = make_llm([OK(), OK()], n=2, timeout=100, soft_timeout=40)
    llm.json("s", "u", S)
    assert llm.http_calls[0]["timeout"]["read"] == 40                                          # AI-1 has AI-2 behind it
    llm2, _ = make_llm([HttpError(500), OK()], n=2, timeout=100, soft_timeout=40)
    llm2.json("s", "u", S)
    assert [c["timeout"]["read"] for c in llm2.http_calls] == [40, 100]                        # the last provider left gets the full time


def test_when_everything_is_rate_limited_it_waits_once_then_succeeds():
    llm, sleeps = make_llm([HttpError(429, {"retry-after": "7"}), OK("hi")])
    assert llm.json("s", "u", S).summary == "hi" and sleeps == [7.5]
    assert llm.health.status(llm.candidates[0])["cooling"] is False


def test_when_nothing_can_answer_the_error_names_every_provider():
    llm, _ = make_llm([HttpError(401), HttpError(429, {"retry-after": "500"})], n=2)
    with pytest.raises(LLMError) as e:
        llm.json("s", "u", S)
    msg = str(e.value)
    assert "No AI provider could answer" in msg and "AI-1 / model-1: the API key was rejected" in msg and "AI-2 / model-2: rate limited" in msg


def test_an_invalid_reply_is_repaired_once_then_the_next_provider_is_used():
    llm, _ = make_llm([reply("not json"), OK("fixed")])
    assert llm.json("s", "u", S).summary == "fixed" and "invalid" in llm.http_calls[1]["messages"][-1]["content"]
    llm, _ = make_llm([reply("nope"), reply("still nope"), OK("B")], n=2)
    assert llm.json("s", "u", S).summary == "B" and llm.events[0]["reason"] == "returned an invalid answer"


def test_optional_steps_can_limit_how_many_providers_they_try():
    llm, sleeps = make_llm([HttpError(500), HttpError(500), OK()], n=3)
    with pytest.raises(LLMError):
        llm.json("s", "u", S, max_providers=2)
    assert len(llm.http_calls) == 2 and sleeps == []                                            # and server errors are not waited out


def test_a_provider_comes_back_after_its_cooldown():
    llm, sleeps = make_llm([HttpError(500), OK("B"), OK("B2"), OK("A is back")], n=2)
    llm.json("s", "u", S)
    llm.json("s", "u", S)
    assert llm.http_calls[-1]["model"] == "model-2"
    llm.sleep(100)                                                                              # the server-error cooldown (90s) passes
    assert llm.json("s", "u", S).summary == "A is back" and llm.http_calls[-1]["model"] == "model-1"


def test_budget_depends_on_the_first_providers_limits():
    assert make_llm([], tpm=8000)[0].budget_chars(1800, 2200) < 17_000                       # a tight free tier
    assert make_llm([], tpm=None)[0].budget_chars(1800, 2200) > 40_000                       # a normal paid key reads much more
    llm, _ = make_llm([HttpError(429, {"retry-after": "60"}), OK()], n=2)
    llm.candidates[0].tpm = 8000
    llm.json("s", "u", S)
    assert llm.budget_chars(1800, 2200) > 40_000                                               # primary moved on to the provider without a limit


def test_pacing_only_applies_to_providers_with_a_token_limit():
    llm, sleeps = make_llm([reply('{"summary": "x"}', tokens=900), OK()], tpm=1000)
    llm.json("s", "u", S, max_tokens=800)
    llm.json("s", "u", S, max_tokens=800)
    assert sleeps and sleeps[0] > 0                                                              # the second call had to wait for the window
    llm, sleeps = make_llm([OK(), OK()], tpm=None)
    llm.json("s", "u", S, max_tokens=800)
    llm.json("s", "u", S, max_tokens=800)
    assert sleeps == []


def test_no_keys_gives_a_clear_message():
    from screening.llm import LLM
    with pytest.raises(LLMError, match="AI keys"):
        LLM([])


# ---------- each vendor's wire format ----------
import json as _json  # noqa: E402
from screening import keys as K, providers as PR  # noqa: E402


def one_shot(cand, handler):
    seen = {}

    def h(request):
        seen.update(url=str(request.url), headers=dict(request.headers), body=_json.loads(request.content) if request.content else None, method=request.method)
        return handler(request)
    return PR.complete(httpx.Client(transport=httpx.MockTransport(h)), cand, "SYS", [{"role": "user", "content": "hi"}], 500, "low", 30.0), seen


def test_openai_format_and_reasoning_models():
    c = PR.Candidate("openai", "OpenAI", "openai", "https://api.openai.com/v1", "sk-test", "gpt-4o-mini")
    (text, tokens), seen = one_shot(c, lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}], "usage": {"total_tokens": 42}}))
    assert (text, tokens) == ("{}", 42) and seen["url"] == "https://api.openai.com/v1/chat/completions" and seen["headers"]["authorization"] == "Bearer sk-test"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "SYS"} and seen["body"]["max_completion_tokens"] == 500 and seen["body"]["temperature"] == 0.1
    assert seen["body"]["response_format"] == {"type": "json_object"} and "reasoning_effort" not in seen["body"]
    _, seen = one_shot(PR.Candidate("openai", "OpenAI", "openai", "https://api.openai.com/v1", "k", "gpt-5-mini"),
                       lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]}))
    assert "temperature" not in seen["body"] and seen["body"]["reasoning_effort"] == "low"      # reasoning models take no temperature


def test_openai_compatible_services_use_max_tokens_and_openrouter_headers():
    c = PR.Candidate("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1", "sk-or", "openai/gpt-4o-mini")
    _, seen = one_shot(c, lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]}))
    assert seen["body"]["max_tokens"] == 500 and "max_completion_tokens" not in seen["body"] and seen["headers"]["x-title"] == "Deal Screening"


def test_parameters_a_model_rejects_are_dropped_and_retried():
    calls = []

    def h(request):
        body = _json.loads(request.content)
        calls.append(sorted(body))
        if "temperature" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported value: 'temperature' does not support 0.1 with this model"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
    (text, _), _ = one_shot(PR.Candidate("custom", "X", "openai", "https://x.test/v1", "k" * 8, "m"), h)
    assert text == "{}" and "temperature" in calls[0] and "temperature" not in calls[1]


def test_anthropic_format():
    c = PR.Candidate("anthropic", "Anthropic", "anthropic", "https://api.anthropic.com", "sk-ant-x", "claude-sonnet-5-5")
    resp = {"content": [{"type": "text", "text": '{"a": '}, {"type": "text", "text": "1}"}], "usage": {"input_tokens": 10, "output_tokens": 5}}
    (text, tokens), seen = one_shot(c, lambda r: httpx.Response(200, json=resp))
    assert (text, tokens) == ('{"a": 1}', 15) and seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["x-api-key"] == "sk-ant-x" and seen["headers"]["anthropic-version"] == "2023-06-01"
    assert seen["body"]["system"] == "SYS" and seen["body"]["messages"] == [{"role": "user", "content": "hi"}] and seen["body"]["max_tokens"] == 500


def test_gemini_format():
    c = PR.Candidate("gemini", "Gemini", "gemini", "https://generativelanguage.googleapis.com/v1beta", "AIza-test", "gemini-2.0-flash")
    resp = {"candidates": [{"content": {"parts": [{"text": "hidden", "thought": True}, {"text": '{"a": 1}'}]}}], "usageMetadata": {"totalTokenCount": 77}}
    (text, tokens), seen = one_shot(c, lambda r: httpx.Response(200, json=resp))
    assert (text, tokens) == ('{"a": 1}', 77) and seen["url"].endswith("/models/gemini-2.0-flash:generateContent") and seen["headers"]["x-goog-api-key"] == "AIza-test"
    assert seen["body"]["systemInstruction"]["parts"][0]["text"] == "SYS" and seen["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert seen["body"]["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]
    with pytest.raises(PR.ProviderError, match="blocked|SAFETY"):
        one_shot(c, lambda r: httpx.Response(200, json={"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}))


def test_errors_are_classified_whichever_vendor_sent_them():
    cls = lambda status, body, headers=None: PR.error_from_response(httpx.Response(status, json=body, headers=headers or {}))  # noqa: E731
    assert cls(401, {"error": {"message": "bad key"}}).kind == "auth" and cls(403, {}).kind == "auth"
    assert cls(429, {"error": {"message": "slow down"}}, {"retry-after": "12"}).retry_after == 12
    assert cls(429, {"error": {"type": "insufficient_quota", "message": "You exceeded your current quota, check billing"}}).kind == "quota"
    assert cls(400, {"error": {"message": "Your credit balance is too low"}}).kind == "quota"
    assert cls(529, {"error": {"message": "Overloaded"}}).kind == "server" and cls(404, {}).kind == "not_found" and cls(400, {}).kind == "bad_request"
    assert cls(400, [{"error": {"message": "x"}}]).kind == "bad_request"                        # Gemini sometimes wraps errors in a list


def test_model_discovery_picks_the_best_available_model():
    models = {"openai": ["gpt-4o", "gpt-4o-mini", "gpt-5-mini", "gpt-5.1-mini", "text-embedding-3-small", "gpt-4o-audio-preview", "dall-e-3", "whisper-1"],
              "anthropic": ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"], "gemini": ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.5-pro"]}
    assert PR.pick_model("openai", models["openai"]) == "gpt-5.1-mini" and PR.pick_model("anthropic", models["anthropic"]) == "claude-sonnet-5-5"
    assert PR.pick_model("gemini", models["gemini"]) == "gemini-2.5-flash" and PR.pick_model("anthropic", ["something-else"]) == "claude-sonnet-5-5"

    def serve(r):
        if "anthropic" in str(r.url):
            return httpx.Response(200, json={"data": [{"id": i} for i in models["anthropic"]]})
        if "googleapis" in str(r.url):
            return httpx.Response(200, json={"models": [{"name": "models/" + i, "supportedGenerationMethods": ["generateContent"]} for i in models["gemini"]]
                                             + [{"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]}]})
        return httpx.Response(200, json={"data": [{"id": i} for i in models["openai"]]})
    http = httpx.Client(transport=httpx.MockTransport(serve))
    assert sorted(PR.list_models(http, PR.Candidate("openai", "O", "openai", "https://api.openai.com/v1", "k", "m"))) == ["gpt-4o", "gpt-4o-mini", "gpt-5-mini", "gpt-5.1-mini"]
    assert "embedding-001" not in PR.list_models(http, PR.Candidate("gemini", "G", "gemini", "https://generativelanguage.googleapis.com/v1beta", "k", "m"))
    assert len(PR.list_models(http, PR.Candidate("anthropic", "A", "anthropic", "https://api.anthropic.com", "k", "m"))) == 3


# ---------- AI keys ----------
def row(**kw):
    return {"id": 1, "provider": "openai", "label": "OpenAI", "model": "", "base_url": "", "api_key": "sk-secret-abcd", "source": "app", "enabled": 1, "priority": 1010, **kw}


def test_keys_become_candidates_and_never_leak(tmp_path):
    from screening.llm import Health
    cs = K.candidates_for(row(provider="groq", api_key="gsk_abcdefgh"), {})
    assert [c.model for c in cs] == ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"] and cs[0].tpm == 8000          # Groq: free models in order
    assert K.candidates_for(row(provider="groq", api_key="gsk_abcdefgh", model="my-model"), {})[0].model == "my-model"
    assert K.candidates_for(row(), {})[0].tpm is None and K.candidates_for(row(enabled=0), {}) == [] and K.candidates_for(row(api_key=""), {}) == []
    assert K.candidates_for(row(provider="custom"), {}) == []                                                                    # needs base URL and model
    assert K.candidates_for(row(provider="custom", base_url="http://localhost:11434/v1", model="llama3"), {})[0].base_url == "http://localhost:11434/v1"
    public = K.public_row(row(), Health(), {})
    assert "api_key" not in public and public["key_hint"] == "…abcd" and "sk-secret" not in str(public)
    assert K.public_row(row(source="env", api_key=None), Health(), {"OPENAI_API_KEY": "sk-from-env-1234"})["key_hint"] == "…1234"
    assert K.public_row(row(api_key=""), Health(), {})["status"] == "missing"


def test_environment_keys_are_listed_once_without_copying_the_secret(tmp_path):
    from screening.store import Store
    s = Store(tmp_path / "k.db")
    env = {"ANTHROPIC_API_KEY": "sk-ant-1234567890", "GROQ_API_KEY": "gsk_1234567890", "LLM_CUSTOM_API_KEY": "ck-1234567890", "LLM_CUSTOM_BASE_URL": "https://api.mistral.ai/v1",
           "LLM_CUSTOM_MODEL": "mistral-small-latest"}
    K.sync_env(s, env)
    K.sync_env(s, env)                                                                          # idempotent
    rows = s.list_keys()
    assert [r["provider"] for r in rows] == ["anthropic", "groq", "custom"] and all(r["api_key"] is None and r["source"] == "env" for r in rows)
    cands = K.all_candidates(s, env)
    assert cands[0].provider == "anthropic" and cands[0].api_key == "sk-ant-1234567890" and any(c.base_url == "https://api.mistral.ai/v1" for c in cands)
    assert K.all_candidates(s, {}) == []                                                        # variable removed: nothing usable, nothing crashes


def test_key_form_validation():
    v = K.validate_new
    assert v("openai", "sk-" + "x" * 20, "", "") is None and v("nope", "k" * 20, "", "") == "Choose a provider."
    assert "too short" in v("openai", "abc", "", "") and "spaces" in v("openai", "sk-abc def ghijk", "", "")
    assert "needs a model" in v("openrouter", "k" * 20, "", "") and "base URL" in v("custom", "k" * 20, "m", "ftp://x")
    assert v("custom", "k" * 20, "m", "http://localhost:11434/v1") is None and "plain http" in v("custom", "k" * 20, "m", "http://example.com/v1")


def test_key_test_proves_a_key_and_explains_failures():
    def serve(r):
        if r.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-5-mini"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})
    c = PR.Candidate("openai", "OpenAI", "openai", "https://api.openai.com/v1", "sk-test-key", "gpt-4o-mini")
    ok = K.test_candidate(httpx.Client(transport=httpx.MockTransport(serve)), c)
    assert ok["ok"] and ok["model"] == "gpt-5-mini"                                              # the best model this key can use
    bad = K.test_candidate(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"error": {"message": "Incorrect API key"}}))), c)
    assert not bad["ok"] and bad["kind"] == "auth" and "rejected" in bad["error"]
    noquota = K.test_candidate(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429, json={"error": {"code": "insufficient_quota", "message": "check billing"}}))), c)
    assert noquota["kind"] == "quota" and "credit" in noquota["error"]


def test_pacer_and_parse_json():
    now, slept = [0.0], []
    p = Pacer(1000, clock=lambda: now[0], sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
    p.wait(800)
    p.record(800)
    p.wait(500)
    assert slept and now[0] >= 60
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1} and parse_json('ok {"a": 1} bye') == {"a": 1}


# ---------- full pipeline (fakes) ----------
def run(llm_router=None, crawl_ok=True, search_ok=True, resources=()):
    llm, _ = make_llm([llm_router or router()] * 12)
    steps = {}
    r = analyse(Inputs("deck.pdf", make_pdf(DECK_TEXT), "https://acme.test", list(resources), ""), llm, T, P,
                step=lambda k, s, d: steps.__setitem__(k, s), crawl_fn=fake_crawl(crawl_ok), search_fn=fake_search(search_ok))
    return r, steps, llm


def test_pipeline_end_to_end():
    r, steps, llm = run(llm_router=router(assess=assessment(8)), resources=[("notes.md", b"Extra notes: 3 enterprise pilots signed.")])
    st = {c["id"]: c["status"] for c in r["claims"]}
    assert st["C1"] == "verified" and st["C2"] == "contradicted" and st["C3"] == "consistent"   # LinkedIn = profile only
    assert st["C4"] == "not_checked" and st["C5"] == "consistent"
    assert next(c for c in r["claims"] if c["id"] == "C4")["quote_found"] is False             # invented quote caught
    assert r["context"] == {"stage": "seed", "stage_group": "early", "model_type": "b2b_saas", "model_label": "B2B software / SaaS"}
    assert r["decision"] == "MAYBE" and "C2" in r["decision_reason"]                            # contradiction holds ADVANCE
    assert r["dimensions"]["traction"]["penalty"] == 2 and r["dimensions"]["traction"]["final"] == 6
    assert r["dimensions"]["timing"]["claim_ids"] == ["C1"]                                    # invented C99 removed
    assert r["inputs"]["resources"] == ["notes.md"] and llm.calls == 4
    assert set(steps.values()) <= {"done", "warning"} and r["red_flag_count"] >= 1


def test_extra_files_reach_the_prompt_and_get_their_own_claim_share():
    seen = []
    base = router()

    def spy(kw):
        if "own materials" in kw["messages"][0]["content"]:
            seen.append((kw["messages"][0]["content"], kw["messages"][1]["content"]))
        return base(kw)
    run(llm_router=spy, resources=[("notes.txt", b"Acme internal: ARR EUR 9M in 2025. " * 4)])
    system, user = seen[0]
    assert "=== D1: extra file 'notes.txt' ===" in user and "ARR EUR 9M" in user
    assert "(max 19)" in system and "take claims from each of them" in system        # 15 for the deck + 4 per extra file


def test_pipeline_survives_dead_website_and_search():
    r, steps, llm = run(crawl_ok=False, search_ok=False)
    assert steps["website"] == "error" and steps["search"] == "warning"
    assert any(i["stage"] == "website" and i["severity"] == "error" for i in r["issues"])
    assert all(c["status"] in ("unverified", "not_checked") for c in r["claims"])             # no evidence -> nothing verified
    assert r["confidence"]["level"] == "Low" and llm.calls == 2                               # no site call, no verify call


def test_pipeline_survives_verification_failure():
    base = router()

    def flaky(kw):
        if "check a startup's claims" in kw["messages"][0]["content"]:
            return reply("this is not json")
        return base(kw)
    r, _, llm = run(llm_router=flaky)
    assert any(i["stage"] == "verify" and i["severity"] == "error" for i in r["issues"])
    assert llm.calls == 5  # 2 extractions + verify (call + repair, then gives up) + scoring
    assert not any(c["status"] == "verified" for c in r["claims"])


def test_a_crashing_crawler_or_search_costs_evidence_not_the_run():
    def boom(*a, **k):
        raise RuntimeError("internal bug")
    for crawl_fn, search_fn in ((boom, fake_search()), (fake_crawl(), boom)):
        llm, _ = make_llm([router()] * 12)
        r = analyse(Inputs("d.pdf", make_pdf(DECK_TEXT), "https://acme.test"), llm, T, P, crawl_fn=crawl_fn, search_fn=search_fn)
        assert r["decision"] in ("ADVANCE", "MAYBE", "PASS") and any(i["severity"] in ("error", "warning") for i in r["issues"])


def test_unreadable_deck_stops_the_run():
    llm, _ = make_llm([])
    with pytest.raises(intake.IntakeError):
        analyse(Inputs("d.pdf", make_pdf("hi"), "https://acme.test"), llm, T, P,
                crawl_fn=fake_crawl(), search_fn=fake_search())


# ---------- suggestions / thesis ----------
def test_suggestions_and_templates():
    assert sg.normalize_country("USA") == "United States" and sg.normalize_stage("Series D+") == "series-d"
    for slug in sg.STAGE_SLUGS:
        assert slug in extract_system(P["model_types"], 10)
    for tp in sg.TEMPLATES:
        Thesis(**tp["data"])
    with pytest.raises(ThesisError):
        parse_thesis("fund_name: [")


# ---------- mandate cross-check: the cases that used to wrongly reject good deals ----------
def fit(profile, t=T):
    base = {"sectors": ["ai"], "one_liner": "", "stage": "seed", "hq_country": "Poland", "founder_origin": "", "ask_eur": 1_500_000}
    return fit_check({**base, **profile}, t, P)


@pytest.mark.parametrize("profile", [
    {"hq_country": "Warsaw, Poland"}, {"hq_country": "Poland (Warsaw)"}, {"stage": "Seed Round"}, {"stage": "pre seed"},
    {"stage": "seed / series a"}, {"sectors": ["artificial intelligence"]}, {"sectors": ["AI/ML"]}, {"sectors": ["developer-tools"]}])
def test_realistic_ai_phrasings_still_fit(profile):
    assert fit(profile)["status"] == "FIT", fit(profile)["checks"]


@pytest.mark.parametrize("profile,name", [({"hq_country": "Silicon Valley"}, "Geography"), ({"hq_country": ""}, "Geography"),
                                          ({"stage": "bootstrapped"}, "Stage"), ({"stage": ""}, "Stage")])
def test_unreadable_values_are_unknown_never_a_rejection(profile, name):
    f = fit(profile)
    assert f["status"] == "CHECK" and next(c for c in f["checks"] if c["name"] == name)["state"] == "unknown"


def test_clear_misses_are_rejections_and_say_why():
    assert fit({"hq_country": "Berlin, Germany"})["status"] == "NO_FIT"
    assert fit({"stage": "Series C"})["status"] == "NO_FIT"
    f = fit({"hq_country": "Atlanta, Georgia, United States"}, T.model_copy(update={"geography": ["Georgia"]}))
    assert next(c for c in f["checks"] if c["name"] == "Geography")["state"] == "warn"  # "Georgia" is a US state here, not certain
    geo = next(c for c in fit({"hq_country": "Berlin, Germany"})["checks"] if c["name"] == "Geography")
    assert "Germany" in geo["detail"] and geo["rule"].startswith("Poland") and geo["value"] == "Berlin, Germany"


def test_excluded_sectors_and_empty_rules():
    gambling = T.model_copy(update={"sectors_excluded": ["gambling"]})
    assert fit({"sectors": ["online gambling"]}, gambling)["status"] == "NO_FIT"
    assert fit({"sectors": ["crypto"]})["checks"][2]["state"] == "warn"                  # risky, not rejected
    open_fund = T.model_copy(update={"geography": [], "sectors_strong": [], "ticket_min_eur": 0, "ticket_max_eur": 0})
    assert fit({"hq_country": "Japan", "sectors": ["retail"], "ask_eur": None}, open_fund)["status"] == "FIT"  # empty = no restriction


def test_round_size_rules():
    states = lambda ask: next(c["state"] for c in fit({"ask_eur": ask})["checks"] if c["name"] == "Round size")  # noqa: E731
    assert states(1_500_000) == "ok" and states(100_000) == "warn" and states(50_000_000) == "warn" and states(None) == "unknown"


def test_a_fit_check_records_where_each_value_came_from():
    f = fit_check({"hq_country": "Poland", "stage": "seed", "sectors": ["ai"], "ask_eur": 1_000_000}, T, P, {"hq_country": "website", "stage": "documents"})
    assert next(c for c in f["checks"] if c["name"] == "Geography")["source"] == "website"


def test_thesis_self_consistency():
    ok = build_thesis(sg.TEMPLATES[0]["data"])
    assert check_thesis(ok) == ([], [])
    bad = build_thesis({**sg.TEMPLATES[0]["data"], "stages_core": ["seed"], "stages_check": ["seed"], "sectors_strong": ["ai"],
                        "sectors_weak": ["AI"], "ticket_min_eur": 9_000_000, "ticket_max_eur": 1_000_000, "advance_at": 4, "maybe_at": 6})
    errors, _ = check_thesis(bad)
    assert len(errors) == 4 and any("both core" in e for e in errors) and any("'ai'" in e for e in errors)
    no_stage, warnings = check_thesis(build_thesis({**sg.BLANK_THESIS, "fund_name": "X"}))
    assert any("core stage" in e for e in no_stage) and len(warnings) >= 3
    assert any("Atlantis" in w for w in check_thesis(build_thesis({**sg.TEMPLATES[0]["data"], "geography": ["Atlantis"]}))[1])
    with pytest.raises(ThesisError, match="name"):
        build_thesis({**sg.TEMPLATES[0]["data"], "fund_name": "  "})
    with pytest.raises(ThesisError, match="weights"):
        build_thesis({**sg.TEMPLATES[0]["data"], "weights": {"timing": 1}})


# ---------- PDF memo ----------
def _pdf_text(r):
    import io
    from pypdf import PdfReader
    from screening.pdf import build_pdf
    data = build_pdf(r, "2026-11-25T10:00:00+00:00")
    assert data.startswith(b"%PDF")
    reader = PdfReader(io.BytesIO(data))
    return data, reader, "\n".join(p.extract_text() for p in reader.pages)


def test_pdf_memo_contains_every_section_and_handles_hostile_text():
    r, _, _ = run(llm_router=router(assess=assessment(8)), resources=[("notes.md", b"Extra: 3 pilots signed.")])
    r["company"] = "Żabka <b>&</b> Spółka"                                # non-ASCII, and markup characters that must not be parsed
    r["claims"][0]["text"] = "Revenue is <font color=red>EUR 5M</font> & growing \u2192 fast \u2713"
    r["search"]["hits"][0]["url"] = "javascript:alert(1)"                  # must never become a link
    r["claims"][1]["note"] = "x" * 400                                     # a long unbroken string must wrap, not overflow
    data, reader, text = _pdf_text(r)
    for needle in ("INVESTMENT SCREENING MEMORANDUM", "Żabka", "RECOMMENDATION", "Executive summary", "Red flags and discrepancies",
                   "Claim verification", "Scorecard", "Fund thesis and mandate fit", "Company profile", "Sources", "Data quality",
                   "Questions for the founders", "Method and disclaimer", "CEE Seed Fund", "Contradicted", "Verified"):
        assert needle in text, needle
    assert "<font color=red>EUR 5M</font>" in text.replace("\n", " ") and "Page 1 of " in text and "javascript:" not in {a for pg in reader.pages for a in
        [str(x.get_object().get("/A", {}).get("/URI", "")) for x in (pg.get("/Annots") or [])]}
    assert len(reader.pages) >= 3


def test_pdf_memo_for_old_and_minimal_results():
    _, reader, text = _pdf_text({"company": "Pitchly", "decision": "PASS", "overall": 4.39})   # version 1 result
    assert len(reader.pages) == 1 and "earlier version" in text.lower() and "Pitchly" in text
    r, _, _ = run(crawl_ok=False, search_ok=False)                                               # no website, no search, thin evidence
    r["questions_for_founder"], r["missing_critical_data"], r["issues"] = [], [], []
    _, _, text = _pdf_text(r)
    assert "No technical or workflow issues" in text and "Search was unavailable" in text
    for k in r["fit"]["checks"]:                                                                  # saved before values/rules existed
        k.pop("rule", None)
    assert "DETAIL" in _pdf_text(r)[2]
