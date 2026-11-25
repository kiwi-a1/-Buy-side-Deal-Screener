"""Local web app.  Run:  python app.py   (then open http://localhost:8501)

FastAPI serves a plain HTML/CSS/JS page from static/ and a small JSON API. Screenings run one at a time in a
background thread (the Groq free tier is ~8k tokens/minute, so running two at once would only make both slower).
"""
from __future__ import annotations

import logging
import os
import re
import threading
from types import SimpleNamespace
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv(Path(__file__).resolve().parent / ".env")

from screening.intake import MAX_FILE_BYTES, MAX_RESOURCES, IntakeError, check_upload, normalize_url  # noqa: E402
from screening.llm import LLM, LLMError  # noqa: E402
from screening.pdf import build_pdf  # noqa: E402
from screening.pipeline import STEPS, Inputs, analyse  # noqa: E402
from screening.report import to_markdown  # noqa: E402
from screening.scoring import DIMENSIONS, ProfilesError, evaluate, load_profiles  # noqa: E402
from screening.store import Store  # noqa: E402
from screening.suggestions import all_suggestions  # noqa: E402
from screening.thesis import LEGACY_PATH, Thesis, ThesisError, build_thesis, check_thesis, parse_thesis, thesis_summary  # noqa: E402

log = logging.getLogger("screening")
STATIC = Path(__file__).resolve().parent / "static"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "testserver"}

app = FastAPI(title="Deal Screening", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
store = Store()


def import_legacy_thesis(path: Path = LEGACY_PATH) -> None:
    """Older versions kept one thesis in thesis.yaml. Import it once as the first fund thesis, then set the file aside."""
    if path.exists() and not store.list_theses():
        try:
            store.save_thesis(parse_thesis(path.read_text(encoding="utf-8")).model_dump())
            path.rename(path.with_suffix(".yaml.imported"))
            log.info("Imported %s as a fund thesis", path.name)
        except (ThesisError, ValueError, OSError):
            log.warning("Could not import %s; create a fund thesis in the app instead.", path)


import_legacy_thesis()
POOL = ThreadPoolExecutor(max_workers=1)
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def make_llm(on_status) -> LLM:  # replaced in tests
    return LLM(on_status=on_status)


RUN_KW: dict = {}  # extra keyword arguments for analyse(); tests inject fake crawl/search here


@app.middleware("http")
async def local_only(request: Request, call_next):
    # Only answer requests addressed to this machine (blocks DNS-rebinding), and require a custom header on
    # anything that changes state, so other websites open in your browser cannot trigger screenings or edits.
    host = request.headers.get("host") or ""
    host = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]  # strip the port
    if host not in LOCAL_HOSTS:
        return JSONResponse({"detail": "This app only serves localhost."}, status_code=403)
    if request.method in ("POST", "PUT", "DELETE") and request.headers.get("x-requested-with") != "screening":
        return JSONResponse({"detail": "Missing X-Requested-With header."}, status_code=403)
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"  # always revalidate, so an updated app is never shown with stale CSS/JS
    return response


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    try:
        load_profiles()
        profiles_error = None
    except ProfilesError as e:
        profiles_error = str(e)
    return {"key": bool(os.getenv("GROQ_API_KEY")), "theses": len(store.list_theses()), "tpm": int(os.getenv("GROQ_TPM_LIMIT") or 8000),
            "search": "Serper" if os.getenv("SERPER_API_KEY") else "free web search", "profiles_error": profiles_error}


# ---------- screening jobs ----------
def _run(job_id: str, inp: Inputs, thesis: Thesis, thesis_id: int) -> None:
    job = JOBS[job_id]
    job.update(status="running", started=time.time())

    def step(key: str, state: str, detail: str) -> None:
        job["steps"][key] = {"state": state, "detail": detail}

    try:
        prof = load_profiles()
        result = analyse(inp, make_llm(lambda m: job.update(note=m)), thesis, prof, step=step, **RUN_KW)
        result["thesis"]["id"] = thesis_id
        job.update(status="done", note="Done", run_id=store.save(result))
    except (IntakeError, LLMError, ProfilesError) as e:
        for s in job["steps"].values():
            if s["state"] == "running":
                s["state"] = "error"
        job.update(status="error", error=str(e))
    except Exception:  # never leave the page spinning; details go to the terminal
        log.exception("screening failed")
        job.update(status="error", error="Something went wrong while screening. See the terminal for details.")


