"""API tests: real FastAPI app with a fake AI server / crawl / search and a temporary database."""
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import app as server
from screening import suggestions as sg
from screening.llm import LLM
from screening.store import Store
from tests.fakes import DECK_TEXT, assessment, candidate, fake_crawl, fake_http, fake_search, make_pdf, router

H = {"X-Requested-With": "screening"}
PDF = make_pdf(DECK_TEXT)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY", "LLM_CUSTOM_API_KEY"):
        monkeypatch.delenv(name, raising=False)                           # the machine's own keys must never leak into tests
    monkeypatch.setattr(server, "store", Store(tmp_path / "t.db"))
    server.store.add_key("openai", "OpenAI", "sk-test-1234567890", model="gpt-4o-mini")      # one AI key so screening is allowed
    monkeypatch.setattr(server, "make_llm", lambda on_status: LLM([candidate()], on_status=on_status, sleep=lambda s: None,
                                                                  http=fake_http([router(assess=assessment(8))] * 12)))
    monkeypatch.setattr(server, "RUN_KW", {"crawl_fn": fake_crawl(), "search_fn": fake_search()})
    server.JOBS.clear()
    c = TestClient(server.app)
    c.tid = server.store.save_thesis(server.build_thesis(sg.TEMPLATES[0]["data"]).model_dump())   # one fund thesis to screen against
    return c


def screen(c, files=None, data=None):
    files = files if files is not None else [("deck", ("deck.pdf", PDF, "application/pdf"))]
    data = data if data is not None else {"website": "acme.test", "thesis_id": str(c.tid)}
    return c.post("/api/screen", data=data, files=files, headers=H)


def wait_job(c, job_id):
    for _ in range(200):
        j = c.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "error"):
            return j
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_page_and_health(client):
    assert "Deal Screening" in client.get("/").text
    h = client.get("/api/health").json()
    assert h["key"] is True and h["ai"] == "OpenAI" and h["profiles_error"] is None and h["theses"] == 1


def test_full_screening_history_memo_rescore_delete(client):
    files = [("deck", ("deck.pdf", PDF, "application/pdf")), ("resources", ("notes.md", b"3 pilots signed", "text/markdown"))]
    job = wait_job(client, screen(client, files).json()["job_id"])
    assert job["status"] == "done", job
    assert all(s["state"] in ("done", "warning") for s in job["steps"].values())
    rid = job["run_id"]
    row = client.get("/api/runs").json()[0]
    assert row["company"] == "Acme Robotics" and row["version"] == 2 and row["claims"] == 4 and row["red_flags"] >= 1
    run = client.get(f"/api/runs/{rid}").json()
    assert run["inputs"]["resources"] == ["notes.md"] and run["decision"] == "MAYBE"
    memo = client.get(f"/api/runs/{rid}/memo")
    assert "Claim verification" in memo.text and "Contradicted" in memo.text

    assert run["thresholds"] == {"advance_at": 7.0, "maybe_at": 5.5} and run["thesis"]["id"] == client.tid
    base = {k: 1.0 for k in run["raw_scores"]}
    lenient = client.post(f"/api/runs/{rid}/rescore", json={"weights": base, "advance_at": 9.5, "maybe_at": 2.0}, headers=H).json()
    assert lenient["decision"] == "MAYBE" and "weights" in lenient
    assert client.post(f"/api/runs/{rid}/rescore", json={"weights": base, "advance_at": 5, "maybe_at": 6}, headers=H).status_code == 400
    assert client.delete(f"/api/runs/{rid}", headers=H).status_code == 200
    assert client.get(f"/api/runs/{rid}").status_code == 404


def test_memo_downloads_as_a_pdf(client):
    job = wait_job(client, screen(client).json()["job_id"])
    resp = client.get(f"/api/runs/{job['run_id']}/memo.pdf")
    assert resp.status_code == 200 and resp.headers["content-type"] == "application/pdf" and resp.content.startswith(b"%PDF")
    assert 'filename="Acme_Robotics_screening_memo.pdf"' in resp.headers["content-disposition"]
    assert client.get("/api/runs/999/memo.pdf").status_code == 404
    run = server.store.get(job["run_id"]); run["company"] = "Żabka Spółka"
    assert client.get(f"/api/runs/{server.store.save(run)}/memo.pdf").status_code == 200       # non-ASCII name, ASCII file name
    assert client.get(f"/api/runs/{job['run_id']}/memo").text.startswith("# Acme Robotics")       # Markdown still there for "Copy memo"


