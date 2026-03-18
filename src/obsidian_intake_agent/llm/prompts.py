from __future__ import annotations


def build_meeting_extraction_prompt(
    transcript_text: str,
    meeting_date: str,
    source: str,
    title: str,
) -> str:
    return f"""You are extracting structured meeting data from a transcript.

Return STRICT JSON only. Do not output markdown. Do not wrap the JSON in code fences. Do not add explanation or extra text.

Use exactly this schema:
{{
  "title": str,
  "date": "YYYY-MM-DD",
  "source": "Teams"|"Copilot"|"Unknown",
  "participants": [str],
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
  "verbatim_excerpt": str
}}

Rules:
- Do NOT invent facts; if unknown use [] or null and add the uncertainty to open_questions.
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
