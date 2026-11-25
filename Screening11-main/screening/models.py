"""Shapes the AI must return. Validation is lenient about formatting (None, a string instead of a list) but strict
about meaning: unknown categories fall back to safe values and a dimension without a score is an error, so the
repair-or-next-model path runs instead of a silent default."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .scoring import DIMENSIONS

CLAIM_CATEGORIES = ["traction", "revenue", "customers", "team", "funding", "market", "product", "partnerships", "company", "other"]
CHECK_STATUSES = ["supported", "contradicted", "outdated", "unverified"]


def strs(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if x not in (None, "") and str(x).strip()]
    return [str(v)]


def text(v) -> str:
    return "" if v is None or isinstance(v, (dict, list)) else str(v).strip()


class Founder(BaseModel):
    name: str = ""
    role: str = ""
    background: str = ""

    @field_validator("name", "role", "background", mode="before")
    @classmethod
    def _t(cls, v):
        return text(v)


class Profile(BaseModel):
    company_name: str = ""
    one_liner: str = ""
    founders: list[Founder] = Field(default_factory=list)
    hq_country: str = ""
    founder_origin: str = ""
    founded_year: str = ""
    stage: str = ""
    business_model_type: str = "other"
    sectors: list[str] = Field(default_factory=list)
    business_model: str = ""
    market: str = ""
    traction: list[str] = Field(default_factory=list)
    customers: str = ""
    team_size: str = ""
    ask_eur: Optional[float] = None
    ask_text: str = ""

    @field_validator("sectors", "traction", mode="before")
    @classmethod
    def _l(cls, v):
        return strs(v)

    @field_validator("founders", mode="before")
    @classmethod
    def _f(cls, v):
        if not isinstance(v, list):
            return []
        out = [x if isinstance(x, dict) else {"name": str(x)} for x in v if x]
        return [f for f in out if text(f.get("name")) and text(f.get("name")).lower() not in ("unknown", "n/a", "not found")]

    @field_validator("ask_eur", mode="before")
    @classmethod
    def _n(cls, v):
        try:
            n = float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None
        return n if n and n > 0 else None

    @field_validator("company_name", "one_liner", "hq_country", "founder_origin", "founded_year", "stage", "business_model_type",
                     "business_model", "market", "customers", "team_size", "ask_text", mode="before")
    @classmethod
    def _t(cls, v):
        t = text(v)
        return "" if t.lower() in ("unknown", "n/a", "none", "not stated", "null") else t


class RawClaim(BaseModel):
    text: str
    category: str = "other"
    importance: str = "medium"
    quote: str = ""
    doc: str = ""   # which document / page the claim came from (D0, D1.. or P1, P2..)
    ref: str = ""   # slide / page number if known

    @field_validator("text")
    @classmethod
    def _nonempty(cls, v):
        if not v.strip():
            raise ValueError("claim text is empty")
        return v.strip()

    @field_validator("category", mode="before")
    @classmethod
    def _c(cls, v):
        v = text(v).lower()
        return v if v in CLAIM_CATEGORIES else "other"

    @field_validator("importance", mode="before")
    @classmethod
    def _i(cls, v):
        v = text(v).lower()
        return v if v in ("high", "medium", "low") else "medium"

    @field_validator("quote", "doc", "ref", mode="before")
    @classmethod
    def _t(cls, v):
        return text(v)


class Extraction(BaseModel):
    profile: Profile = Field(default_factory=Profile)
    claims: list[RawClaim] = Field(default_factory=list)

    @field_validator("claims", mode="before")
    @classmethod
    def _drop_bad(cls, v):  # one malformed claim should not throw away the others
        if not isinstance(v, list):
            return []
        return [c for c in v if isinstance(c, dict) and text(c.get("text"))]


class Check(BaseModel):
    claim_id: str
    status: str = "unverified"
    evidence_ids: list[str] = Field(default_factory=list)
    note: str = ""

    @field_validator("status", mode="before")
    @classmethod
    def _s(cls, v):
        v = text(v).lower()
        return v if v in CHECK_STATUSES else "unverified"

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _e(cls, v):
        return [x.upper() for x in strs(v)]

    @field_validator("note", mode="before")
    @classmethod
    def _t(cls, v):
        return text(v)


class Verification(BaseModel):
    checks: list[Check] = Field(default_factory=list)

    @field_validator("checks", mode="before")
    @classmethod
    def _drop_bad(cls, v):
        if not isinstance(v, list):
            return []
        return [c for c in v if isinstance(c, dict) and text(c.get("claim_id"))]


class Dim(BaseModel):
    score: int
    reasoning: str = ""
    claim_ids: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)

    @field_validator("score", mode="before")
    @classmethod
    def _score(cls, v):
        try:
            return max(1, min(10, round(float(v))))
        except (TypeError, ValueError):
            raise ValueError("score must be a number from 1 to 10")

    @field_validator("claim_ids", mode="before")
    @classmethod
    def _ids(cls, v):
        return [x.upper() for x in strs(v)]

    @field_validator("evidence", "missing", mode="before")
    @classmethod
    def _l(cls, v):
        return strs(v)[:3]

    @field_validator("reasoning", mode="before")
    @classmethod
    def _t(cls, v):
        return text(v)


class Assessment(BaseModel):
    dimensions: dict[str, Dim]
    summary: str = ""
    strengths: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    questions_for_founder: list[str] = Field(default_factory=list)
    missing_critical_data: list[str] = Field(default_factory=list)

    @field_validator("strengths", "concerns", "next_steps", mode="before")
    @classmethod
    def _three(cls, v):
        return strs(v)[:3]

    @field_validator("questions_for_founder", "missing_critical_data", mode="before")
    @classmethod
    def _five(cls, v):
        return strs(v)[:6]

    @field_validator("summary", mode="before")
    @classmethod
    def _t(cls, v):
        return text(v)

    @model_validator(mode="after")
    def _complete(self):
        missing = [k for k in DIMENSIONS if k not in self.dimensions]
        if missing:
            raise ValueError(f"missing dimensions: {', '.join(missing)}")
        self.dimensions = {k: self.dimensions[k] for k in DIMENSIONS}
        return self
