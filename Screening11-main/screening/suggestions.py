"""Suggestion lists for the thesis editor - and the single source of truth for stage names, so what you can pick in the
form is exactly what the AI is told to extract (a fund set to "series-b" must be able to match a Series B deal)."""
from __future__ import annotations

import re

COUNTRIES = [
    "Afghanistan", "Albania", "Algeria", "Andorra", "Angola", "Antigua and Barbuda", "Argentina", "Armenia", "Australia",
    "Austria", "Azerbaijan", "Bahamas", "Bahrain", "Bangladesh", "Barbados", "Belarus", "Belgium", "Belize", "Benin",
    "Bhutan", "Bolivia", "Bosnia and Herzegovina", "Botswana", "Brazil", "Brunei", "Bulgaria", "Burkina Faso", "Burundi",
    "Cambodia", "Cameroon", "Canada", "Cape Verde", "Central African Republic", "Chad", "Chile", "China", "Colombia",
    "Comoros", "Congo", "Costa Rica", "Croatia", "Cuba", "Cyprus", "Czech Republic", "Democratic Republic of the Congo",
    "Denmark", "Djibouti", "Dominica", "Dominican Republic", "Ecuador", "Egypt", "El Salvador", "Equatorial Guinea",
    "Eritrea", "Estonia", "Eswatini", "Ethiopia", "Fiji", "Finland", "France", "Gabon", "Gambia", "Georgia", "Germany",
    "Ghana", "Greece", "Grenada", "Guatemala", "Guinea", "Guinea-Bissau", "Guyana", "Haiti", "Honduras", "Hong Kong",
    "Hungary", "Iceland", "India", "Indonesia", "Iran", "Iraq", "Ireland", "Israel", "Italy", "Ivory Coast", "Jamaica",
    "Japan", "Jordan", "Kazakhstan", "Kenya", "Kiribati", "Kosovo", "Kuwait", "Kyrgyzstan", "Laos", "Latvia", "Lebanon",
    "Lesotho", "Liberia", "Libya", "Liechtenstein", "Lithuania", "Luxembourg", "Madagascar", "Malawi", "Malaysia",
    "Maldives", "Mali", "Malta", "Marshall Islands", "Mauritania", "Mauritius", "Mexico", "Micronesia", "Moldova", "Monaco",
    "Mongolia", "Montenegro", "Morocco", "Mozambique", "Myanmar", "Namibia", "Nauru", "Nepal", "Netherlands",
    "New Zealand", "Nicaragua", "Niger", "Nigeria", "North Korea", "North Macedonia", "Norway", "Oman", "Pakistan",
    "Palau", "Palestine", "Panama", "Papua New Guinea", "Paraguay", "Peru", "Philippines", "Poland", "Portugal", "Qatar",
    "Romania", "Russia", "Rwanda", "Saint Kitts and Nevis", "Saint Lucia", "Saint Vincent and the Grenadines", "Samoa",
    "San Marino", "Sao Tome and Principe", "Saudi Arabia", "Senegal", "Serbia", "Seychelles", "Sierra Leone",
    "Singapore", "Slovakia", "Slovenia", "Solomon Islands", "Somalia", "South Africa", "South Korea", "South Sudan",
    "Spain", "Sri Lanka", "Sudan", "Suriname", "Sweden", "Switzerland", "Syria", "Taiwan", "Tajikistan", "Tanzania",
    "Thailand", "Timor-Leste", "Togo", "Tonga", "Trinidad and Tobago", "Tunisia", "Turkey", "Turkmenistan", "Tuvalu",
    "Uganda", "Ukraine", "United Arab Emirates", "United Kingdom", "United States", "Uruguay", "Uzbekistan", "Vanuatu",
    "Vatican City", "Venezuela", "Vietnam", "Yemen", "Zambia", "Zimbabwe",
]

