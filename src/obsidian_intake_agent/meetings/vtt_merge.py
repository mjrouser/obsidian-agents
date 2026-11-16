"""VTT and transcript-text conversion helpers for meeting sync.

Pure byte/text transforms used when downloading Teams transcripts: normalize
and merge WebVTT payloads, and render Graph metadata content as plain
speaker-prefixed transcript text. No I/O. Imported by ``meetings.sync``.
"""

from __future__ import annotations

import json

from .sync_models import _optional_string


def _normalized_vtt_bytes(content: bytes) -> bytes:
    return content if content.endswith(b"\n") else content + b"\n"


def _merged_vtt_bytes(contents: list[bytes]) -> bytes:
    bodies: list[bytes] = []
    for content in contents:
        normalized = content.removeprefix(b"\xef\xbb\xbf")
        lines = normalized.splitlines()
        signature_line = lines[0] if lines else b""
        has_webvtt_signature = signature_line == b"WEBVTT" or signature_line.startswith((b"WEBVTT ", b"WEBVTT\t"))
        if has_webvtt_signature and b"-->" not in signature_line:
            lines = lines[1:]
            while lines and lines[0].strip():
                lines = lines[1:]
            while lines and not lines[0].strip():
                lines = lines[1:]
        body = b"\n".join(lines).strip()
        if body:
            bodies.append(body)
    return b"WEBVTT\n\n" + b"\n\n".join(bodies) + b"\n"


def _transcript_markdown_from_metadata_content(content: bytes) -> str:
    lines: list[str] = []
    for raw_line in content.decode("utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped == "WEBVTT" or "-->" in stripped:
            continue
        if stripped.isdigit():
            continue
        speaker, spoken_text = _metadata_content_line_parts(stripped)
        if spoken_text is None:
            continue
        if speaker:
            lines.append(f"{speaker}: {spoken_text}")
        else:
            lines.append(spoken_text)
    rendered = "\n".join(lines).strip()
    return f"{rendered}\n" if rendered else ""


def _metadata_content_line_parts(line: str) -> tuple[str | None, str | None]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return _vtt_voice_line_parts(line)
    if not isinstance(payload, dict):
        return None, None
    spoken_text = _optional_string(str(payload.get("spokenText") or ""))
    if spoken_text is None:
        return None, None
    speaker_name = _optional_string(str(payload.get("speakerName") or ""))
    return speaker_name, spoken_text


def _vtt_voice_line_parts(line: str) -> tuple[str | None, str | None]:
    if line.startswith("<v ") and ">" in line and "</v>" in line:
        speaker_name = _optional_string(line[3 : line.index(">")].strip())
        spoken_text = _optional_string(line[line.index(">") + 1 : line.rindex("</v>")].strip())
        return speaker_name, spoken_text
    return None, _optional_string(line)
