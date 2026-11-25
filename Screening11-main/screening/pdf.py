"""Investment screening memorandum as a PDF (A4). build_pdf(result, created) -> bytes.

Formal layout: header/footer on every page, cover block with the recommendation, numbered sections, repeating table
headers, "Page x of y". Status is always written out in words (not colour alone) so it survives black-and-white printing.
All text from the analysis is escaped, and characters the fonts cannot draw are replaced instead of printing boxes."""
from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.graphics.shapes import Drawing, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (BaseDocTemplate, CondPageBreak, Frame, KeepTogether, PageTemplate, Paragraph, Spacer, Table, TableStyle)

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
INK, MUTED, FAINT = colors.HexColor("#111111"), colors.HexColor("#52525B"), colors.HexColor("#71717A")
GOLD, LINE, WASH = colors.HexColor("#FECE14"), colors.HexColor("#D4D4D8"), colors.HexColor("#F7F7F5")
TONES = {  # (text, background) - text colours pass WCAG AA on their background
    "good": (colors.HexColor("#166534"), colors.HexColor("#ECFDF3")), "warn": (colors.HexColor("#92400E"), colors.HexColor("#FFF7E6")),
    "bad": (colors.HexColor("#991B1B"), colors.HexColor("#FEF2F2")), "info": (colors.HexColor("#334155"), colors.HexColor("#F1F5F9"))}
STATUS_TONE = {"verified": "good", "consistent": "info", "unverified": "warn", "contradicted": "bad", "outdated": "bad", "not_checked": "info"}
STATUS_WORD = {"verified": "Verified", "consistent": "Company sources only", "unverified": "Unverified", "contradicted": "Contradicted",
               "outdated": "Outdated", "not_checked": "Not checked"}
STATE_TONE = {"ok": "good", "warn": "warn", "unknown": "warn", "fail": "bad"}
STATE_WORD = {"ok": "Meets", "warn": "Check", "unknown": "Unknown", "fail": "Does not meet"}
DECISION_TONE = {"ADVANCE": "good", "MAYBE": "warn", "PASS": "bad"}
DECISION_LABEL = {"ADVANCE": "ADVANCE", "MAYBE": "MAYBE", "PASS": "PASS"}
KIND_WORD = {"independent": "Independent", "profile": "Self-reported", "company": "Company"}
W = A4[0] - 36 * mm  # text width

_fonts_ready = False
_missing: set[str] = set()


def _register_fonts() -> None:
    global _fonts_ready
    if _fonts_ready:
        return
    for name, file in (("Body", "Roboto-Regular.ttf"), ("Body-Medium", "Roboto-Medium.ttf"), ("Body-Bold", "Roboto-Bold.ttf"),
                       ("Body-Italic", "Roboto-Italic.ttf"), ("Head", "SourceSerifPro-Semibold.ttf"), ("Head-Bold", "SourceSerifPro-Bold.ttf")):
        pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / file)))
    pdfmetrics.registerFontFamily("Body", normal="Body", bold="Body-Bold", italic="Body-Italic", boldItalic="Body-Bold")
    pdfmetrics.registerFontFamily("Head", normal="Head", bold="Head-Bold", italic="Head", boldItalic="Head-Bold")
    face = pdfmetrics.getFont("Body").face
    _missing.update(chr(c) for c in range(0x20, 0x2600) if c not in face.charToGlyph and chr(c).isprintable())
    _fonts_ready = True


_REPLACE = {"→": "->", "←": "<-", "✓": "yes", "✕": "x", "⚠": "!", "≥": ">=", "≤": "<=", "​": "", " ": " ", "\t": " "}


def _t(v) -> str:
    """Escape for the PDF's mini-markup and replace characters the font cannot draw."""
    s = "" if v is None else str(v)
    s = re.sub(r"[\x00-\x08\x0b-\x1f]", " ", s)
    s = "".join(_REPLACE.get(ch, "?" if ch in _missing else ch) for ch in s)
    return escape(s)


def _link(url: str, label: str = "") -> str:
    url = (url or "").strip()
    text = _t(label or url)
    return f'<a href="{escape(url, {chr(34): "&quot;"})}" color="#1D4ED8">{text}</a>' if re.match(r"^https?://", url, re.I) else text


