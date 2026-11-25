"""Shared test doubles: a fake AI server (httpx mock transport), a fake crawl and a fake web search. No network, no cost."""
import json

import httpx

from screening.crawler import CrawlResult, Page
from screening.llm import LLM
from screening.providers import Candidate
from screening.scoring import DIMENSIONS

DECK_TEXT = ("Acme Robotics builds AI inspection robots for factories. Founded in 2021 in Warsaw, Poland. "
             "We have 40 paying customers and EUR 1.2M ARR growing 15% month over month. "
             "Founders: Anna Nowak (CEO, ex-ABB) and Piotr Zielinski (CTO, PhD robotics). Raising EUR 3M seed.")


class Reply:
    def __init__(self, content, tokens=100):
        self.content, self.tokens = content, tokens


def reply(content, tokens=100):
    return Reply(content, tokens)


class HttpError:
    """Scripted HTTP error response (status, headers, message)."""
    def __init__(self, status, headers=None, message="error"):
        self.status_code, self.headers, self.message = status, headers or {}, message


def candidate(i=1, tpm=None, kind="openai", model=None, base="https://ai.test/v1"):
    return Candidate(provider="custom", label=f"AI-{i}", kind=kind, base_url=base, api_key=f"key-{i}-abcdefgh", model=model or f"model-{i}", key_id=i, tpm=tpm)


def fake_http(script):
    """An httpx client whose 'server' answers from `script`: each request pops the next item - a Reply (OpenAI format), an
    HttpError, an exception to raise (e.g. httpx.ReadTimeout), or a function(kw) returning one of those."""
    calls = []

    def handler(request):
        body = json.loads(request.content) if request.content else {}
        kw = {"messages": body.get("messages", []), "model": body.get("model"), "url": str(request.url), "body": body,
              "headers": dict(request.headers), "timeout": request.extensions.get("timeout")}
        calls.append(kw)
        item = script.pop(0)
        if callable(item):
            item = item(kw)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, HttpError):
            return httpx.Response(item.status_code, headers=item.headers, json={"error": {"message": item.message}})
        return httpx.Response(200, json={"choices": [{"message": {"content": item.content}}], "usage": {"total_tokens": item.tokens}})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    client.calls = calls
    return client


def make_llm(script, n=1, tpm=None, **kw):
    """n providers (AI-1 first) all served by one scripted fake server, with a fake clock that sleeping advances. Returns (llm, sleeps)."""
    now, sleeps = [1000.0], []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    http = fake_http(script)
    llm = LLM([candidate(i + 1, tpm=tpm) for i in range(n)], http=http, sleep=sleep, clock=lambda: now[0], **kw)
    llm.http_calls = http.calls
    return llm, sleeps


DECK_EX = {"profile": {"company_name": "Acme Robotics", "one_liner": "AI inspection robots for factories",
                       "founders": [{"name": "Anna Nowak", "role": "CEO", "background": "ex-ABB"},
                                    {"name": "Piotr Zielinski", "role": "CTO", "background": "PhD robotics"}],
                       "hq_country": "Poland", "founded_year": "2021", "stage": "seed", "business_model_type": "b2b_saas",
                       "sectors": ["ai", "robotics"], "traction": ["EUR 1.2M ARR"], "ask_eur": 3000000, "ask_text": "EUR 3M seed"},
           "claims": [
               {"text": "Acme has 40 paying customers.", "category": "customers", "importance": "high",
                "quote": "We have 40 paying customers", "doc": "D0"},
               {"text": "Acme has EUR 1.2M ARR.", "category": "revenue", "importance": "high",
                "quote": "EUR 1.2M ARR growing 15% month over month", "doc": "D0"},
               {"text": "The CEO previously worked at ABB.", "category": "team", "importance": "high",
                "quote": "Anna Nowak (CEO, ex-ABB)", "doc": "D0"},
               {"text": "Acme won a robotics award.", "category": "other", "importance": "low", "quote": "invented quote", "doc": "D0"}]}
SITE_EX = {"profile": {"hq_country": "Poland", "founded_year": "2021"},
           "claims": [{"text": "Acme serves 40 customers.", "category": "customers", "importance": "medium",
                       "quote": "Trusted by 40 manufacturers", "doc": "P1"}]}
VERIFY = {"checks": [{"claim_id": "C1", "status": "supported", "evidence_ids": ["S1", "C5"], "note": "News article confirms 40 customers."},
                     {"claim_id": "C2", "status": "contradicted", "evidence_ids": ["S1"], "note": "Article says EUR 0.6M ARR."},
                     {"claim_id": "C3", "status": "supported", "evidence_ids": ["S2"], "note": "LinkedIn lists ABB."},
                     {"claim_id": "C5", "status": "supported", "evidence_ids": ["C1"], "note": "Deck says 40 customers."}]}


def assessment(score=7):
    return {"dimensions": {k: {"score": score, "reasoning": "ok", "claim_ids": ["C1", "C99"], "evidence": ["x"], "missing": []}
                           for k in DIMENSIONS},
            "summary": "Solid seed company.", "strengths": ["a", "b", "c", "d"], "concerns": ["x"], "next_steps": ["call"],
            "questions_for_founder": ["What is churn?"], "missing_critical_data": ["churn"]}


def router(deck=DECK_EX, site=SITE_EX, verify=VERIFY, assess=None):
    """Answer each AI call by recognising its system prompt."""
    def answer(kw):
        sys = kw["messages"][0]["content"]
        if "own materials" in sys:
            return reply(json.dumps(deck))
        if "pages of a startup's own website" in sys:
            return reply(json.dumps(site))
        if "check a startup's claims" in sys:
            return reply(json.dumps(verify))
        return reply(json.dumps(assess or assessment()))
    return answer


def fake_crawl(reachable=True):
    def run(url, on_progress=lambda m: None):
        r = CrawlResult(start_url=url, final_url=url, reachable=reachable, stopped_reason="all reachable pages read")
        if reachable:
            r.pages = [Page(url=url, title="Acme Robotics", text="Acme Robotics. Trusted by 40 manufacturers. Team: Anna Nowak, CEO.",
                            priority=0, depth=0)]
        else:
            r.errors = [{"url": url, "error": "timed out"}]
        return r
    return run


def fake_search(available=True):
    def run(name, host, founders, has_traction, on_progress=lambda m: None):
        if not available:
            return {"provider": "fake", "queries": ["q"], "failed": [{"query": "q", "error": "down"}], "irrelevant": 0,
                    "available": False, "hits": []}
        return {"provider": "fake", "queries": ["q1", "q2"], "failed": [], "irrelevant": 1, "available": True,
                "hits": [{"id": "S1", "title": "Acme Robotics raises seed", "url": "https://news.example/acme",
                          "snippet": "Acme Robotics, with 40 customers and EUR 0.6M ARR...", "query": "q1", "kind": "independent"},
                         {"id": "S2", "title": "Anna Nowak - Acme Robotics | LinkedIn", "url": "https://linkedin.com/in/anna",
                          "snippet": "CEO at Acme Robotics. Previously ABB.", "query": "q2", "kind": "profile"}]}
    return run


def make_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