def test_deck_and_website_are_both_required(client):
    assert "website" in screen(client, data={"website": "", "thesis_id": str(client.tid)}).json()["detail"]
    assert "pitch deck" in screen(client, files=[]).json()["detail"].lower()
    bad_deck = [("deck", ("deck.docx", b"PK\x03\x04", "application/octet-stream"))]
    assert "PDF" in screen(client, files=bad_deck).json()["detail"]
    bad_extra = [("deck", ("deck.pdf", PDF, "application/pdf")), ("resources", ("x.exe", b"MZ", "application/octet-stream"))]
    assert "unsupported" in screen(client, files=bad_extra).json()["detail"]
    too_many = [("deck", ("deck.pdf", PDF, "application/pdf"))] + [("resources", (f"{i}.txt", b"x", "text/plain")) for i in range(6)]
    assert "at most" in screen(client, files=too_many).json()["detail"]


def test_a_fund_thesis_is_required_and_recorded_with_the_report(client, tmp_path, monkeypatch):
    real = server.store
    fresh = Store(tmp_path / "empty.db")                                         # a fresh install: an AI key but no theses at all
    fresh.add_key("openai", "OpenAI", "sk-test-1234567890", model="gpt-4o-mini")
    monkeypatch.setattr(server, "store", fresh)
    assert "Create a fund thesis first" in screen(client, data={"website": "acme.test", "thesis_id": "1"}).json()["detail"]
    monkeypatch.setattr(server, "store", real)
    assert "Choose a fund thesis" in screen(client, data={"website": "acme.test"}).json()["detail"]
    assert "no longer exists" in screen(client, data={"website": "acme.test", "thesis_id": "999"}).json()["detail"]
    assert "no longer exists" in screen(client, data={"website": "acme.test", "thesis_id": "abc"}).json()["detail"]
    broken = client.post("/api/theses", json={"data": {**sg.TEMPLATES[1]["data"], "fund_name": "Broken"}}, headers=H).json()["id"]
    server.store.save_thesis({**sg.TEMPLATES[1]["data"], "fund_name": "Broken", "stages_core": []}, broken)  # corrupted by hand
    assert "needs fixing" in screen(client, data={"website": "acme.test", "thesis_id": str(broken)}).json()["detail"]
    other = client.post("/api/theses", json={"data": sg.TEMPLATES[1]["data"]}, headers=H).json()["id"]
    job = wait_job(client, screen(client, data={"website": "acme.test", "thesis_id": str(other)}).json()["job_id"])
    run = client.get(f"/api/runs/{job['run_id']}").json()
    assert run["thesis"]["id"] == other and run["fund"] == "US Seed Fund" and run["thesis_exists"] is True
    assert next(c for c in run["fit"]["checks"] if c["name"] == "Geography")["state"] == "fail"       # Poland vs a US fund
    assert run["decision"] == "PASS" and "mandate" in run["decision_reason"]
    assert client.get("/api/runs").json()[0]["thesis_name"] == "US Seed Fund"
    client.delete(f"/api/theses/{other}", headers=H)
    after = client.get(f"/api/runs/{job['run_id']}").json()
    assert after["thesis_exists"] is False and after["thesis"]["data"]["fund_name"] == "US Seed Fund"  # report keeps its own copy


