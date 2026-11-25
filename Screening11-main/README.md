# Deal Screening (free, local)

A local due-diligence screener for early-stage investors. Create a **fund thesis** (what you invest in), then give it a
company's **pitch deck (PDF)** and **website** (both required) plus any extra files. It crawls the website, searches the web, extracts the company's claims,
**checks each claim against independent evidence**, scores the company with weights adapted to its stage and business
model, and produces a report with an **ADVANCE / MAYBE / PASS** decision, red flags and founder questions. Download it as a
formal **PDF memorandum**.

Free to run: use [Groq](https://console.groq.com)'s free tier, or any AI you already pay for. Web search needs no key.
Everything is stored where the app runs (your computer, or your own server).

**Bring any AI key, or several.** Anthropic, OpenAI, Gemini, OpenRouter, Groq, or any OpenAI-compatible service (Mistral,
Together, DeepSeek, a local Ollama...). Add keys on the **AI keys** page (each is tested before it is saved) or in `.env`.
They are used in the order listed. If one is slow (over 60 s), rate-limited, out of credit, rejected or down, the app moves
to the next one in the middle of a screening, pauses the failed key for a while, and the report says which AI answered and
when it switched.

## Run it

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # optional: paste a key there, or add it later on the app's AI keys page
python app.py               # then open http://localhost:8501
```

On first start the app asks for an AI key (if none is set) and then for a fund thesis, so the app asks you to create one before anything can be screened. You can keep
several (e.g. a seed fund and a Series A fund) and pick which one each company is compared against.

Tests (offline, no key needed): `python -m pytest -q`

## What happens in a screening (about 2-4 minutes)

| Step | What it does | If it fails |
|---|---|---|
| 1. Read documents | Text from the deck and up to 5 extra files (PDF, DOCX, TXT, MD, CSV). Long files keep their start **and end** (team and round slides are usually last). | An unreadable/scanned deck stops the run with a clear message; a bad extra file is skipped and reported. |
| 2. Crawl the website | Starts immediately, in the background. Sitemap + links, ranked by relevance (about, team, product, pricing, customers, press...), max 3 pages per section, pagination and other-language copies capped, robots.txt respected, broken links recorded, 20 pages / 75 s limit, public addresses only (also after redirects). | Unreachable site or a crawler error: the report says so, confidence drops, the run continues on documents and web search. |
| 3. Extract claims (AI) | Profile + important checkable claims from the documents, then from the most relevant website pages. Every claim carries an exact quote, and **code checks the quote really is in the source**. | Website extraction failure is reported and skipped. |
| 4. Search the web | Runs while step 3 works on the website. Identity, funding, founders, traction and adverse-news queries. Off-topic results dropped. Every source is classed **independent**, **self-reported** (LinkedIn, Crunchbase, social...) or **company**. Failed queries are retried once. | Search unavailable or crashed: reported; claims can only be compared across the company's own materials. |
| 5. Verify claims (AI + code) | Code pairs each claim with its most likely evidence; the AI compares them; **code decides what may be shown**: *verified* needs an independent source; company/self-reported agreement is only *company sources only*; contradicted/outdated need cited evidence; invalid or self-citations are rejected. Code also flags deck-vs-website number mismatches, founders not found anywhere, HQ/founding-year mismatches, implausible round size for the stage, missing independent coverage and possible adverse coverage. | Verification failure: claims stay *unverified* and the issue is reported. |
| 6. Score & report (AI + code) | The AI scores 11 dimensions against what is normal for the company's stage and business model. **Code** then applies your thesis weights x stage x business-model multipliers, lowers dimensions that relied on contradicted claims (a deck-vs-website disagreement counts once), computes confidence, checks your mandate and makes the decision. | - |

The report shows the verdict and confidence, red flags, a claim table (status, quote, evidence links), a scorecard
(score on a 1-10 scale, raw vs final after penalties, base vs applied weight with the reason for every change, and each
dimension's contribution), the scoring context with a live **what-if** re-weighting, mandate fit, the company profile
(with whether each field came from the documents or the website), every source used, and every data-quality or
workflow issue from the run.

## Controlling the scoring

- **Fund theses** (the *Fund theses* page): create, edit, duplicate and delete as many as you like. Each holds your countries,
  stages, sectors (target, risky and excluded), round size, decision thresholds and base weights, with suggestions as you
  type, templates and presets. Each report keeps its own copy of the thesis it was scored against, so editing or deleting
  a thesis never changes past reports. (Older versions kept one `thesis.yaml`; it is imported automatically on first start.)
- **`scoring_profiles.yaml`**: how weights adapt by stage group (early / venture / growth) and business model (SaaS,
  marketplace, consumer, fintech, health/bio, deep tech, services), the verification penalty, confidence rules,
  outlier cap/boost and typical round sizes. Every multiplier has a written `why` that is shown in the report. The file
  is validated at the start of every screening; an invalid edit is refused with the reason.

## How a company is compared with the fund thesis

Code, not the AI, makes this comparison, and every row in the report shows what was compared and where the company's value came from.

- **Geography:** the HQ text is read for country names ("Warsaw, Poland" is Poland; "Atlanta, Georgia, United States" is the US).
  A founder link to a listed country also counts. A country that is clearly not listed is a mismatch; text that names no
  country ("Silicon Valley") is *Unknown*, never a mismatch.
- **Stage:** free text is mapped to a stage ("Seed Round" = seed; "seed / series a" = both). Core stage = meets; double-check stage =
  flagged; any other recognised stage = mismatch; unrecognised or missing = *Unknown*.
- **Sector:** matched by whole words with common synonyms ("artificial intelligence" = AI). An excluded sector is a mismatch,
  a risky one is flagged, a target sector meets.
- **Round size:** below the fund's minimum ticket, or more than 3x its largest ticket, is flagged.
- **An empty list means "no restriction"** (a fund with no countries accepts any HQ).
- A clear mismatch on any criterion makes the recommendation **PASS** whatever the score; flagged or unknown items keep the
  recommendation but are listed so you can confirm them. A thesis is also checked against itself when saved (no stage, a
  sector in two lists, minimum above maximum, thresholds out of order).

## Limits - read these

- **Free-tier speed:** Groq's free tier allows ~8,000 tokens/minute, so with Groq as the first key the 4 AI calls are paced (~1 minute apart). Paid providers are not paced and read more text per call.
- **Depth limits:** about 16,000 characters of documents and ~7 website pages are analysed in depth per run; up to 16
  key claims are verified. The report says when anything was cut.
- **Verification is only as good as public evidence.** Search snippets often do not state specific numbers, so many
  early-stage claims will honestly stay *unverified*. Free search backends are sometimes flaky; set `SERPER_API_KEY`
  in `.env` to use Serper (free credits) instead.
- **"Contradicted" vs "outdated"** depends on dates the AI can see; decks often have none. Both lower the score equally.
  The AI can also mistake two different events for a contradiction; every verdict shows its evidence so you can check.
- **Weights and multipliers are judgement-based starting points**, not calibrated on historical outcomes. Edit them.
- **No OCR:** image-only PDFs are rejected. Sites that need JavaScript to render may yield few pages.
- **Crawler safety:** only public addresses are fetched, including after redirects. One known gap: the hostname is
  checked and then looked up again by the HTTP library, so a hostile site with rigged DNS could in theory point at a
  private address in between. Don't screen websites you consider hostile on a machine with sensitive local services.
- **Privacy:** documents and website text go to the AI provider(s) you configured; search queries go to a public search engine.
- **Provider support:** Groq was tested live, including automatic failover from a rejected OpenAI key. The Anthropic, Gemini and OpenRouter connections follow the providers' documented formats and are tested against simulated servers only; use *Test* on the AI keys page to confirm yours.
- The AI classifies the business model; if it picks the wrong type the weighting follows it (shown in the report).

## Files

| File | Purpose |
|---|---|
| `app.py` | web server (FastAPI) + JSON API; localhost only by default; optional login when `APP_PASSWORD` is set |
| `Dockerfile`, `render.yaml`, `DEPLOY.md` | private hosted copy (see DEPLOY.md) |
| `static/` | the web UI: `index.html`, `style.css`, `app.js`, `thesis.js` (no build step) |
| `screening/pipeline.py` | the 6-step pipeline |
| `screening/crawler.py` / `websearch.py` | website crawl / web search with source classification |
| `screening/verify.py` | evidence rules, quote checks, cross-source consistency checks |
| `screening/scoring.py` | dynamic weights, penalties, confidence, mandate fit, decision |
| `screening/models.py` / `prompts.py` / `llm.py` | AI output validation / prompts / AI engine with failover across keys |
| `screening/providers.py` / `keys.py` | one adapter per AI API (OpenAI-style, Anthropic, Gemini) / key storage, testing and ordering |
| `screening/intake.py` / `report.py` / `store.py` | document parsing / Markdown memo / local SQLite (history and fund theses; `SCREENING_DB` changes its location) |
| `screening/suggestions.py` / `thesis.py` | thesis-editor suggestions and text readers (countries, stages, sectors) / thesis model and self-checks |
| `screening/pdf.py` + `assets/fonts/` | the PDF memorandum template (Roboto and Source Serif fonts, licences included) |
| `tests/` | offline tests with fake AI servers, crawl and search |
