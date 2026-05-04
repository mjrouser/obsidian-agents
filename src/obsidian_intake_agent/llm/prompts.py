from __future__ import annotations

STRICT_JSON_INSTRUCTION = (
    "Return STRICT JSON only. Do not output markdown. Do not wrap the JSON in code fences. "
    "Do not add explanation or extra text."
)


def build_meeting_extraction_prompt(
    transcript_text: str,
    meeting_date: str,
    source: str,
    title: str,
) -> str:
    return f"""You are extracting structured meeting data from a transcript.

{STRICT_JSON_INSTRUCTION}

Use exactly this schema:
{{
  "title": str,
  "date": "YYYY-MM-DD",
  "source": "Teams"|"Copilot"|"Unknown",
  "context": str,
  "participants": [str],
  "attendance_confidence": "confirmed"|"calendar_invite_only"|"partial_visibility"|"unknown",
  "summary_bullets": [str],
  "key_points": [str],
  "decisions": [str],
  "decision_signals": [str],
  "risks": [str],
  "assumptions": [str],
  "open_questions": [str],
  "action_items": [{{"text": str, "owner": str|null, "due": str|null}}],
  "signals_and_tensions": [str],
  "alignment_path": [str],
  "related_initiatives": [str],
  "related_themes": [str],
  "sources_used": [str],
  "source_limitations": [str],
  "verbatim_excerpt": str
}}

Rules:
- Do NOT invent facts; if unknown use [] or null and add the uncertainty to open_questions.
- Use the full available transcript before writing. Preserve nuance and named attribution when a person's
  viewpoint, concern, decision, or action materially matters.
- Distinguish explicit decisions from directional alignment, preferences, unresolved discussion, or inferred signals.
- participants: include only people with transcript evidence of participation. If participant visibility is partial,
  say so in source_limitations.
- attendance_confidence: use "confirmed" only when the transcript or meeting artifact shows participation.
  Use "calendar_invite_only" when attendance is known only from invite metadata, and include exactly
  "Known from calendar invite; attendance not guaranteed." in source_limitations.
- sources_used: name the concrete sources available in this extraction, such as "Teams transcript".
- source_limitations: state missing or partial access, such as missing chat, missing recap, partial transcript,
  or unconfirmed attendance. Use [] only when there are no known limitations.
- action_items: only include if explicitly stated or strongly implied; otherwise [].
- Keep summary_bullets to 3-6 items.
- Output JSON only, no extra text.

Known meeting metadata:
- title: {title}
- date: {meeting_date}
- source: {source}

===TRANSCRIPT===
{transcript_text}
"""
