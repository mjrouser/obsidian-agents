from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.client import HTTPConnection
from pathlib import Path

from obsidian_intake_agent.processors.web_clip_processor import WebClipFileProcessingResult
from obsidian_intake_agent.web_clips.bookmarklet import render_bookmarklet
from obsidian_intake_agent.web_clips.capture_server import (
    MAX_FIELD_CHARS,
    MAX_PASSAGES,
    MAX_REQUEST_BYTES,
    capture_payload_to_note,
    create_capture_server,
    is_valid_capture_token,
    parse_content_length,
    run_capture_server,
    sanitize_clip_filename,
    validate_loopback_host,
)


class WebClipCaptureServerTests(unittest.TestCase):
    def test_sanitize_clip_filename_keeps_safe_title_and_date(self) -> None:
        captured_at = datetime(2026, 5, 18, 14, 30, tzinfo=timezone.utc)

        name = sanitize_clip_filename("A/B: Useful * Article?", captured_at)

        self.assertEqual(name, "2026-05-18 - A-B Useful Article.md")

    def test_capture_payload_to_note_writes_raw_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake" / "Web Clips"
            payload = {
                "source_url": "https://example.com/article",
                "source_title": "Article Title",
                "captured_at": "2026-05-18T14:30:00+00:00",
                "why": "Use this for CRM adoption messaging.",
                "passages": ["First exact passage.", "Second exact passage."],
            }

            written = capture_payload_to_note(payload, intake_dir=intake_dir, vault_path=vault)

            self.assertTrue(written.exists())
            text = written.read_text(encoding="utf-8")
            self.assertIn("type: web_clip_intake", text)
            self.assertIn("> First exact passage.", text)
            self.assertIn("> Second exact passage.", text)

    def test_capture_payload_rejects_blank_passages(self) -> None:
        payload = {
            "source_url": "https://example.com/article",
            "source_title": "Article Title",
            "captured_at": "2026-05-18T14:30:00+00:00",
            "why": "Use this.",
            "passages": [" "],
        }

        with self.assertRaisesRegex(ValueError, "passages cannot be blank"):
            capture_payload_to_note(payload, intake_dir=Path("/tmp/not-used"), vault_path=Path("/tmp"))

    def test_capture_payload_rejects_field_over_limit(self) -> None:
        payload = {
            "source_url": "https://example.com/article",
            "source_title": "Article Title",
            "captured_at": "2026-05-18T14:30:00+00:00",
            "why": "x" * (MAX_FIELD_CHARS + 1),
            "passages": ["Useful passage."],
        }

        with self.assertRaisesRegex(ValueError, "why is too large"):
            capture_payload_to_note(payload, intake_dir=Path("/tmp/not-used"), vault_path=Path("/tmp"))

    def test_capture_payload_rejects_too_many_passages(self) -> None:
        payload = {
            "source_url": "https://example.com/article",
            "source_title": "Article Title",
            "captured_at": "2026-05-18T14:30:00+00:00",
            "why": "Use this.",
            "passages": ["Useful passage."] * (MAX_PASSAGES + 1),
        }

        with self.assertRaisesRegex(ValueError, "too many passages"):
            capture_payload_to_note(payload, intake_dir=Path("/tmp/not-used"), vault_path=Path("/tmp"))

    def test_parse_content_length_accepts_positive_bounded_value(self) -> None:
        self.assertEqual(parse_content_length(str(MAX_REQUEST_BYTES)), MAX_REQUEST_BYTES)

    def test_parse_content_length_rejects_missing_or_malformed_values(self) -> None:
        for value in (None, "", "abc", "1.5", "+1", " 1", "1 ", "1\n"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "Content-Length"):
                    parse_content_length(value)

    def test_parse_content_length_rejects_non_positive_values(self) -> None:
        for value in ("0", "-1"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "Content-Length"):
                    parse_content_length(value)

    def test_parse_content_length_rejects_large_requests(self) -> None:
        with self.assertRaisesRegex(ValueError, "request is too large"):
            parse_content_length(str(MAX_REQUEST_BYTES + 1))

    def test_sanitize_clip_filename_truncates_long_titles(self) -> None:
        captured_at = datetime(2026, 5, 18, 14, 30, tzinfo=timezone.utc)

        name = sanitize_clip_filename("x" * 300, captured_at)

        self.assertEqual(len(name.removeprefix("2026-05-18 - ").removesuffix(".md")), 120)

    def test_capture_payload_to_note_uses_unique_name_for_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake" / "Web Clips"
            payload = {
                "source_url": "https://example.com/article",
                "source_title": "Article Title",
                "captured_at": "2026-05-18T14:30:00+00:00",
                "why": "Use this.",
                "passages": ["Useful passage."],
            }

            first = capture_payload_to_note(payload, intake_dir=intake_dir, vault_path=vault)
            second = capture_payload_to_note(payload, intake_dir=intake_dir, vault_path=vault)

            self.assertNotEqual(first, second)
            self.assertEqual(second.name, "2026-05-18 - Article Title 2.md")

    def test_bookmarklet_targets_configured_endpoint(self) -> None:
        bookmarklet = render_bookmarklet(host="127.0.0.1", port=8765, token="secret-token")

        self.assertTrue(bookmarklet.startswith("javascript:"))
        self.assertIn("http://127.0.0.1:8765/capture", bookmarklet)
        self.assertIn("Add passage", bookmarklet)

    def test_bookmarklet_sends_token_header_when_configured(self) -> None:
        bookmarklet = render_bookmarklet(host="127.0.0.1", port=8765, token="secret-token")

        self.assertIn("X-Obsidian-Web-Clipper-Token", bookmarklet)
        self.assertIn("secret-token", bookmarklet)

    def test_bookmarklet_reports_processed_and_partial_failure_statuses(self) -> None:
        bookmarklet = render_bookmarklet(host="127.0.0.1", port=8765, token="secret-token")

        self.assertIn("Saved and processed.", bookmarklet)
        self.assertIn("Saved and processed, but follow-up failed:", bookmarklet)
        self.assertIn("Saved, but processing failed:", bookmarklet)
        self.assertIn("Failed: ${responseBody.error || responseText}", bookmarklet)
        self.assertIn("catch (error)", bookmarklet)

    def test_bookmarklet_rejects_blank_token(self) -> None:
        with self.assertRaisesRegex(ValueError, "token is required"):
            render_bookmarklet(host="127.0.0.1", port=8765, token="")

    def test_capture_token_validation_requires_match(self) -> None:
        self.assertTrue(is_valid_capture_token({"X-Obsidian-Web-Clipper-Token": "secret-token"}, "secret-token"))
        self.assertFalse(is_valid_capture_token({"X-Obsidian-Web-Clipper-Token": "wrong-token"}, "secret-token"))
        self.assertFalse(is_valid_capture_token({}, "secret-token"))

    def test_capture_token_validation_rejects_blank_configured_token(self) -> None:
        with self.assertRaisesRegex(ValueError, "token is required"):
            is_valid_capture_token({"X-Obsidian-Web-Clipper-Token": "secret-token"}, "")

    def test_capture_server_rejects_blank_token_before_starting(self) -> None:
        with self.assertRaisesRegex(ValueError, "token is required"):
            run_capture_server(
                host="127.0.0.1",
                port=8765,
                intake_dir=Path("/tmp/not-used"),
                token="",
                vault_path=Path("/tmp"),
            )

    def test_validate_loopback_host_accepts_loopback_hosts(self) -> None:
        for host in ("127.0.0.1", "localhost"):
            with self.subTest(host=host):
                validate_loopback_host(host)

    def test_validate_loopback_host_rejects_non_loopback_hosts(self) -> None:
        for host in ("", "0.0.0.0", "192.168.1.25", "10.0.0.5", "example.com", "::1"):
            with self.subTest(host=host):
                with self.assertRaisesRegex(ValueError, "web clip capture host must be loopback-only"):
                    validate_loopback_host(host)

    def test_capture_server_rejects_non_loopback_host_before_starting(self) -> None:
        with self.assertRaisesRegex(ValueError, "web clip capture host must be loopback-only"):
            run_capture_server(
                host="0.0.0.0",
                port=8765,
                intake_dir=Path("/tmp/not-used"),
                token="secret-token",
                vault_path=Path("/tmp"),
            )

    def test_capture_payload_rejects_intake_dir_outside_vault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            outside = Path(tmp_dir) / "outside"

            with self.assertRaisesRegex(ValueError, "outside the vault"):
                capture_payload_to_note(_payload(), intake_dir=outside, vault_path=vault)

            self.assertFalse(outside.exists())

    def test_capture_payload_rejects_symlinked_intake_dir_outside_vault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            outside = Path(tmp_dir) / "outside"
            outside.mkdir()
            intake_link = vault / "00_Intake" / "Web Clips"
            intake_link.parent.mkdir(parents=True)
            try:
                intake_link.symlink_to(outside, target_is_directory=True)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "outside the vault"):
                capture_payload_to_note(_payload(), intake_dir=intake_link, vault_path=vault)

            self.assertEqual(list(outside.iterdir()), [])

    def test_capture_server_rejects_intake_dir_outside_vault_before_starting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            outside = Path(tmp_dir) / "outside"

            with self.assertRaisesRegex(ValueError, "outside the vault"):
                run_capture_server(
                    host="127.0.0.1",
                    port=8765,
                    intake_dir=outside,
                    token="secret-token",
                    vault_path=vault,
                )

    def test_capture_server_allows_private_network_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake" / "Web Clips"
            server = create_capture_server(
                host="127.0.0.1",
                port=0,
                intake_dir=intake_dir,
                token="secret-token",
                vault_path=vault,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, headers = _options_capture(server.server_port)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

            self.assertEqual(status, 204)
            self.assertEqual(headers.get("Access-Control-Allow-Origin"), "https://example.com")
            self.assertEqual(headers.get("Vary"), "Origin")
            self.assertEqual(headers.get("Access-Control-Allow-Private-Network"), "true")

    def test_capture_server_processes_written_raw_clip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake" / "Web Clips"
            seen_paths: list[Path] = []

            def process_capture(raw_path: Path) -> WebClipFileProcessingResult:
                seen_paths.append(raw_path)
                return WebClipFileProcessingResult(
                    raw_capture_path=raw_path,
                    processed=True,
                    reference_note_path=vault / "10_References" / "Web Clips" / raw_path.name,
                    archived_raw_capture_path=vault / "_Archive" / "Intake" / "Web Clips" / raw_path.name,
                    written_reference_notes=1,
                    archived_raw_captures=1,
                )

            server = create_capture_server(
                host="127.0.0.1",
                port=0,
                intake_dir=intake_dir,
                token="secret-token",
                vault_path=vault,
                process_capture=process_capture,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, body = _post_capture(server.server_port, _payload())
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

            self.assertEqual(status, 201)
            self.assertEqual(len(seen_paths), 1)
            self.assertEqual(body["status"], "processed")
            self.assertEqual(body["path"], str(seen_paths[0]))
            self.assertEqual(body["reference_path"], str(vault / "10_References" / "Web Clips" / seen_paths[0].name))
            self.assertEqual(
                body["archive_path"],
                str(vault / "_Archive" / "Intake" / "Web Clips" / seen_paths[0].name),
            )

    def test_capture_server_preserves_raw_clip_when_processing_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake" / "Web Clips"

            def process_capture(raw_path: Path) -> WebClipFileProcessingResult:
                raise RuntimeError("processor exploded")

            server = create_capture_server(
                host="127.0.0.1",
                port=0,
                intake_dir=intake_dir,
                token="secret-token",
                vault_path=vault,
                process_capture=process_capture,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, body = _post_capture(server.server_port, _payload())
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

            self.assertEqual(status, 500)
            self.assertEqual(body["status"], "saved_processing_failed")
            self.assertIn("processor exploded", body["error"])
            self.assertTrue(Path(str(body["path"])).exists())

    def test_capture_server_calls_processed_callback_after_successful_processing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake" / "Web Clips"
            callback_results: list[WebClipFileProcessingResult] = []

            def process_capture(raw_path: Path) -> WebClipFileProcessingResult:
                return WebClipFileProcessingResult(
                    raw_capture_path=raw_path,
                    processed=True,
                    reference_note_path=vault / "10_References" / "Web Clips" / raw_path.name,
                    archived_raw_capture_path=vault / "_Archive" / "Intake" / "Web Clips" / raw_path.name,
                    written_reference_notes=1,
                    archived_raw_captures=1,
                )

            server = create_capture_server(
                host="127.0.0.1",
                port=0,
                intake_dir=intake_dir,
                token="secret-token",
                vault_path=vault,
                process_capture=process_capture,
                on_processed=callback_results.append,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, _body = _post_capture(server.server_port, _payload())
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

            self.assertEqual(status, 201)
            self.assertEqual(len(callback_results), 1)
            self.assertTrue(callback_results[0].processed)

    def test_capture_server_callback_failure_returns_processed_response_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake" / "Web Clips"

            def process_capture(raw_path: Path) -> WebClipFileProcessingResult:
                return WebClipFileProcessingResult(
                    raw_capture_path=raw_path,
                    processed=True,
                    reference_note_path=vault / "10_References" / "Web Clips" / raw_path.name,
                    archived_raw_capture_path=vault / "_Archive" / "Intake" / "Web Clips" / raw_path.name,
                    written_reference_notes=1,
                    archived_raw_captures=1,
                )

            def on_processed(_result: WebClipFileProcessingResult) -> None:
                raise RuntimeError("auto-commit exploded")

            server = create_capture_server(
                host="127.0.0.1",
                port=0,
                intake_dir=intake_dir,
                token="secret-token",
                vault_path=vault,
                process_capture=process_capture,
                on_processed=on_processed,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, body = _post_capture(server.server_port, _payload())
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

            self.assertEqual(status, 201)
            self.assertEqual(body["status"], "processed")
            self.assertEqual(
                body["reference_path"], str(vault / "10_References" / "Web Clips" / Path(str(body["path"])).name)
            )
            self.assertEqual(
                body["archive_path"],
                str(vault / "_Archive" / "Intake" / "Web Clips" / Path(str(body["path"])).name),
            )
            self.assertIn("post-processing callback failed", body["warning"])
            self.assertIn("auto-commit exploded", body["warning"])

    def test_capture_server_does_not_call_processed_callback_for_unprocessed_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake_dir = vault / "00_Intake" / "Web Clips"
            callback_results: list[WebClipFileProcessingResult] = []

            def process_capture(raw_path: Path) -> WebClipFileProcessingResult:
                return WebClipFileProcessingResult(raw_capture_path=raw_path, processed=False)

            server = create_capture_server(
                host="127.0.0.1",
                port=0,
                intake_dir=intake_dir,
                token="secret-token",
                vault_path=vault,
                process_capture=process_capture,
                on_processed=callback_results.append,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, body = _post_capture(server.server_port, _payload())
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

            self.assertEqual(status, 201)
            self.assertEqual(body["status"], "saved_unprocessed")
            self.assertEqual(callback_results, [])


def _payload() -> dict[str, object]:
    return {
        "source_url": "https://example.com/article",
        "source_title": "Article Title",
        "captured_at": "2026-05-18T14:30:00+00:00",
        "why": "Use this.",
        "passages": ["Useful passage."],
    }


def _post_capture(port: int, payload: dict[str, object], token: str = "secret-token") -> tuple[int, dict[str, object]]:
    body_text = json.dumps(payload)
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(
            "POST",
            "/capture",
            body=body_text,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body_text.encode("utf-8"))),
                "X-Obsidian-Web-Clipper-Token": token,
            },
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        return response.status, body
    finally:
        connection.close()


def _options_capture(port: int) -> tuple[int, dict[str, str]]:
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(
            "OPTIONS",
            "/capture",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-obsidian-web-clipper-token",
                "Access-Control-Request-Private-Network": "true",
            },
        )
        response = connection.getresponse()
        response.read()
        return response.status, dict(response.getheaders())
    finally:
        connection.close()