def _styles() -> dict[str, ParagraphStyle]:
    def s(name, **kw):
        base = dict(fontName="Body", fontSize=9, leading=12.6, textColor=INK, alignment=TA_LEFT, splitLongWords=1)
        return ParagraphStyle(name, **{**base, **kw})
    return {
        "body": s("body"), "small": s("small", fontSize=7.8, leading=10.4, textColor=MUTED), "tiny": s("tiny", fontSize=7, leading=9.2, textColor=FAINT),
        "lede": s("lede", fontSize=10.2, leading=14.6), "eyebrow": s("eyebrow", fontName="Body-Bold", fontSize=7.4, leading=10, textColor=FAINT, spaceAfter=3),
        "title": s("title", fontName="Head-Bold", fontSize=27, leading=31, spaceAfter=3), "sub": s("sub", fontSize=9.5, leading=13.5, textColor=MUTED),
        "h1": s("h1", fontName="Head", fontSize=13.5, leading=17, spaceBefore=14, spaceAfter=0, keepWithNext=1),
        "h2": s("h2", fontName="Body-Bold", fontSize=8.6, leading=11.5, textColor=MUTED, spaceBefore=8, spaceAfter=3, keepWithNext=1),
        "th": s("th", fontName="Body-Bold", fontSize=7.2, leading=9.6, textColor=MUTED), "td": s("td", fontSize=8.4, leading=11.2),
        "tdb": s("tdb", fontName="Body-Medium", fontSize=8.4, leading=11.2), "num": s("num", fontName="Body-Medium", fontSize=8.4, leading=11.2, alignment=2),
        "bullet": s("bullet", leftIndent=9, bulletIndent=0, spaceAfter=3), "big": s("big", fontName="Head-Bold", fontSize=22, leading=24),
        "kpi": s("kpi", fontName="Head-Bold", fontSize=15, leading=17), "kpil": s("kpil", fontName="Body-Bold", fontSize=6.6, leading=8.4, textColor=FAINT),
    }


class _Numbered(canvas.Canvas):
    """Canvas that knows the total page count, for 'Page x of y'."""
    def __init__(self, *a, meta=None, **kw):
        super().__init__(*a, **kw)
        self._pages, self.meta = [], meta or {}

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self._chrome(total)
            super().showPage()
        super().save()

    def _chrome(self, total: int) -> None:
        m, pw, ph = self.meta, A4[0], A4[1]
        self.setStrokeColor(GOLD); self.setLineWidth(2.2); self.line(18 * mm, ph - 11 * mm, pw - 18 * mm, ph - 11 * mm)
        self.setFillColor(MUTED); self.setFont("Body-Bold", 6.8)
        self.drawString(18 * mm, ph - 15.2 * mm, "INVESTMENT SCREENING MEMORANDUM")
        self.drawRightString(pw - 18 * mm, ph - 15.2 * mm, "CONFIDENTIAL")
        self.setStrokeColor(LINE); self.setLineWidth(0.5); self.line(18 * mm, 15 * mm, pw - 18 * mm, 15 * mm)
        self.setFont("Body", 7); self.setFillColor(FAINT)
        self.drawString(18 * mm, 10.6 * mm, f"{m.get('company', '')}  ·  {m.get('fund', '')}")
        self.drawRightString(pw - 18 * mm, 10.6 * mm, f"Page {self._pageNumber} of {total}")
        self.setFont("Body", 6.2)
        self.drawCentredString(pw / 2, 6.6 * mm, "AI-assisted screening. Claims not marked Verified have not been independently confirmed.")


def _table(rows, widths, header=True, zebra=False, extra=None, repeat=1):
    t = Table(rows, colWidths=widths, repeatRows=repeat if header else 0, splitByRow=1)
    st = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
          ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE)]
    if header:
        st += [("BACKGROUND", (0, 0), (-1, 0), WASH), ("LINEBELOW", (0, 0), (-1, 0), 0.9, INK), ("LINEABOVE", (0, 0), (-1, 0), 0.9, INK)]
    if zebra:
        st += [("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FBFBFA")])]
    t.setStyle(TableStyle(st + (extra or [])))
    return t


def _pill_html(text: str, tone: str) -> str:
    return f'<font name="Body-Bold" color="{TONES[tone][0].hexval().replace("0x", "#")}">{_t(text)}</font>'


def _pill(text: str, tone: str, st) -> Paragraph:
    return Paragraph(_pill_html(text, tone), st["td"])


