from datetime import datetime

from pydantic import BaseModel, Field


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
    reply: str = Field(max_length=320)
    pain_points: list[str] = Field(default_factory=list)
