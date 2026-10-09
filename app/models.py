from datetime import datetime

from pydantic import BaseModel, Field

# Flags added to Lead.flags
DOORS_AMBIGUOUS = "doors_ambiguous"  # parse: several numbers, can't tell which is doors
DOORS_UNKNOWN = "doors_unknown"  # rules: no door count at all
NO_VALID_CONTACT = "no_valid_contact"  # rules: no valid email and no valid phone


class Lead(BaseModel):
    leadgen_id: str  # "missing-<uuid>" when the payload has none
    created_time: str | None = None  # kept raw; Facebook uses "+0000" offsets
    received_at: datetime

    name: str | None = None
    email: str | None = None
    phone: str | None = None
    company: str | None = None
    doors: int | None = None  # parsed from doors_raw; None if missing, unparseable or ambiguous
    doors_raw: str | None = None
    message: str | None = None

    # field_data entries we don't map to a named field, kept so nothing is lost
    extra: dict[str, str] = Field(default_factory=dict)
    flags: list[str] = Field(default_factory=list)  # e.g. "doors_ambiguous"


class Qualification(BaseModel):
    fit_score: int = Field(ge=1, le=5)
    reason: str = Field(max_length=140)
    reply: str = Field(max_length=300)  # the model's text; the sign-off is added after (<= 320 total)
    pain_points: list[str] = Field(default_factory=list)


class LLMResult(BaseModel):
    """What llm.qualify returns: a validated Qualification, or the fallback."""

    fit_score: int | None  # None when the LLM was unavailable
    reason: str
    reply: str
    pain_points: list[str] = Field(default_factory=list)
    fallback: bool = False
    em_dashes_stripped: int = 0  # em dashes removed from the model's reply