def test_thesis_crud_validation_and_duplicates(client):
    assert client.get("/api/thesis-options").json()["suggestions"]["blank"]["fund_name"] == ""
    listed = client.get("/api/theses").json()
    assert [t["name"] for t in listed] == ["CEE Seed Fund"] and listed[0]["summary"]["countries"] == 19 and listed[0]["errors"] == []
    new = client.post("/api/theses", json={"data": sg.TEMPLATES[2]["data"]}, headers=H)
    assert new.status_code == 200 and new.json()["name"] == "European Series A Fund"
    dup = client.post("/api/theses", json={"data": {**sg.TEMPLATES[2]["data"], "fund_name": "european series a FUND"}}, headers=H)
    assert dup.status_code == 400 and "already exists" in dup.json()["detail"]                      # names are unique, any case
    bad = client.post("/api/theses", json={"data": {**sg.TEMPLATES[2]["data"], "fund_name": "Bad", "stages_core": [], "advance_at": 3}}, headers=H)
    assert bad.status_code == 400 and "core stage" in bad.json()["detail"]
    assert client.post("/api/theses", json={"data": {**sg.BLANK_THESIS}}, headers=H).status_code == 400  # no name
    assert client.post("/api/theses", json={"text": "fund_name: ["}, headers=H).status_code == 400
    warn = client.post("/api/theses", json={"data": {**sg.TEMPLATES[0]["data"], "fund_name": "Open", "geography": [], "sectors_strong": []}}, headers=H).json()
    assert len(warn["warnings"]) >= 2
    tid = new.json()["id"]
    yaml_text = client.get(f"/api/theses/{tid}").json()["text"]
    assert client.put(f"/api/theses/{tid}", json={"text": yaml_text.replace("European Series A Fund", "Renamed Fund")}, headers=H).json()["name"] == "Renamed Fund"
    assert client.put(f"/api/theses/{tid}", json={"data": {**sg.TEMPLATES[2]["data"], "fund_name": "CEE Seed Fund"}}, headers=H).status_code == 400
    assert client.put("/api/theses/999", json={"data": sg.TEMPLATES[2]["data"]}, headers=H).status_code == 404
    d1 = client.post(f"/api/theses/{tid}/duplicate", headers=H).json()
    d2 = client.post(f"/api/theses/{tid}/duplicate", headers=H).json()
    assert d1["name"] == "Renamed Fund (copy)" and d2["name"] == "Renamed Fund (copy 2)"
    assert client.delete(f"/api/theses/{tid}", headers=H).status_code == 200 and client.get(f"/api/theses/{tid}").status_code == 404
    assert client.delete(f"/api/theses/{tid}", headers=H).status_code == 404


def test_the_old_thesis_file_is_imported_once(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "store", Store(tmp_path / "m.db"))
    f = tmp_path / "thesis.yaml"
    import yaml
    f.write_text(yaml.safe_dump(server.build_thesis(sg.TEMPLATES[0]["data"]).model_dump()))
    server.import_legacy_thesis(f)
    assert [t["name"] for t in server.store.list_theses()] == ["CEE Seed Fund"] and not f.exists()
    f.write_text("fund_name: [")
    server.import_legacy_thesis(f)                                        # already has theses: untouched
    assert f.exists() and len(server.store.list_theses()) == 1


def test_unreachable_website_still_produces_a_report(client, monkeypatch):
    monkeypatch.setattr(server, "RUN_KW", {"crawl_fn": fake_crawl(False), "search_fn": fake_search(False)})
    job = wait_job(client, screen(client).json()["job_id"])
    assert job["status"] == "done" and job["steps"]["website"]["state"] == "error"
    run = client.get(f"/api/runs/{job['run_id']}").json()
    assert run["confidence"]["level"] == "Low" and any(i["severity"] == "error" for i in run["issues"])


def test_scanned_deck_reports_a_readable_error(client):
    job = wait_job(client, screen(client, [("deck", ("d.pdf", make_pdf("hi"), "application/pdf"))]).json()["job_id"])
    assert job["status"] == "error" and "scanned" in job["error"]


def test_an_ai_key_is_required_before_queueing(client):
    for k in server.store.list_keys():
        server.store.delete_key(k["id"])
    assert "AI key" in screen(client).json()["detail"] and client.get("/api/health").json()["key"] is False


def test_non_ascii_company_memo_downloads(client):
    job = wait_job(client, screen(client).json()["job_id"])
    r = server.store.get(job["run_id"])
    r["company"] = "Żabka Spółka"
    rid = server.store.save(r)
    resp = client.get(f"/api/runs/{rid}/memo")
    assert resp.status_code == 200 and "Żabka" in resp.text                         # audit: used to be a 500


