# Buy-side Deal Screener

A deal-screening tool for early-stage investors. You tell it **what your fund invests in**, then give it a
company's **pitch deck (PDF)** and **website**. It checks what the company claims against the open web and
returns a scored report with an **ADVANCE / MAYBE / PASS** decision, red flags, and questions to ask the founders.
You can download the report as a formal **PDF memorandum**.

It runs on your own computer, stores everything locally, and can be used for free (Groq free tier) or with any AI key you already have.

> The application lives in [`Screening11-main/`](Screening11-main/). Everything below is what the code in that folder does.

## What it does, step by step

1. **Pick a fund thesis (optional, recommended).** A thesis is your mandate: target countries, stages, sectors (target / risky / excluded),
   round size, decision thresholds and the base weight of each scoring factor. You can keep several (for example a
   seed fund and a Series A fund) and choose one per screening. The editor gives suggestions, templates and presets.
   If you choose none, the app asks you to confirm and runs an **open screening**: any country, stage and sector, no round limits,
   standard thresholds (advance at 7, maybe at 5.5) and the default weight for every scoring dimension.
2. **Add the company.** The pitch deck (PDF) and the website are both required. Up to 5 extra files
   (PDF, Word, text, Markdown, CSV) are optional.
3. **The app screens it** (about 2 to 4 minutes):

   | Step | What happens |
   |---|---|
   | Read documents | Extracts text from the deck and extra files. Image-only PDFs are rejected with a clear message. |
   | Crawl the website | Follows the sitemap and links, ranks pages by relevance (team, product, pricing, customers, press), respects robots.txt, fetches public addresses only, records broken links. |
   | Extract claims (AI) | Pulls the company profile and checkable claims. Each claim carries an exact quote, and **code verifies the quote really appears in the source**. |
   | Search the web | Looks for funding, founders, traction and adverse news. Each source is labelled *independent*, *self-reported* (LinkedIn, Crunchbase...) or *company-owned*. |
   | Verify claims (AI + code) | The AI compares each claim with evidence. **Code decides what may be called "verified"**: only an independent source counts. Contradictions need cited evidence. It also flags deck-vs-website number mismatches, founders found nowhere, HQ or founding-year mismatches, and implausible round size for the stage. |
   | Score and decide (AI + code) | The AI scores 11 dimensions. **Code** then applies your thesis weights, adjusts them for the company's stage and business model, lowers scores that relied on contradicted claims, computes a confidence level, checks the mandate and makes the decision. |

4. **Read the report.** It shows the decision and confidence, red flags, a claim table (status, quote, evidence links),
   a scorecard with every weight adjustment explained, a live what-if re-weighting, mandate fit, the extracted profile,
   all sources, and every data-quality issue from the run. Past reports are kept in **History**, and each report keeps
   its own copy of the thesis it was scored against, so editing a thesis never changes old reports.

## How the mandate check works

Code (not the AI) compares the company with your thesis: geography (HQ text is read for country names; "Silicon Valley"
is *Unknown*, not a mismatch), stage (free text mapped to a stage), sector (whole-word matching with synonyms) and round
size (below your minimum, or above 3x your maximum, is flagged). A clear mismatch on any criterion makes the
recommendation **PASS** regardless of score. An empty list means "no restriction".

## AI providers

Use **any** of: Anthropic, OpenAI, Gemini, OpenRouter, Groq (free tier), or any OpenAI-compatible service.
Add one or several on the **AI keys** page (each is tested before saving) or in `.env`. Keys are tried in the order
listed. If one is slow, rate-limited, out of credit, rejected or down, the app switches to the next in the middle of a
screening, and the report states which AI answered and when it switched.

## Run it

```bash
cd Screening11-main
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # optional: put a key here, or add it later on the AI keys page
python app.py            # open http://localhost:8501
```

On first start the app asks for an AI key (if none is set). A fund thesis is optional. Tests run offline:
`python -m pytest -q`.

## Limits worth knowing

- **Verification is only as good as public evidence.** Many early-stage claims will honestly stay *unverified*.
- **Scoring weights and multipliers are judgement-based starting points**, not calibrated on past deal outcomes. They are editable
  (`scoring_profiles.yaml` and your fund theses).
- **No OCR**: scanned decks are rejected. Websites that need JavaScript to render may yield few pages.
- **The AI can misjudge** a business model or mistake two events for a contradiction. Every verdict shows its evidence so you can check.
- **Privacy**: deck and website text go to the AI provider you configured; search queries go to a public search engine.
- **Tested providers**: Groq was tested live (including automatic failover from a rejected key). Anthropic, Gemini and
  OpenRouter follow the providers' documented formats and were tested against simulated servers only. Use *Test* on the AI keys page for yours.
- This is a screening aid, not investment advice. A human should make the decision.

## Project layout

| Path | Purpose |
|---|---|
| `Screening11-main/app.py` | Web server (FastAPI) and JSON API |
| `Screening11-main/static/` | The web interface (plain HTML, CSS, JS; no build step) |
| `Screening11-main/screening/` | Pipeline, crawler, web search, verification rules, scoring, AI engine, PDF memo, storage |
| `Screening11-main/scoring_profiles.yaml` | Stage and business-model weight adjustments, each with a written reason |
| `Screening11-main/tests/` | Offline tests with fake AI, crawl and search |
| `Screening11-main/DEPLOY.md`, `Dockerfile`, `render.yaml` | Optional notes for a private hosted copy (not required to use the app) |
