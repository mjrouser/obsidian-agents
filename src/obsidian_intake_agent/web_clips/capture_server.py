from __future__ import annotations

import json
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from obsidian_intake_agent.rendering.web_clip_renderer import render_raw_web_clip_note
from obsidian_intake_agent.utils.fs import safe_write_text
from obsidian_intake_agent.web_clips.models import WebClipCapture, WebClipPassage

_UNSAFE_FILENAME_CHARS = re.compile(r'[\\:*?"<>|]+')
_WHITESPACE = re.compile(r"\s+")
MAX_REQUEST_BYTES = 256_000
MAX_FIELD_CHARS = 10_000
MAX_PASSAGES = 25
MAX_FILENAME_TITLE_CHARS = 120
TOKEN_HEADER = "X-Obsidian-Web-Clipper-Token"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def sanitize_clip_filename(title: str, captured_at: datetime) -> str:
    safe_title = title.strip().replace("/", "-")
    safe_title = _UNSAFE_FILENAME_CHARS.sub("", safe_title)
    safe_title = _WHITESPACE.sub(" ", safe_title).strip(" .-")
    if not safe_title:
        safe_title = "Untitled Web Clip"
    safe_title = safe_title[:MAX_FILENAME_TITLE_CHARS].rstrip(" .-")
    return f"{captured_at.date().isoformat()} - {safe_title}.md"


def capture_payload_to_note(payload: dict[str, Any], *, intake_dir: Path) -> Path:
    capture = _capture_from_payload(payload)
    content = render_raw_web_clip_note(capture)
    note_path = _unique_note_path(intake_dir / sanitize_clip_filename(capture.source_title, capture.captured_at))
    safe_write_text(note_path, content)
    return note_path


def is_valid_capture_token(headers: Any, token: str) -> bool:
    _validate_token(token)
    return headers.get(TOKEN_HEADER) == token


def parse_content_length(value: str | None) -> int:
    if value is None or not value.isascii() or not value.isdigit():
        raise ValueError("Content-Length must be ASCII digits.")
    content_length = int(value)
    if content_length <= 0:
        raise ValueError("Content-Length must be positive.")
    if content_length > MAX_REQUEST_BYTES:
        raise ValueError("request is too large.")
    return content_length


def validate_loopback_host(host: str) -> None:
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("web clip capture host must be loopback-only.")


def run_capture_server(*, host: str, port: int, intake_dir: Path, token: str) -> None:
    validate_loopback_host(host)
    _validate_token(token)

    class CaptureRequestHandler(BaseHTTPRequestHandler):
        def do_OPTIONS(self) -> None:
            self._send_empty(204)

        def do_POST(self) -> None:
            if self.path != "/capture":
                self._send_json(404, {"error": "not found"})
                return

            if not is_valid_capture_token(self.headers, token):
                self._send_json(403, {"error": "invalid token"})
                return
            try:
                length = parse_content_length(self.headers.get("Content-Length"))
            except ValueError as exc:
                status = 413 if str(exc) == "request is too large." else 400
                self._send_json(status, {"error": str(exc)})
                return

            try:
                raw_body = self.rfile.read(length).decode("utf-8")
                payload = json.loads(raw_body)
                if not isinstance(payload, dict):
                    raise ValueError("payload must be an object.")
                written = capture_payload_to_note(payload, intake_dir=intake_dir)
            except (json.JSONDecodeError, ValueError) as exc:
                self._send_json(400, {"error": str(exc)})
                return

            self._send_json(201, {"path": str(written)})

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_empty(self, status: int) -> None:
            self.send_response(status)
            self._send_common_headers()
            self.end_headers()

        def _send_json(self, status: int, body: dict[str, Any]) -> None:
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self._send_common_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_common_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", f"Content-Type, {TOKEN_HEADER}")

    HTTPServer((host, port), CaptureRequestHandler).serve_forever()


def _validate_token(token: str) -> None:
    if not token:
        raise ValueError("token is required.")


def _capture_from_payload(payload: dict[str, Any]) -> WebClipCapture:
    passages = payload.get("passages")
    if not isinstance(passages, list):
        raise ValueError("passages must be a list.")
    if len(passages) > MAX_PASSAGES:
        raise ValueError("too many passages.")

    return WebClipCapture(
        source_url=_string_field(payload, "source_url"),
        source_title=_string_field(payload, "source_title"),
        captured_at=_datetime_field(payload, "captured_at"),
        why=_string_field(payload, "why"),
        passages=[WebClipPassage(text=_passage_text(passage)) for passage in passages],
    )


def _string_field(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} is required.")
    if len(value) > MAX_FIELD_CHARS:
        raise ValueError(f"{key} is too large.")
    return value


def _passage_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("passages must be strings.")
    if len(value) > MAX_FIELD_CHARS:
        raise ValueError("passage is too large.")
    return value


def _datetime_field(payload: dict[str, Any], key: str) -> datetime:
    value = _string_field(payload, key)
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{key} must be an ISO 8601 datetime.") from exc


def _unique_note_path(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    counter = 2
    while True:
        candidate = path.with_name(f"{stem} {counter}{suffix}")
        if not candidate.exists():
            return candidate
        counter += 1