# What people (and the AI) write -> the name used in COUNTRIES
COUNTRY_ALIASES = {
    "usa": "United States", "us": "United States", "u.s.": "United States", "u.s.a.": "United States",
    "united states of america": "United States", "america": "United States",
    "uk": "United Kingdom", "u.k.": "United Kingdom", "great britain": "United Kingdom", "britain": "United Kingdom",
    "england": "United Kingdom", "scotland": "United Kingdom", "wales": "United Kingdom", "northern ireland": "United Kingdom",
    "uae": "United Arab Emirates", "czechia": "Czech Republic", "korea": "South Korea", "republic of korea": "South Korea",
    "holland": "Netherlands", "the netherlands": "Netherlands", "turkiye": "Turkey", "türkiye": "Turkey",
    "cote d'ivoire": "Ivory Coast", "côte d'ivoire": "Ivory Coast", "macedonia": "North Macedonia",
    "russian federation": "Russia", "viet nam": "Vietnam", "drc": "Democratic Republic of the Congo",
}

REGIONS = {
    "CEE": ["Poland", "Lithuania", "Latvia", "Estonia", "Czech Republic", "Slovakia", "Hungary", "Romania", "Bulgaria",
            "Slovenia", "Croatia", "Serbia", "Ukraine", "North Macedonia", "Bosnia and Herzegovina", "Kosovo", "Albania",
            "Montenegro", "Moldova"],
    "Nordics": ["Denmark", "Finland", "Iceland", "Norway", "Sweden"],
    "DACH": ["Germany", "Austria", "Switzerland"],
    "UK & Ireland": ["United Kingdom", "Ireland"],
    "Western Europe": ["France", "Belgium", "Netherlands", "Luxembourg", "Germany", "Austria", "Switzerland",
                       "United Kingdom", "Ireland"],
    "Southern Europe": ["Spain", "Portugal", "Italy", "Greece", "Malta", "Cyprus"],
    "North America": ["United States", "Canada"],
    "Latin America": ["Mexico", "Brazil", "Argentina", "Chile", "Colombia", "Peru", "Uruguay", "Costa Rica"],
    "MENA": ["United Arab Emirates", "Saudi Arabia", "Israel", "Egypt", "Morocco", "Qatar", "Jordan", "Bahrain",
             "Tunisia", "Turkey"],
    "Africa": ["Nigeria", "Kenya", "South Africa", "Egypt", "Ghana", "Rwanda", "Morocco", "Senegal", "Ethiopia"],
    "South Asia": ["India", "Pakistan", "Bangladesh", "Sri Lanka", "Nepal"],
    "Southeast Asia": ["Singapore", "Indonesia", "Vietnam", "Thailand", "Malaysia", "Philippines"],
    "East Asia": ["Japan", "South Korea", "China", "Taiwan", "Hong Kong"],
    "Oceania": ["Australia", "New Zealand"],
}

# slug, label, what it means. The AI is told to answer with one of these slugs.
STAGES = [
    ("pre-seed", "Pre-seed", "idea or MVP, first outside money"),
    ("seed", "Seed", "early product, first customers"),
    ("seed-extension", "Seed extension", "extra seed money before Series A"),
    ("late-seed", "Late seed", "strong seed, close to Series A metrics"),
    ("series-a", "Series A", "product-market fit, scaling go-to-market"),
    ("series-b", "Series B", "proven model, scaling fast"),
    ("series-c", "Series C", "expanding markets or products"),
    ("series-d", "Series D+", "later private rounds"),
    ("growth", "Growth / late stage", "mature private company"),
    ("pre-ipo", "Pre-IPO", "preparing to go public"),
    ("public", "Public / mature company", "listed, or very mature"),
]
STAGE_SLUGS = [s for s, _, _ in STAGES]