@app.post("/api/screen")
async def start_screen(deck: Optional[UploadFile] = File(None), website: str = Form(""), hint: str = Form(""),
                       thesis_id: str = Form(""), resources: list[UploadFile] = File(default=[])):
    if not os.getenv("GROQ_API_KEY"):
        raise HTTPException(400, "No GROQ_API_KEY found. Add your free Groq key to the .env file and restart the app.")
    if not store.list_theses():
        raise HTTPException(400, "Create a fund thesis first: it defines what each company is compared against.")
    row = store.get_thesis(int(thesis_id)) if thesis_id.strip().isdigit() else None
    if row is None:
        raise HTTPException(400, "Choose a fund thesis to compare the company against." if not thesis_id.strip()
                            else "That fund thesis no longer exists. Choose another.")
    try:
        thesis = build_thesis(row["data"])
        errors, _ = check_thesis(thesis)
        if errors:
            raise ThesisError(f"The fund thesis '{row['name']}' needs fixing before it can be used: {errors[0]}")
    except ThesisError as e:
        raise HTTPException(400, str(e))
    try:
        url = normalize_url(website)
        if deck is None or not deck.filename:
            raise IntakeError("Upload the pitch deck (PDF).")
        deck_bytes = await deck.read(MAX_FILE_BYTES + 1)
        check_upload(deck.filename, deck_bytes, deck=True)
        resources = [r for r in resources if r.filename]
        if len(resources) > MAX_RESOURCES:
            raise IntakeError(f"Add at most {MAX_RESOURCES} extra files.")
        extra = []
        for r in resources:
            data = await r.read(MAX_FILE_BYTES + 1)
            check_upload(r.filename, data)
            extra.append((Path(r.filename).name, data))
    except IntakeError as e:
        raise HTTPException(400, str(e))

    inp = Inputs(deck_name=Path(deck.filename).name, deck=deck_bytes, website=url, resources=extra, hint=hint.strip()[:120])
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        busy = any(j["status"] in ("queued", "running") for j in JOBS.values())
        JOBS[job_id] = {"id": job_id, "status": "queued", "created": time.time(), "error": None, "run_id": None,
                        "note": "Waiting for the previous screening to finish..." if busy else "Starting...",
                        "steps": {k: {"state": "pending", "detail": ""} for k, _ in STEPS}}
        finished = [k for k, j in sorted(JOBS.items(), key=lambda kv: kv[1]["created"]) if j["status"] in ("done", "error")]
        for old in finished[:-20]:  # forget old finished jobs only; never a queued or running one
            JOBS.pop(old, None)
    POOL.submit(_run, job_id, inp, thesis, row["id"])
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job.")
    elapsed = time.time() - job.get("started", job["created"])
    return {k: job[k] for k in ("id", "status", "note", "error", "run_id", "steps")} | {
        "elapsed": round(elapsed), "step_labels": dict(STEPS)}


# ---------- history ----------
@app.get("/api/runs")
def runs():
    return store.list()


def _get_run(run_id: int) -> dict:
    r = store.get(run_id)
    if r is None:
        raise HTTPException(404, "No saved screening with that id.")
    return r


@app.get("/api/runs/{run_id}")
def run(run_id: int):
    r = _get_run(run_id)
    tid = (r.get("thesis") or {}).get("id")
    return {"id": run_id, "thesis_exists": bool(tid and store.get_thesis(tid)), **r}


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: int):
    if not store.delete(run_id):
        raise HTTPException(404, "No saved screening with that id.")
    return {"deleted": run_id}


@app.get("/api/runs/{run_id}/memo")
def memo(run_id: int):
    r = _get_run(run_id)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{r.get('company', 'company')}_screening.md")  # HTTP headers must be ASCII
    return PlainTextResponse(to_markdown(r), media_type="text/markdown",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/runs/{run_id}/memo.pdf")
def memo_pdf(run_id: int):
    r = _get_run(run_id)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{r.get('company', 'company')}_screening_memo.pdf")
    try:
        data = build_pdf(r, r.get("created"))
    except Exception:  # a template bug must give a readable message, not a broken download
        log.exception("PDF generation failed for run %s", run_id)
        raise HTTPException(500, "The PDF could not be generated. See the terminal for details; the Markdown memo is still available.")
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}"'})


class Rescore(BaseModel):
    weights: dict[str, float]
    advance_at: float
    maybe_at: float