def test_queued_jobs_are_never_pruned(client):
    for i in range(25):
        server.JOBS[f"old{i}"] = {"status": "done", "created": i}
    server.JOBS["waiting"] = {"status": "queued", "created": -1}
    wait_job(client, screen(client).json()["job_id"])
    assert "waiting" in server.JOBS and sum(k.startswith("old") for k in server.JOBS) <= 20


def test_writes_need_header_and_localhost(client):
    assert client.post("/api/screen", data={"website": "x.com"}).status_code == 403
    assert client.get("/api/health", headers={"host": "evil.example.com"}).status_code == 403


# ---------- AI keys ----------
def serve_models(request):
    """A fake OpenAI: lists models, answers chat; the key 'sk-bad...' is rejected."""
    if "sk-bad" in request.headers.get("authorization", ""):
        return httpx.Response(401, json={"error": {"message": "Incorrect API key provided"}})
    if request.method == "GET":
        return httpx.Response(200, json={"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-5-mini"}]})
    return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})


@pytest.fixture()
def keys_client(client, monkeypatch):
    monkeypatch.setattr(server, "HTTP", httpx.Client(transport=httpx.MockTransport(serve_models)))
    for k in server.store.list_keys():
        server.store.delete_key(k["id"])
    return client


def test_adding_a_key_tests_it_first_and_never_returns_it(keys_client):
    c = keys_client
    r = c.post("/api/llm-keys", json={"provider": "openai", "api_key": "sk-good-abcdef123456"}, headers=H)
    assert r.status_code == 200 and "sk-good" not in r.text
    row = r.json()["key"]
    assert row["model"] == "gpt-5-mini" and row["key_hint"] == "…3456" and row["status"] == "ready" and row["source"] == "app"   # best model found automatically
    assert r.json()["ready"] is True and c.get("/api/health").json()["key"] is True
    assert "sk-good" not in c.get("/api/llm-keys").text
    bad = c.post("/api/llm-keys", json={"provider": "openai", "api_key": "sk-bad-abcdef123456"}, headers=H)
    assert bad.status_code == 400 and "rejected" in bad.json()["detail"] and bad.json()["can_save_anyway"] is False
    assert len(c.get("/api/llm-keys").json()["keys"]) == 1                                       # the bad key was not saved


def test_key_form_errors_are_readable(keys_client):
    c = keys_client
    post = lambda **b: c.post("/api/llm-keys", json=b, headers=H)  # noqa: E731
    assert "Choose a provider" in post(provider="x", api_key="k" * 20).json()["detail"]
    assert "too short" in post(provider="openai", api_key="abc").json()["detail"]
    assert "needs a model" in post(provider="openrouter", api_key="sk-or-" + "x" * 20).json()["detail"]
    assert "base URL" in post(provider="custom", api_key="k" * 20, model="m", base_url="nope").json()["detail"]
    assert c.get("/api/llm-keys").json()["providers"][0]["id"] == "anthropic" and len(c.get("/api/llm-keys").json()["providers"]) == 6


def test_several_keys_can_be_ordered_switched_off_and_removed(keys_client):
    c = keys_client
    ids = [c.post("/api/llm-keys", json={"provider": "openai", "api_key": f"sk-key-{i}-abcdef123456", "label": f"Key {i}", "skip_test": True}, headers=H).json()["key"]["id"]
           for i in (1, 2, 3)]
    order = lambda: [k["label"] for k in c.get("/api/llm-keys").json()["keys"]]  # noqa: E731
    assert order() == ["Key 1", "Key 2", "Key 3"]
    c.put(f"/api/llm-keys/{ids[2]}", json={"move": "up"}, headers=H)
    assert order() == ["Key 1", "Key 3", "Key 2"]
    c.put(f"/api/llm-keys/{ids[0]}", json={"move": "down"}, headers=H)
    assert order() == ["Key 3", "Key 1", "Key 2"]
    off = c.put(f"/api/llm-keys/{ids[0]}", json={"enabled": False}, headers=H).json()
    assert next(k for k in off["keys"] if k["id"] == ids[0])["status"] == "disabled"
    assert [x.key_id for x in server.all_candidates(server.store)] == [ids[2], ids[1]]              # the disabled key is not used, order kept
    assert c.delete(f"/api/llm-keys/{ids[1]}", headers=H).status_code == 200 and len(c.get("/api/llm-keys").json()["keys"]) == 2
    assert c.put("/api/llm-keys/999", json={"enabled": True}, headers=H).status_code == 404 and c.delete("/api/llm-keys/999", headers=H).status_code == 404
    assert c.put(f"/api/llm-keys/{ids[0]}", json={"move": "sideways"}, headers=H).status_code == 400