SECTORS = [
    "ai", "machine learning", "generative ai", "vertical ai", "ai agents", "developer tools", "infrastructure",
    "cloud", "devops", "cybersecurity", "data", "analytics", "databases", "open source", "api", "no-code",
    "saas", "vertical saas", "enterprise software", "b2b", "b2c", "b2b2c", "workflow automation", "productivity",
    "fintech", "payments", "banking", "lending", "insurtech", "wealthtech", "regtech", "accounting",
    "healthcare", "healthtech", "digital health", "medtech", "biotech", "mental health", "femtech",
    "climate tech", "energy", "cleantech", "sustainability", "agtech", "foodtech",
    "mobility", "automotive", "logistics", "supply chain", "manufacturing", "industrial", "construction tech",
    "proptech", "real estate", "e-commerce", "retail", "marketplace", "consumer", "consumer social", "creator economy",
    "gaming", "media", "entertainment", "adtech", "martech", "sales tech", "hr tech", "future of work", "legal tech",
    "govtech", "edtech", "education", "travel", "hospitality", "sports tech", "robotics", "hardware", "semiconductors",
    "iot", "ar/vr", "space", "defense", "deep tech", "quantum", "web3", "crypto", "blockchain",
    "telecom", "services", "consulting", "agency",
]
POPULAR_SECTORS = ["ai", "saas", "fintech", "developer tools", "healthtech", "cybersecurity", "climate tech",
                   "marketplace", "enterprise software", "data", "e-commerce", "edtech"]
POPULAR_WEAK = ["crypto", "consumer social", "services", "consulting", "agency", "hardware", "gaming", "real estate"]

TICKET_PRESETS = [
    {"label": "Pre-seed", "min": 150_000, "max": 1_000_000},
    {"label": "Seed", "min": 500_000, "max": 4_000_000},
    {"label": "Series A", "min": 3_000_000, "max": 15_000_000},
    {"label": "Series B", "min": 10_000_000, "max": 40_000_000},
    {"label": "Growth", "min": 25_000_000, "max": 150_000_000},
]

THRESHOLD_PRESETS = [
    {"label": "Strict", "advance_at": 8.0, "maybe_at": 6.5, "hint": "only clear winners advance"},
    {"label": "Balanced", "advance_at": 7.0, "maybe_at": 5.5, "hint": "default"},
    {"label": "Lenient", "advance_at": 6.0, "maybe_at": 4.5, "hint": "see more deals"},
]

DEFAULT_WEIGHTS = {"timing": 1.2, "problem": 1.0, "wedge": 1.0, "founder_market_fit": 1.3, "product_love": 1.0,
                   "execution_speed": 1.0, "market": 1.0, "moat_path": 1.0, "traction": 1.5, "business_model": 1.0,
                   "distribution": 1.5}


def _w(**over: float) -> dict[str, float]:
    return {k: over.get(k, 1.0) for k in DEFAULT_WEIGHTS}


WEIGHT_PRESETS = [
    {"label": "Default", "hint": "traction and distribution count most", "weights": DEFAULT_WEIGHTS},
    {"label": "Equal", "hint": "every dimension counts the same", "weights": _w()},
    {"label": "Team-first", "hint": "for very early bets on people",
     "weights": _w(founder_market_fit=2.0, execution_speed=1.5, product_love=1.2, problem=1.2)},
    {"label": "Traction-first", "hint": "for Series A and later",
     "weights": _w(traction=2.0, distribution=1.8, business_model=1.5, product_love=1.2)},
    {"label": "Market-first", "hint": "big markets and timing above all",
     "weights": _w(market=2.0, timing=1.5, moat_path=1.5, wedge=1.2)},
]

WESTERN_EUROPE = sorted(set(REGIONS["Western Europe"] + REGIONS["Nordics"] + REGIONS["Southern Europe"]))