def _section(n: int, title: str, st) -> list:
    return [CondPageBreak(50 * mm), Paragraph(f'<font color="#B38B00">{n:02d}</font>&nbsp;&nbsp;{_t(title)}', st["h1"]),
            Table([[""]], colWidths=[W], rowHeights=[2], style=TableStyle([("LINEBELOW", (0, 0), (-1, -1), 1.1, INK)])), Spacer(1, 7)]


def _bullets(items, st, empty="None."):
    return [Paragraph(_t(x), st["bullet"], bulletText="•") for x in items] or [Paragraph(f"<i>{_t(empty)}</i>", st["small"])]


def _scorebars(dims: dict, width: float) -> Drawing:
    row_h, label_w, num_w = 15, 118, 40
    d = Drawing(width, row_h * len(dims) + 18)
    x0, bar_w = label_w, width - label_w - num_w
    for i, (k, v) in enumerate(dims.items()):
        y = d.height - 14 - i * row_h
        final, raw = v["final"], v["raw"]
        tone = "good" if final >= 7 else "warn" if final >= 5 else "bad"
        d.add(String(0, y + 2, k.replace("_", " ").capitalize(), fontName="Body", fontSize=8, fillColor=INK))
        d.add(Rect(x0, y, bar_w, 7, fillColor=colors.HexColor("#EFEFEC"), strokeColor=None))
        d.add(Rect(x0, y, bar_w * (final - 1) / 9 if final > 1 else 1.5, 7, fillColor=TONES[tone][0], strokeColor=None))
        if v["penalty"]:  # hollow marker where the score was before verification penalties
            mx = x0 + bar_w * (raw - 1) / 9
            d.add(Polygon([mx, y - 1.5, mx + 3.5, y + 3.5, mx, y + 8.5, mx - 3.5, y + 3.5], fillColor=colors.white, strokeColor=TONES["bad"][0], strokeWidth=0.8))
        d.add(String(width - 2, y + 1.5, f"{final:g}", fontName="Body-Bold", fontSize=8.4, fillColor=INK, textAnchor="end"))
    for s in (1, 5, 10):  # axis labels
        d.add(String(x0 + bar_w * (s - 1) / 9, 2, str(s), fontName="Body", fontSize=6.4, fillColor=FAINT, textAnchor="middle"))
    return d


def _fmt(n, digits=2) -> str:
    return "-" if n is None else f"{float(n):.{digits}f}".rstrip("0").rstrip(".") or "0"


def _legacy(r: dict, st) -> list:
    return [Paragraph("EARLIER VERSION", st["eyebrow"]), Paragraph(_t(r.get("company", "Unknown")), st["title"]),
            Paragraph(f"Recorded decision: <b>{_t(r.get('decision', ''))}</b>, score {_t(_fmt(r.get('overall')))}/10.", st["lede"]), Spacer(1, 8),
            Paragraph("This screening was made before claim verification, website analysis and adaptive scoring existed, so it cannot "
                      "be turned into a full memorandum. Run the screening again to generate one.", st["body"])]


def build_pdf(r: dict, created: str | None = None) -> bytes:
    _register_fonts()
    st = _styles()
    when = ""
    try:
        when = datetime.fromisoformat((created or r.get("created") or "").replace("Z", "+00:00")).strftime("%d %B %Y")
    except ValueError:
        when = datetime.now().strftime("%d %B %Y")
    meta = {"company": r.get("company", ""), "fund": r.get("fund", "")}
    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=22 * mm, bottomMargin=20 * mm,
                          title=f"{r.get('company', '')} - Investment Screening Memorandum", author="Deal Screening",
                          subject=f"Screening memorandum prepared for {r.get('fund', '')}")
    doc.addPageTemplates([PageTemplate(id="p", frames=[Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="f", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)])])
    story = _legacy(r, st) if r.get("version") != 2 else _story(r, st, when)
    doc.build(story, canvasmaker=lambda *a, **k: _Numbered(*a, meta=meta, **k))
    return buf.getvalue()