def test_environment_keys_appear_and_cannot_be_deleted(keys_client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-1234567890")
    keys = keys_client.get("/api/llm-keys").json()["keys"]
    env = next(k for k in keys if k["source"] == "env")
    assert env["provider"] == "anthropic" and env["key_hint"] == "…7890" and env["env_names"] == ["ANTHROPIC_API_KEY"] and "sk-ant-env" not in str(keys)
    gone = keys_client.delete(f"/api/llm-keys/{env['id']}", headers=H)
    assert gone.status_code == 400 and "ANTHROPIC_API_KEY" in gone.json()["detail"]
    assert keys_client.put(f"/api/llm-keys/{env['id']}", json={"enabled": False}, headers=H).status_code == 200


def test_testing_a_saved_key_clears_its_cooldown(keys_client):
    c = keys_client
    kid = c.post("/api/llm-keys", json={"provider": "openai", "api_key": "sk-good-abcdef123456"}, headers=H).json()["key"]["id"]
    for cand in server.candidates_for(server.store.get_key(kid)):
        server.HEALTH.mark(cand, "rate", 300)
    assert c.get("/api/llm-keys").json()["keys"][0]["status"] == "cooling"
    result = c.post(f"/api/llm-keys/{kid}/test", headers=H).json()
    assert result["ok"] is True and c.get("/api/llm-keys").json()["keys"][0]["status"] == "ready"


def test_key_endpoints_need_the_header_like_every_write(keys_client):
    assert keys_client.post("/api/llm-keys", json={"provider": "openai", "api_key": "k" * 20}).status_code == 403


# ---------- hosted mode: APP_PASSWORD ----------
def test_password_protects_everything_when_set(client, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "correct-horse-battery")
    hosted = {"host": "screening.example.com"}                                  # not localhost: allowed only because a password is set
    assert client.get("/api/runs", headers=hosted).status_code == 401
    assert client.get("/", headers=hosted, follow_redirects=False).headers["location"] == "/login"
    assert client.get("/login", headers=hosted).status_code == 200 and "Password" in client.get("/login", headers=hosted).text
    assert client.post("/api/login", json={"password": "nope"}, headers={**H, **hosted}).status_code == 401
    ok = client.post("/api/login", json={"password": "correct-horse-battery"}, headers={**H, **hosted})
    assert ok.status_code == 200 and "httponly" in ok.headers["set-cookie"].lower()
    assert client.get("/api/runs", headers=hosted).status_code == 200                # the cookie is now sent by the client
    client.cookies.set("session", "9999999999.forged")
    assert client.get("/api/runs", headers=hosted).status_code == 401                # a forged cookie is refused


def test_login_is_rate_limited_and_local_mode_has_no_login(client, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "correct-horse-battery")
    server.FAILS.clear()
    codes = [client.post("/api/login", json={"password": "x"}, headers=H).status_code for _ in range(7)]
    assert codes[:5] == [401] * 5 and codes[5:] == [429, 429]
    assert client.post("/api/login", json={"password": "correct-horse-battery"}, headers=H).status_code == 429   # even the right one waits
    server.FAILS.clear()
    monkeypatch.delenv("APP_PASSWORD")
    assert client.get("/api/runs").status_code == 200 and client.get("/api/runs", headers={"host": "evil.example"}).status_code == 403