TEMPLATES = [
    {"label": "CEE seed fund", "data": {
        "fund_name": "CEE Seed Fund", "geography": REGIONS["CEE"], "stages_core": ["pre-seed", "seed", "seed-extension"],
        "stages_check": ["late-seed", "series-a"],
        "sectors_strong": ["developer tools", "infrastructure", "ai", "data", "healthcare", "saas", "enterprise software",
                           "fintech", "workflow automation", "marketplace"],
        "sectors_weak": ["crypto", "consumer social", "education", "consulting", "agency", "services"],
        "ticket_min_eur": 500_000, "ticket_max_eur": 4_000_000, "advance_at": 7.0, "maybe_at": 5.5, "weights": DEFAULT_WEIGHTS}},
    {"label": "US seed fund", "data": {
        "fund_name": "US Seed Fund", "geography": REGIONS["North America"], "stages_core": ["pre-seed", "seed"],
        "stages_check": ["seed-extension", "series-a"],
        "sectors_strong": ["ai", "saas", "developer tools", "fintech", "healthtech", "infrastructure", "marketplace", "consumer"],
        "sectors_weak": ["crypto", "services", "consulting", "agency", "hardware"],
        "ticket_min_eur": 250_000, "ticket_max_eur": 3_000_000, "advance_at": 7.0, "maybe_at": 5.5,
        "weights": WEIGHT_PRESETS[2]["weights"]}},
    {"label": "European Series A", "data": {
        "fund_name": "European Series A Fund", "geography": WESTERN_EUROPE + ["Poland", "Estonia", "Czech Republic"],
        "stages_core": ["series-a"], "stages_check": ["late-seed", "seed-extension", "series-b"],
        "sectors_strong": ["saas", "fintech", "ai", "enterprise software", "healthtech", "climate tech", "marketplace", "cybersecurity"],
        "sectors_weak": ["crypto", "consumer social", "services", "real estate"],
        "ticket_min_eur": 3_000_000, "ticket_max_eur": 15_000_000, "advance_at": 7.0, "maybe_at": 5.5,
        "weights": WEIGHT_PRESETS[3]["weights"]}},
    {"label": "Global growth fund", "data": {
        "fund_name": "Global Growth Fund", "geography": COUNTRIES,
        "stages_core": ["series-b", "series-c", "series-d", "growth"], "stages_check": ["series-a", "pre-ipo"],
        "sectors_strong": ["saas", "fintech", "ai", "enterprise software", "healthtech", "e-commerce", "marketplace",
                           "cybersecurity", "logistics", "climate tech"],
        "sectors_weak": ["crypto", "consumer social", "services", "consulting"],
        "ticket_min_eur": 25_000_000, "ticket_max_eur": 150_000_000, "advance_at": 7.5, "maybe_at": 6.0,
        "weights": WEIGHT_PRESETS[3]["weights"]}},
]


BLANK_THESIS = {"fund_name": "", "geography": [], "stages_core": [], "stages_check": [], "sectors_strong": [], "sectors_weak": [],
                "sectors_excluded": [], "ticket_min_eur": 0, "ticket_max_eur": 0, "advance_at": 7.0, "maybe_at": 5.5,
                "weights": DEFAULT_WEIGHTS}


def normalize_country(name: str) -> str:
    """'USA' -> 'United States', 'czechia' -> 'Czech Republic', 'poland' -> 'Poland'; unknown names pass through."""
    n = (name or "").strip()
    low = n.lower()
    if low in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[low]
    return next((c for c in COUNTRIES if c.lower() == low), n)


def normalize_stage(stage: str) -> str:
    s = (stage or "").strip().lower().replace(" ", "-").replace("_", "-")
    return {"series-d+": "series-d", "series-e": "series-d", "series-f": "series-d", "late-stage": "growth",
            "ipo": "pre-ipo", "listed": "public", "mature": "public", "later": "growth"}.get(s, s)



# ---------- reading free text the way a person would (used by the mandate check) ----------
_ALIAS_OK = {a: c for a, c in COUNTRY_ALIASES.items() if len(a) >= 3 or a == "uk"}  # "us" would match the word "us"
_COUNTRY_RE = re.compile(r"(?<![\w-])(" + "|".join(re.escape(n.lower()) for n in sorted(set(COUNTRIES) | set(_ALIAS_OK), key=len, reverse=True))
                         + r")(?![\w-])")
_CANON = {c.lower(): c for c in COUNTRIES} | {a: c for a, c in _ALIAS_OK.items()}


