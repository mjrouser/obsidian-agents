from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class WebClipPassage:
    text: str


@dataclass(frozen=True, slots=True)
class WebClipCapture:
    source_url: str
    source_title: str
    captured_at: datetime
    why: str
    passages: list[WebClipPassage]


@dataclass(frozen=True, slots=True)
class ProcessedWebClip:
    source_url: str
    source_title: str
    captured_at: str
    why: str
    summary: str
    passages: list[str]
    topics: list[str]
    application: str
    related: list[str]
    intake_source: str


def validate_capture(capture: WebClipCapture) -> None:
    if not capture.source_url.strip():
        raise ValueError("source_url is required.")
    if not capture.source_title.strip():
        raise ValueError("source_title is required.")
    if not capture.why.strip():
        raise ValueError("why is required.")
    if not capture.passages:
        raise ValueError("at least one passage is required.")
    for passage in capture.passages:
        if not passage.text.strip():
            raise ValueError("passages cannot be blank.")