@app.post("/api/runs/{run_id}/rescore")
def rescore(run_id: int, body: Rescore):
    """What-if: same AI scores and verification, different base weights/thresholds. No AI call, instant."""
    r = _get_run(run_id)
    if r.get("version") != 2:
        raise HTTPException(400, "What-if needs a result from the current version; re-run this screening.")
    if set(body.weights) != set(DIMENSIONS) or any(v < 0 for v in body.weights.values()) or sum(body.weights.values()) <= 0:
        raise HTTPException(400, "Give a weight >= 0 for every dimension, not all zero.")
    if not (0 <= body.maybe_at <= body.advance_at <= 10):
        raise HTTPException(400, "Thresholds must be between 0 and 10, with 'maybe' not above 'advance'.")
    try:
        prof = load_profiles()
    except ProfilesError as e:
        raise HTTPException(400, str(e))
    t = SimpleNamespace(weights=body.weights, advance_at=body.advance_at, maybe_at=body.maybe_at)  # all evaluate() reads
    ev = evaluate(r["raw_scores"], r["evidence_coverage"], r["fit"], t, r["context"], r["claims"], prof,
                  search_ok=r["search"]["available"], website_ok=r["crawl"]["pages_crawled"] > 0)
    return {k: ev[k] for k in ("decision", "decision_reason", "overall", "overall_raw", "adjustment", "weights", "scores")}


# ---------- fund theses ----------
class ThesisBody(BaseModel):
    text: Optional[str] = None   # YAML (advanced editor)
    data: Optional[dict] = None


def _thesis_payload(row: dict) -> dict:
    t = build_thesis(row["data"])
    errors, warnings = check_thesis(t)
    return {"id": row["id"], "name": row["name"], "data": t.model_dump(), "runs": row["runs"], "updated": row["updated"],
            "summary": thesis_summary(t), "errors": errors, "warnings": warnings}


def _validated(body: ThesisBody) -> tuple[Thesis, list[str]]:
    if body.text is not None:
        t = parse_thesis(body.text)
    elif body.data is not None:
        t = build_thesis(body.data)
    else:
        raise HTTPException(400, "Nothing to save.")
    errors, warnings = check_thesis(t)
    if errors:
        raise HTTPException(400, "Not saved: " + " ".join(errors))
    return t, warnings


@app.get("/api/thesis-options")
def thesis_options():
    return {"dimensions": DIMENSIONS, "suggestions": all_suggestions()}


@app.get("/api/theses")
def list_theses():
    out = []
    for row in store.list_theses():
        try:
            out.append(_thesis_payload(row))
        except ThesisError as e:  # a corrupted row must not hide the others
            out.append({"id": row["id"], "name": row["name"], "runs": row["runs"], "updated": row["updated"], "errors": [str(e)],
                        "warnings": [], "summary": None, "data": None})
    return out


@app.post("/api/theses")
def create_thesis(body: ThesisBody):
    try:
        t, warnings = _validated(body)
        tid = store.save_thesis(t.model_dump())
    except (ThesisError, ValueError) as e:
        raise HTTPException(400, f"Not saved: {e}")
    return {**_thesis_payload(store.get_thesis(tid)), "warnings": warnings}


@app.get("/api/theses/{thesis_id}")
def get_thesis(thesis_id: int):
    row = store.get_thesis(thesis_id)
    if row is None:
        raise HTTPException(404, "No such fund thesis.")
    return {**_thesis_payload(row), "text": yaml.safe_dump(row["data"], sort_keys=False, allow_unicode=True)}


@app.put("/api/theses/{thesis_id}")
def update_thesis(thesis_id: int, body: ThesisBody):
    try:
        t, warnings = _validated(body)
        store.save_thesis(t.model_dump(), thesis_id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except (ThesisError, ValueError) as e:
        raise HTTPException(400, f"Not saved: {e}")
    return {**_thesis_payload(store.get_thesis(thesis_id)), "warnings": warnings}


@app.delete("/api/theses/{thesis_id}")
def delete_thesis(thesis_id: int):
    if not store.delete_thesis(thesis_id):
        raise HTTPException(404, "No such fund thesis.")
    return {"deleted": thesis_id}  # past reports keep their own copy of the thesis they were scored against


@app.post("/api/theses/{thesis_id}/duplicate")
def duplicate_thesis(thesis_id: int):
    row = store.get_thesis(thesis_id)
    if row is None:
        raise HTTPException(404, "No such fund thesis.")
    names = {r["name"].lower() for r in store.list_theses()}
    n, name = 1, f"{row['name']} (copy)"
    while name.lower() in names:
        n += 1
        name = f"{row['name']} (copy {n})"
    tid = store.save_thesis({**row["data"], "fund_name": name[:80]})
    return _thesis_payload(store.get_thesis(tid))


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT") or 8501)
    print(f"\n  Deal Screening is running:  http://localhost:{port}\n  (Ctrl-C to stop)\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