def find_countries(text: str) -> list[str]:
    """Countries named in free text, in order: 'Warsaw, Poland' -> ['Poland']; 'Silicon Valley' -> []."""
    t = re.sub(r"new mexico", " ", (text or "").lower())  # a US state, not the country
    out: list[str] = []
    for m in _COUNTRY_RE.finditer(t):
        c = _CANON[m.group(1)]
        if c not in out:
            out.append(c)
    return out


_STAGE_PATTERNS = [  # longest / most specific first, so "pre-seed" is not also read as "seed"
    ("seed-extension", r"seed[\s-]*(?:extension|ext\.?|bridge)|extended[\s-]*seed"), ("late-seed", r"late[\s-]*seed"),
    ("pre-seed", r"pre[\s-]*seed|angel|incubat\w*|accelerator"), ("pre-ipo", r"pre[\s-]*ipo"),
    ("series-d", r"series[\s-]*[d-z]\b(?:\+)?|round[\s-]*[d-z]\b|late[\s-]*stage"), ("series-c", r"series[\s-]*c\b"),
    ("series-b", r"series[\s-]*b\b"), ("series-a", r"series[\s-]*a\b"), ("seed", r"seed"),
    ("growth", r"growth|expansion|scale[\s-]*up|mature"), ("public", r"\bpublic\b|\bipo\b|listed"),
]


def parse_stages(text: str) -> list[str]:
    """Stage slugs named in free text: 'Seed Round' -> ['seed']; 'seed / series a' -> ['seed','series-a']; 'bootstrapped' -> []."""
    t = (text or "").lower()
    found: list[tuple[int, str]] = []
    for slug, pat in _STAGE_PATTERNS:
        for m in re.finditer(pat, t):
            found.append((m.start(), slug))
            t = t[:m.start()] + " " * (m.end() - m.start()) + t[m.end():]  # blank it so shorter patterns can't re-match
    return [slug for _, slug in sorted(found)]


_SECTOR_SYNONYMS = [("artificial intelligence", "ai"), ("a i", "ai"), ("software as a service", "saas"), ("financial technology", "fintech"),
                    ("health tech", "healthtech"), ("digital health", "healthtech"), ("developer tooling", "developer tools"),
                    ("devtools", "developer tools"), ("dev tools", "developer tools"), ("ecommerce", "e commerce"),
                    ("climatetech", "climate tech"), ("cyber security", "cybersecurity"), ("proptech", "real estate")]


def norm_terms(text: str) -> str:
    """Lowercase, punctuation to spaces, common synonyms unified - applied to BOTH sides of a sector comparison."""
    t = " " + re.sub(r"[\s\-_/&,;:.|()]+", " ", (text or "").lower()) + " "
    for a, b in _SECTOR_SYNONYMS:
        t = t.replace(f" {a} ", f" {b} ")
    return t


def term_hits(text: str, terms: list[str]) -> list[str]:
    """Which of `terms` appear in `text` as whole words/phrases (after normalising both)."""
    t = norm_terms(text)
    return [x for x in terms if f" {norm_terms(x).strip()} " in t and norm_terms(x).strip()]


def all_suggestions() -> dict:
    return {
        "countries": COUNTRIES, "country_aliases": COUNTRY_ALIASES, "regions": REGIONS,
        "popular_countries": ["United States", "United Kingdom", "Germany", "France", "India", "Israel", "Canada",
                              "Netherlands", "Sweden", "Singapore", "Spain", "Poland"],
        "stages": [{"value": s, "label": label, "hint": hint} for s, label, hint in STAGES],
        "sectors": SECTORS, "popular_sectors": POPULAR_SECTORS, "popular_weak": POPULAR_WEAK, "popular_excluded": ["gambling", "weapons", "tobacco", "adult entertainment", "cannabis", "fossil fuels", "crypto"],
        "ticket_presets": TICKET_PRESETS, "threshold_presets": THRESHOLD_PRESETS,
        "weight_presets": WEIGHT_PRESETS, "templates": TEMPLATES, "blank": BLANK_THESIS,
    }