def _story(r: dict, st: dict, when: str) -> list:
    p, ctx, cf, c, brief, cw, sr = r["profile"], r["context"], r["confidence"], r["claim_counts"], r["brief"], r["crawl"], r["search"]
    checked = sum(v for k, v in c.items() if k != "not_checked")
    tone = DECISION_TONE[r["decision"]]
    fg, bg = TONES[tone]
    S: list = []

    # ---- cover block ----
    S += [Paragraph("PREPARED FOR " + _t(r.get("fund", "").upper()), st["eyebrow"]), Paragraph(_t(r["company"]), st["title"]),
          Paragraph("  ·  ".join(_t(x) for x in (hostname(r.get("website", "")), ctx["stage"] and ctx["stage"].replace("-", " ").title(), ctx["model_label"], p.get("hq_country")) if x), st["sub"]),
          Spacer(1, 10)]
    docs = [r["inputs"]["deck"]] + r["inputs"]["resources"]
    meta_rows = [[Paragraph("Date", st["th"]), Paragraph(_t(when), st["td"]), Paragraph("Documents reviewed", st["th"]), Paragraph(_t(", ".join(docs)), st["td"])],
                 [Paragraph("Fund thesis", st["th"]), Paragraph(_t(r.get("fund", "")), st["td"]), Paragraph("Website", st["th"]),
                  Paragraph(f"{_link(r.get('website', ''))}<br/>{cw['pages_crawled']} pages crawled, {len(cw['pages_analysed'])} analysed", st["td"])]]
    S += [_table(meta_rows, [24 * mm, 56 * mm, 32 * mm, W - 112 * mm], header=False), Spacer(1, 12)]

    # ---- recommendation panel ----
    def cell(label: str, value: str, color: str = "#111111", big: bool = False, unit: str = "") -> list:
        size, lead = (22, 26) if big else (14.5, 18)
        return [Paragraph(label, st["kpil"]), Paragraph(f'<font name="Head-Bold" size="{size}" color="{color}">{value}</font>{unit}',
                                                       ParagraphStyle("pv", parent=st["body"], leading=lead, spaceBefore=2))]
    panel = Table([[cell("RECOMMENDATION", DECISION_LABEL[r["decision"]], fg.hexval().replace("0x", "#"), True),
                    cell("OVERALL SCORE", _t(_fmt(r["overall"])), big=True, unit='<font size="9" color="#666666"> / 10</font>'),
                    cell("CONFIDENCE", _t(cf["level"])), cell("MANDATE FIT", _t(r["fit"]["status"].replace("_", " ").title()))]],
                  colWidths=[W * .30, W * .24, W * .23, W * .23])
    panel.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, 0), bg), ("BOX", (0, 0), (-1, -1), 0.8, INK), ("LINEBEFORE", (1, 0), (-1, 0), 0.4, LINE),
                               ("LINEBEFORE", (0, 0), (0, 0), 4, fg), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
                               ("LEFTPADDING", (0, 0), (-1, -1), 11)]))
    S += [panel, Spacer(1, 7), Paragraph(_t(r["decision_reason"]) + (f" <i>({_t(r['adjustment'])})</i>" if r.get("adjustment") else ""), st["body"]), Spacer(1, 9)]
    kp = lambda label, val, note: Paragraph(f'<font name="Body-Bold" size="6.6" color="#71717A">{label}</font><br/><font name="Head-Bold" size="14">{_t(val)}</font><br/><font size="7" color="#52525B">{_t(note)}</font>', st["body"])  # noqa: E731
    S += [_table([[kp("VERIFIED", f"{c['verified']}/{checked}", "independent source"), kp("CONTRADICTED", str(c["contradicted"] + c["outdated"]), "incl. outdated"),
                   kp("RED FLAGS", str(r["red_flag_count"]), "claims and checks"), kp("EVIDENCE", f"{r['evidence_coverage']}/11", "dimensions evidenced"),
                   kp("SOURCES", f"{cw['pages_crawled']} + {len(sr['hits'])}", "pages + web results")]], [W / 5] * 5, header=False,
                  extra=[("LINEAFTER", (0, 0), (-2, -1), 0.4, LINE), ("BOX", (0, 0), (-1, -1), 0.4, LINE), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)])]

    # ---- 1. executive summary ----
    S += _section(1, "Executive summary", st)
    S += [Paragraph(_t(brief["summary"]) or "<i>No summary was produced.</i>", st["lede"]), Spacer(1, 8)]
    cols = [[Paragraph("STRENGTHS", st["th"])] + _bullets(brief["strengths"], st), [Paragraph("CONCERNS", st["th"])] + _bullets(brief["concerns"], st),
            [Paragraph("NEXT STEPS", st["th"])] + _bullets(brief["next_steps"], st)]
    S += [Table([cols], colWidths=[W / 3] * 3, style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEAFTER", (0, 0), (-2, -1), 0.4, LINE),
                                                                    ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 2)]))]

    # ---- 2. red flags ----
    S += _section(2, "Red flags and discrepancies", st)
    flags = [x for x in r["claims"] if x["status"] in ("contradicted", "outdated")]
    checks = [x for x in r["consistency"] if x["state"] in ("warn", "fail")]
    if flags or checks:
        rows = [[Paragraph(h, st["th"]) for h in ("TYPE", "FINDING", "EVIDENCE")]]
        for x in flags:
            ev = "<br/>".join(f"{_t(e['id'])}: {_link(e.get('url', ''), e.get('title') or e['id'])}" for e in x["evidence"][:3]) or "-"
            rows.append([_pill(STATUS_WORD[x["status"]], "bad", st), Paragraph(f"<b>{_t(x['id'])}</b>  {_t(x['text'])}<br/><font color='#52525B'>{_t(x['note'])}</font>", st["td"]), Paragraph(ev, st["small"])])
        for x in checks:
            links = "<br/>".join(_link(l["url"], l.get("title")) for l in x.get("links", [])[:3]) or "-"
            rows.append([_pill("Check" if x["state"] == "warn" else "Failed", "warn" if x["state"] == "warn" else "bad", st),
                         Paragraph(f"<b>{_t(x['check'])}</b><br/><font color='#52525B'>{_t(x['detail'])}</font>", st["td"]), Paragraph(links, st["small"])])
        S += [_table(rows, [24 * mm, W - 24 * mm - 55 * mm, 55 * mm])]
    else:
        S += [Paragraph("No contradictions or discrepancies were found in the evidence available. This is not proof of accuracy: see the verification "
                        "counts on the first page.", st["body"])]

    # ---- 3. claim verification ----
    S += _section(3, "Claim verification", st)
    S += [Paragraph("Each important claim was checked against other company materials and the open web. <b>Verified</b> means an independent source "
                    "confirms it; <b>Company sources only</b> means only the company's own materials or self-reported profiles agree.", st["small"]), Spacer(1, 5)]
    rows = [[Paragraph(h, st["th"]) for h in ("#", "CLAIM AND SOURCE", "STATUS", "EVIDENCE")]]
    for x in r["claims"]:
        ev = "<br/>".join(f"{_t(e['id'])} <font color='#71717A'>({_t(KIND_WORD.get(e['kind'], e['kind']))})</font> {_link(e.get('url', ''), (e.get('title') or e['id'])[:70])}" for e in x["evidence"][:3]) or "-"
        warn = "<br/><font color='#92400E'>Quote not found in source: may be paraphrased.</font>" if not x["quote_found"] else ""
        note = f"<br/><font color='#52525B'>{_t(x['note'])}</font>" if x["note"] and x["status"] not in ("not_checked",) else ""
        rows.append([Paragraph(_t(x["id"]), st["tiny"]), Paragraph(f"{_t(x['text'])}<br/><font size='7' color='#71717A'>{_t(x['source_label'])} · {_t(x['category'])} · {_t(x['importance'])} importance</font>{note}{warn}", st["td"]),
                     _pill(STATUS_WORD[x["status"]], STATUS_TONE[x["status"]], st), Paragraph(ev, st["small"])])
    S += [_table(rows, [9 * mm, W - 9 * mm - 27 * mm - 52 * mm, 27 * mm, 52 * mm], zebra=True)]

    # ---- 4. scorecard ----
    S += _section(4, "Scorecard", st)
    dims = r["dimensions"]
    S += [Paragraph("Scores are 1 to 10, judged against what is normal for the company's stage and business model. A hollow marker shows the score "
                    "before deductions for contradicted claims.", st["small"]), Spacer(1, 4), _scorebars(dims, W), Spacer(1, 8)]
    rows = [[Paragraph(h, st["th"]) for h in ("DIMENSION", "SCORE", "WEIGHT", "RATIONALE")]]
    for k, v in dims.items():
        w = r["weights"][k]
        pen = f"<br/><font size='7' color='#991B1B'>raw {v['raw']}, -{_fmt(v['penalty'], 1)} for {_t(', '.join(v['penalty_claims']))}</font>" if v["penalty"] else ""
        rows.append([Paragraph(_t(k.replace("_", " ").capitalize()), st["tdb"]), Paragraph(f"{_t(_fmt(v['final'], 1))}{pen}", st["num"]),
                     Paragraph(f"{_t(_fmt(w['base']))} → {_t(_fmt(w['applied']))}".replace("→", "to"), st["num"]), Paragraph(_t(v["reasoning"]), st["td"])])
    S += [_table(rows, [34 * mm, 22 * mm, 24 * mm, W - 80 * mm], zebra=True)]
    factors = {}
    for k, w in r["weights"].items():
        for f in w["factors"]:
            factors.setdefault((f["source"], f["why"]), []).append((k.replace("_", " "), f["x"]))
    if factors:
        S += [Paragraph("Why the weights differ from the fund's base weights", st["h2"])]
        S += [Paragraph(f"<b>{_t(src)}.</b> {_t(why)} <font color='#52525B'>({_t(', '.join(f'{d} x{_fmt(x)}' for d, x in ds))})</font>", st["small"]) for (src, why), ds in factors.items()]

    # ---- 5. mandate ----
    S += _section(5, "Fund thesis and mandate fit", st)
    th = (r.get("thesis") or {}).get("data") or {}
    thr = r.get("thresholds") or {}
    S += [Paragraph(f"Compared against <b>{_t(r.get('fund', ''))}</b>. Advance at {_t(_fmt(thr.get('advance_at', th.get('advance_at', 7))))} or above, maybe at "
                    f"{_t(_fmt(thr.get('maybe_at', th.get('maybe_at', 5.5))))} or above. A clear mismatch on any criterion is a PASS whatever the score; "
                    "unreadable or missing values are marked Unknown, never as a mismatch.", st["small"]), Spacer(1, 5)]
    detailed = all("rule" in k for k in r["fit"]["checks"])  # results saved before this version lack the compared values
    if detailed:
        rows = [[Paragraph(h, st["th"]) for h in ("CRITERION", "COMPANY", "FUND REQUIRES", "RESULT")]]
        for k in r["fit"]["checks"]:
            src = f"<br/><font size='7' color='#71717A'>from {_t(k['source'])}</font>" if k.get("source") else ""
            rows.append([Paragraph(_t(k["name"]), st["tdb"]), Paragraph(f"{_t(k.get('value', ''))}{src}", st["td"]), Paragraph(_t(k.get("rule", "")), st["td"]),
                         Paragraph(f"{_pill_html(STATE_WORD[k['state']], STATE_TONE[k['state']])}<br/><font size='7.4' color='#52525B'>{_t(k['detail'])}</font>", st["td"])])
        S += [_table(rows, [26 * mm, 40 * mm, 42 * mm, W - 108 * mm])]
    else:
        rows = [[Paragraph(h, st["th"]) for h in ("CRITERION", "RESULT", "DETAIL")]]
        for k in r["fit"]["checks"]:
            rows.append([Paragraph(_t(k["name"]), st["tdb"]), _pill(STATE_WORD[k["state"]], STATE_TONE[k["state"]], st), Paragraph(_t(k["detail"]), st["td"])])
        S += [_table(rows, [30 * mm, 30 * mm, W - 60 * mm])]

    # ---- 6. profile ----
    S += _section(6, "Company profile (as stated by the company)", st)
    ps = r.get("profile_sources", {})
    kv = [("One-liner", "one_liner", p.get("one_liner")), ("Founders", "founders", "<br/>".join(_t(f"{f['name']}, {f['role']}. {f['background']}".strip(" .,")) for f in p.get("founders", []))),
          ("Headquarters", "hq_country", _t(p.get("hq_country"))), ("Founded", "founded_year", _t(p.get("founded_year"))), ("Team size", "team_size", _t(p.get("team_size"))),
          ("Stage", "stage", _t(p.get("stage"))), ("Sectors", "sectors", _t(", ".join(p.get("sectors", [])))), ("Business model", "business_model", _t(p.get("business_model"))),
          ("Market", "market", _t(p.get("market"))), ("Traction", "traction", "<br/>".join(_t(x) for x in p.get("traction", []))), ("Customers", "customers", _t(p.get("customers"))),
          ("Raising", "ask_text", _t(p.get("ask_text") or (f"EUR {p['ask_eur']:,.0f}" if p.get("ask_eur") else "")))]
    rows = [[Paragraph(h, st["th"]) for h in ("ITEM", "AS STATED", "SOURCE")]]
    for label, key, val in kv:
        if val:
            rows.append([Paragraph(label, st["tdb"]), Paragraph(val if key in ("founders", "traction", "hq_country", "founded_year", "team_size", "stage", "sectors", "business_model", "market", "customers", "ask_text") else _t(val), st["td"]), Paragraph(_t(ps.get(key, "")), st["small"])])
    S += [_table(rows, [30 * mm, W - 30 * mm - 24 * mm, 24 * mm]), Paragraph("Not independently verified unless the matching claim in section 3 says so.", st["tiny"])]

    # ---- 7. sources ----
    S += _section(7, "Sources", st)
    S += [Paragraph("Website", st["h2"]),
          Paragraph(f"{_link(cw['start_url'])}. {cw['pages_crawled']} pages read, {len(cw['pages_analysed'])} analysed in depth, {len(cw['errors'])} failed. "
                    f"Crawl stopped: {_t(cw['stopped_reason'])}.", st["small"])]
    S += [Paragraph(_link(pg["url"], pg["url"]) + f" <font color='#71717A'>{_t(pg['title'])}</font>", st["tiny"]) for pg in cw["pages_analysed"]]
    S += [CondPageBreak(40 * mm), Paragraph(f"Web search ({_t(sr.get('provider'))})", st["h2"]),
          Paragraph(f"{len(sr['hits'])} relevant results; {sr.get('irrelevant', 0)} off-topic results dropped." + ("" if sr["available"] else " <b>Search was unavailable.</b>"), st["small"])]
    if sr["hits"]:
        rows = [[Paragraph(h, st["th"]) for h in ("ID", "TYPE", "RESULT")]]
        for h in sorted(sr["hits"], key=lambda h: {"independent": 0, "profile": 1, "company": 2}[h["kind"]]):
            rows.append([Paragraph(_t(h["id"]), st["tiny"]), Paragraph(_t(KIND_WORD[h["kind"]]), st["small"]), Paragraph(f"{_link(h['url'], h['title'] or h['url'])}<br/><font size='7' color='#71717A'>{_t(h['snippet'][:170])}</font>", st["td"])])
        S += [_table(rows, [11 * mm, 25 * mm, W - 36 * mm], zebra=True)]

    # ---- 8. data quality ----
    S += _section(8, "Data quality and limitations", st)
    if r["issues"]:
        rows = [[Paragraph(h, st["th"]) for h in ("LEVEL", "STAGE", "DETAIL")]]
        for i in r["issues"]:
            rows.append([_pill(i["severity"].capitalize(), {"error": "bad", "warning": "warn"}.get(i["severity"], "info"), st), Paragraph(_t(i["stage"].capitalize()), st["small"]), Paragraph(_t(i["message"]), st["td"])])
        S += [_table(rows, [20 * mm, 24 * mm, W - 44 * mm])]
    else:
        S += [Paragraph("No technical or workflow issues were recorded during this analysis.", st["body"])]
    S += [Spacer(1, 6), Paragraph("Confidence: " + _t("; ".join(cf["reasons"])) + ".", st["small"])]

    # ---- method note (before the questions, so the last page is never just a disclaimer) ----
    S += [Spacer(1, 10), Table([[Paragraph(
        "<b>Method and disclaimer.</b> This memorandum was produced by an automated screening tool. An AI model extracted claims and scored each "
        "dimension; code then applied the fund's weights adjusted for stage and business model, deducted points for contradicted claims, "
        "assessed confidence, checked the mandate and set the recommendation. Evidence rules are enforced in code: only an independent source "
        "can verify a claim. Verification depends on what is publicly findable; absence of evidence is not evidence of inaccuracy. The "
        f"{_t(r['model'])} model made {r['llm_calls']} calls; the analysis took {_fmt(r['seconds'], 0)} seconds. This document supports, and does not replace, human "
        "judgement and is not investment advice.", st["small"])]], colWidths=[W],
        style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), WASH), ("BOX", (0, 0), (-1, -1), 0.4, LINE), ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                          ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))]
    # ---- 9. questions ----
    S += _section(9, "Questions for the founders and missing information", st)
    S += [KeepTogether([Paragraph("Questions for the next conversation", st["h2"]), *_bullets(r["questions_for_founder"], st)]),
          KeepTogether([Paragraph("Information that was missing", st["h2"]), *_bullets(r["missing_critical_data"], st)])]

    return S


def hostname(url: str) -> str:
    m = re.match(r"^https?://([^/]+)", url or "")
    return m.group(1).removeprefix("www.") if m else (url or "")
