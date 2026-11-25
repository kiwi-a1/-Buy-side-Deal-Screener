"""Fund theses: validation and cross-consistency checks. Storage lives in store.py (SQLite)."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from .scoring import DIMENSIONS
from .suggestions import COUNTRIES, DEFAULT_WEIGHTS, STAGE_SLUGS, normalize_country, normalize_stage

LEGACY_PATH = Path(__file__).resolve().parent.parent / "thesis.yaml"


class Thesis(BaseModel):
    fund_name: str
    geography: list[str] = Field(default_factory=list)
    stages_core: list[str] = Field(default_factory=list)
    stages_check: list[str] = Field(default_factory=list)
    sectors_strong: list[str] = Field(default_factory=list)
    sectors_weak: list[str] = Field(default_factory=list)
    sectors_excluded: list[str] = Field(default_factory=list)
    ticket_min_eur: int = 0
    ticket_max_eur: int = 0
    advance_at: float = 7.0
    maybe_at: float = 5.5
    weights: dict[str, float]

    @field_validator("fund_name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(str(v).split())
        if not v:
            raise ValueError("Give the fund thesis a name")
        return v[:80]

    @field_validator("geography", "stages_core", "stages_check", "sectors_strong", "sectors_weak", "sectors_excluded", mode="before")
    @classmethod
    def _clean_list(cls, v):
        if v is None:
            return []
        out, seen = [], set()
        for x in v if isinstance(v, list) else [v]:
            x = " ".join(str(x).split())
            if x and x.lower() not in seen:
                seen.add(x.lower())
                out.append(x)
        return out

    @field_validator("ticket_min_eur", "ticket_max_eur", mode="before")
    @classmethod
    def _money(cls, v):
        try:
            return max(0, int(float(v or 0)))
        except (TypeError, ValueError):
            raise ValueError("round sizes must be numbers")

    @field_validator("advance_at", "maybe_at")
    @classmethod
    def _score(cls, v: float) -> float:
        if not 0 <= v <= 10:
            raise ValueError("thresholds must be between 0 and 10")
        return v

    @field_validator("weights")
    @classmethod
    def _all_dimensions(cls, w: dict[str, float]) -> dict[str, float]:
        missing = [d for d in DIMENSIONS if d not in w]
        if missing:
            raise ValueError(f"weights missing for: {', '.join(missing)}")
        if any(v < 0 for v in w.values()) or sum(w.values()) <= 0:
            raise ValueError("weights must be >= 0 and not all zero")
        return {d: float(w[d]) for d in DIMENSIONS}


class ThesisError(ValueError):
    pass


def parse_thesis(text: str) -> Thesis:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ThesisError(f"Not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ThesisError("A thesis must be a YAML mapping (key: value lines).")
    return build_thesis(data)


def build_thesis(data: dict) -> Thesis:
    try:
        return Thesis(**data)
    except (ValidationError, TypeError) as e:
        if isinstance(e, ValidationError):
            raise ThesisError("; ".join(f"{'.'.join(map(str, x['loc'])) or 'thesis'}: {x['msg'].removeprefix('Value error, ')}" for x in e.errors()[:5])) from e
        raise ThesisError(str(e)) from e


def check_thesis(t: Thesis) -> tuple[list[str], list[str]]:
    """Cross-check a thesis against itself. Errors block saving; warnings are shown but allowed."""
    errors, warnings = [], []
    core, check = [normalize_stage(s) for s in t.stages_core], [normalize_stage(s) for s in t.stages_check]
    if not core:
        errors.append("Choose at least one core stage - without it the fund would accept (or reject) every stage.")
    bad = [s for s in core + check if s not in STAGE_SLUGS]
    if bad:
        errors.append(f"Unknown stage(s): {', '.join(bad)}. Use the suggested stages so the AI's answer can be matched.")
    for a, b, an, bn in ((core, check, "core stages", "stages to double-check"),):
        both = sorted(set(a) & set(b))
        if both:
            errors.append(f"{', '.join(both)} is in both {an} and {bn}.")
    low = lambda xs: {x.lower() for x in xs}  # noqa: E731
    for (x, xn), (y, yn) in (((t.sectors_strong, "target sectors"), (t.sectors_weak, "risky sectors")),
                             ((t.sectors_strong, "target sectors"), (t.sectors_excluded, "excluded sectors")),
                             ((t.sectors_weak, "risky sectors"), (t.sectors_excluded, "excluded sectors"))):
        both = sorted(low(x) & low(y))
        if both:
            errors.append(f"'{both[0]}' is in both {xn} and {yn}.")
    if t.ticket_min_eur and t.ticket_max_eur and t.ticket_min_eur > t.ticket_max_eur:
        errors.append("The smallest round is larger than the largest ticket.")
    if t.maybe_at > t.advance_at:
        errors.append("The 'maybe' threshold must not be above the 'advance' threshold.")
    known = {c.lower() for c in COUNTRIES}
    odd = [g for g in t.geography if normalize_country(g).lower() not in known]
    if odd:
        warnings.append(f"Not recognised as countries: {', '.join(odd[:4])}. Company HQs are matched by country name.")
    if not t.geography:
        warnings.append("No countries chosen: any HQ location will be accepted.")
    if not t.sectors_strong:
        warnings.append("No target sectors chosen: every sector will be accepted.")
    if not (t.ticket_min_eur or t.ticket_max_eur):
        warnings.append("No round size limits: the round-size check will be skipped.")
    return errors, warnings


def thesis_summary(t: Thesis) -> dict:
    return {"countries": len(t.geography), "stages": t.stages_core, "sectors": len(t.sectors_strong),
            "ticket": [t.ticket_min_eur, t.ticket_max_eur], "advance_at": t.advance_at, "maybe_at": t.maybe_at}


OPEN_NAME = "Open screening (no fund thesis)"


def open_thesis() -> Thesis:
    """Used when no fund thesis is chosen: any country, stage and sector, no round limits, standard thresholds and default weights."""
    return Thesis(fund_name=OPEN_NAME, weights=dict(DEFAULT_WEIGHTS))
