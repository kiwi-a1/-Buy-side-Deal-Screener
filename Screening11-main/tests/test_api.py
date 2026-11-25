"""API tests: real FastAPI app with fake Groq / crawl / search, temp database and thesis file."""
import time

import pytest
from fastapi.testclient import TestClient

import app as server
from screening import suggestions as sg
from screening.llm import LLM
from screening.store import Store
from tests.fakes import DECK_TEXT, FakeClient, assessment, fake_crawl, fake_search, make_pdf, router

H = {"X-Requested-With": "screening"}
PDF = make_pdf(DECK_TEXT)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(server, "store", Store(tmp_path / "t.db"))
    monkeypatch.setattr(server, "make_llm", lambda on_status: LLM(client=FakeClient([router(assess=assessment(8))] * 12),
                                                                  on_status=on_status, sleep=lambda s: None))
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
    assert h["key"] is True and h["profiles_error"] is None and h["theses"] == 1


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
    monkeypatch.setattr(server, "store", Store(tmp_path / "empty.db"))          # a fresh install: no theses at all
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


def test_missing_key_rejected_before_queueing(client, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY")
    assert "GROQ_API_KEY" in screen(client).json()["detail"]


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
