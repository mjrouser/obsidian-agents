# Web Clipper Auto-Processing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make bookmarklet capture immediately produce a processed web clip reference note while preserving and archiving the raw capture.

**Architecture:** Add a single-file processing primitive to `WebClipProcessor`, then have batch processing and capture-time processing share it. The capture server will save the raw clip first, then process only that newly written raw note and return structured JSON to the bookmarklet.

**Tech Stack:** Python 3.11+, stdlib HTTP server, existing Obsidian markdown renderers, existing unittest/pytest suite.

---

### Task 1: Single-File Web Clip Processing

**Files:**
- Modify: `src/obsidian_intake_agent/processors/web_clip_processor.py`
- Modify: `tests/test_web_clip_processor.py`

- [ ] **Step 1: Write failing single-file processing tests**

Add these tests to `tests/test_web_clip_processor.py`:

```python
    def test_process_file_writes_reference_note_and_archives_only_requested_raw_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake = vault / "00_Intake" / "Web Clips"
            requested = intake / "2026-05-18 - Article.md"
            untouched = intake / "2026-05-18 - Other.md"
            intake.mkdir(parents=True)
            requested.write_text(_raw_note(), encoding="utf-8")
            untouched.write_text(_raw_note().replace("Article", "Other"), encoding="utf-8")
            processor = WebClipProcessor(config(vault, dry_run=False))

            result = processor.process_file(requested, dry_run=False)

            self.assertTrue(result.processed)
            self.assertEqual(result.raw_capture_path, requested)
            self.assertEqual(
                result.reference_note_path,
                vault / "10_References" / "Web Clips" / "2026-05-18 - Article.md",
            )
            self.assertEqual(
                result.archived_raw_capture_path,
                vault / "_Archive" / "Intake" / "Web Clips" / "2026-05-18 - Article.md",
            )
            self.assertTrue(result.reference_note_path.exists())
            self.assertTrue(result.archived_raw_capture_path.exists())
            self.assertFalse(requested.exists())
            self.assertTrue(untouched.exists())

    def test_process_file_rejects_path_outside_web_clip_intake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            outside_raw = vault / "00_Intake" / "Other" / "2026-05-18 - Article.md"
            outside_raw.parent.mkdir(parents=True)
            outside_raw.write_text(_raw_note(), encoding="utf-8")
            processor = WebClipProcessor(config(vault, dry_run=False))

            with self.assertRaisesRegex(ValueError, "web clip intake note must be under"):
                processor.process_file(outside_raw, dry_run=False)

            self.assertTrue(outside_raw.exists())
            self.assertFalse((vault / "10_References" / "Web Clips").exists())

    def test_process_file_returns_unprocessed_for_malformed_raw_clip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            raw = vault / "00_Intake" / "Web Clips" / "2026-05-18 - Broken.md"
            raw.parent.mkdir(parents=True)
            raw.write_text("---\ntype: web_clip_intake\nstatus: unprocessed\n", encoding="utf-8")
            processor = WebClipProcessor(config(vault, dry_run=False))

            result = processor.process_file(raw, dry_run=False)

            self.assertFalse(result.processed)
            self.assertEqual(result.raw_capture_path, raw)
            self.assertIsNone(result.reference_note_path)
            self.assertIsNone(result.archived_raw_capture_path)
            self.assertTrue(raw.exists())
```

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_clip_processor.py::WebClipProcessorTests::test_process_file_writes_reference_note_and_archives_only_requested_raw_capture tests/test_web_clip_processor.py::WebClipProcessorTests::test_process_file_rejects_path_outside_web_clip_intake tests/test_web_clip_processor.py::WebClipProcessorTests::test_process_file_returns_unprocessed_for_malformed_raw_clip -q
```

Expected: fail with `AttributeError: 'WebClipProcessor' object has no attribute 'process_file'`.

- [ ] **Step 3: Add result dataclass and `process_file()`**

In `src/obsidian_intake_agent/processors/web_clip_processor.py`, add:

```python
@dataclass(frozen=True, slots=True)
class WebClipFileProcessingResult:
    raw_capture_path: Path
    processed: bool = False
    reference_note_path: Path | None = None
    archived_raw_capture_path: Path | None = None
    written_reference_notes: int = 0
    archived_raw_captures: int = 0
```

Then add `process_file()` to `WebClipProcessor`. It should:

```python
    def process_file(self, raw_path: Path, dry_run: bool | None = None) -> WebClipFileProcessingResult:
        effective_dry_run = self.config.dry_run if dry_run is None else dry_run
        self.intake_path = validate_vault_path(self.vault_path, self.intake_path, label="web clip intake directory")
        raw_path = validate_vault_path(self.vault_path, raw_path, label="web clip intake note")
        if raw_path.parent != self.intake_path:
            raise ValueError("web clip intake note must be under the configured web clip intake directory.")
        if raw_path.is_symlink() or not raw_path.exists():
            return WebClipFileProcessingResult(raw_capture_path=raw_path)

        raw_text = raw_path.read_text(encoding="utf-8")
        parsed = _parse_raw_note(raw_text)
        if parsed is None:
            return WebClipFileProcessingResult(raw_capture_path=raw_path)

        base_reference_path = validate_vault_path(
            self.vault_path, self.references_path / raw_path.name, label="web clip reference note"
        )
        base_archive_path = validate_vault_path(
            self.vault_path, self.archive_path / raw_path.name, label="web clip archive note"
        )

        recoverable_archive_path = _recoverable_archive_path_for_existing_reference(
            vault_path=self.vault_path,
            references_path=self.references_path,
            archive_path=self.archive_path,
            raw_name=raw_path.name,
        )
        if not effective_dry_run and recoverable_archive_path is not None:
            recoverable_archive_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.replace(recoverable_archive_path)
            return WebClipFileProcessingResult(
                raw_capture_path=raw_path,
                processed=True,
                archived_raw_capture_path=recoverable_archive_path,
                archived_raw_captures=1,
            )

        reference_path = _unique_path(base_reference_path)
        archive_path = _unique_path(base_archive_path)
        if effective_dry_run:
            print(f"DRY RUN: would write web clip reference note: {reference_path}")
            print(f"DRY RUN: would archive web clip intake note: {archive_path}")
            return WebClipFileProcessingResult(
                raw_capture_path=raw_path,
                processed=True,
                reference_note_path=reference_path,
                archived_raw_capture_path=archive_path,
            )

        processed = _build_processed_clip(parsed, vault_relative_path(self.vault_path, archive_path))
        safe_write_text(reference_path, render_processed_web_clip_note(processed))
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.replace(archive_path)
        return WebClipFileProcessingResult(
            raw_capture_path=raw_path,
            processed=True,
            reference_note_path=reference_path,
            archived_raw_capture_path=archive_path,
            written_reference_notes=1,
            archived_raw_captures=1,
        )
```

- [ ] **Step 4: Verify GREEN**

Run the same focused pytest command from Step 2.

Expected: all three tests pass.

### Task 2: Reuse Single-File Processing From Batch Processing

**Files:**
- Modify: `src/obsidian_intake_agent/processors/web_clip_processor.py`
- Modify: `tests/test_web_clip_processor.py`

- [ ] **Step 1: Add a regression test for batch summary counts**

Add this test to `tests/test_web_clip_processor.py`:

```python
    def test_process_all_summary_counts_match_single_file_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            intake = vault / "00_Intake" / "Web Clips"
            intake.mkdir(parents=True)
            (intake / "2026-05-18 - Article.md").write_text(_raw_note(), encoding="utf-8")
            (intake / "2026-05-18 - Broken.md").write_text(
                "---\ntype: web_clip_intake\nstatus: unprocessed\n",
                encoding="utf-8",
            )
            processor = WebClipProcessor(config(vault, dry_run=False))

            result = processor.process_all(dry_run=False)

            self.assertEqual(result.processed_files, 1)
            self.assertEqual(result.written_reference_notes, 1)
            self.assertEqual(result.archived_raw_captures, 1)
            self.assertTrue((vault / "00_Intake" / "Web Clips" / "2026-05-18 - Broken.md").exists())
```

- [ ] **Step 2: Verify the regression test**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_clip_processor.py::WebClipProcessorTests::test_process_all_summary_counts_match_single_file_results -q
```

Expected before refactor: pass, because current batch processing already skips malformed raw clips and counts only valid processed clips.

- [ ] **Step 3: Refactor `process_all()`**

Replace the body of `process_all()` so it validates `self.intake_path`, loops over eligible `*.md` files, calls `self.process_file(raw_path, dry_run=effective_dry_run)`, and increments summary counts only when `result.processed` is true:

```python
    def process_all(self, dry_run: bool | None = None) -> WebClipProcessingSummary:
        effective_dry_run = self.config.dry_run if dry_run is None else dry_run
        self.intake_path = validate_vault_path(self.vault_path, self.intake_path, label="web clip intake directory")
        if not self.intake_path.exists():
            return WebClipProcessingSummary()

        processed_files = 0
        written_reference_notes = 0
        archived_raw_captures = 0
        for raw_path in sorted(self.intake_path.glob("*.md")):
            result = self.process_file(raw_path, dry_run=effective_dry_run)
            if not result.processed:
                continue
            processed_files += 1
            written_reference_notes += result.written_reference_notes
            archived_raw_captures += result.archived_raw_captures

        return WebClipProcessingSummary(
            processed_files=processed_files,
            written_reference_notes=written_reference_notes,
            archived_raw_captures=archived_raw_captures,
        )
```

- [ ] **Step 4: Verify all web clip processor tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_clip_processor.py -q
```

Expected: all tests pass.

### Task 3: Capture Server Auto-Processing

**Files:**
- Modify: `src/obsidian_intake_agent/web_clips/capture_server.py`
- Modify: `src/obsidian_intake_agent/main.py`
- Modify: `tests/test_web_clip_capture_server.py`

- [ ] **Step 1: Write failing capture auto-processing tests**

Add imports to `tests/test_web_clip_capture_server.py`:

```python
import json
import threading
from http.client import HTTPConnection
from obsidian_intake_agent.processors.web_clip_processor import WebClipFileProcessingResult
from obsidian_intake_agent.web_clips.capture_server import create_capture_server
```

Add helper:

```python
def _post_capture(port: int, payload: dict[str, object], token: str = "secret-token") -> tuple[int, dict[str, object]]:
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(
        "POST",
        "/capture",
        body=json.dumps(payload),
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(json.dumps(payload).encode("utf-8"))),
            "X-Obsidian-Web-Clipper-Token": token,
        },
    )
    response = connection.getresponse()
    body = json.loads(response.read().decode("utf-8"))
    connection.close()
    return response.status, body
```

Add tests:

```python
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

            self.assertEqual(status, 201)
            self.assertEqual(len(callback_results), 1)
            self.assertTrue(callback_results[0].processed)
```

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_clip_capture_server.py::WebClipCaptureServerTests::test_capture_server_processes_written_raw_clip tests/test_web_clip_capture_server.py::WebClipCaptureServerTests::test_capture_server_preserves_raw_clip_when_processing_fails tests/test_web_clip_capture_server.py::WebClipCaptureServerTests::test_capture_server_calls_processed_callback_after_successful_processing -q
```

Expected: fail because `create_capture_server` does not exist.

- [ ] **Step 3: Add `create_capture_server()` and processing hooks**

In `src/obsidian_intake_agent/web_clips/capture_server.py`, import `Callable`, then add:

```python
def create_capture_server(
    *,
    host: str,
    port: int,
    intake_dir: Path,
    token: str,
    vault_path: Path,
    process_capture: Callable[[Path], Any] | None = None,
    on_processed: Callable[[Any], None] | None = None,
) -> HTTPServer:
    validate_loopback_host(host)
    _validate_token(token)
    resolved_intake_dir = validate_vault_path(vault_path, intake_dir, label="web clip intake directory")

    class CaptureRequestHandler(BaseHTTPRequestHandler):
        ...

    return HTTPServer((host, port), CaptureRequestHandler)
```

Move the existing nested handler from `run_capture_server()` into this function. In `do_POST()`, after `written = capture_payload_to_note(...)`, run:

```python
                result = process_capture(written) if process_capture is not None else None
            except (json.JSONDecodeError, ValueError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except Exception as exc:
                self._send_json(500, {"status": "saved_processing_failed", "path": str(written), "error": str(exc)})
                return

            if result is not None and getattr(result, "processed", False) and on_processed is not None:
                on_processed(result)

            self._send_json(
                201,
                {
                    "status": "processed" if result is not None and getattr(result, "processed", False) else "saved_unprocessed",
                    "path": str(written),
                    "reference_path": str(result.reference_note_path) if result is not None and result.reference_note_path else None,
                    "archive_path": str(result.archived_raw_capture_path) if result is not None and result.archived_raw_capture_path else None,
                },
            )
```

- [ ] **Step 4: Update `run_capture_server()` and CLI integration**

Keep `run_capture_server()` as the public function and have it call `create_capture_server(...).serve_forever()`. Update its signature to accept `process_capture: Callable[[Path], Any] | None = None` and `on_processed: Callable[[Any], None] | None = None`.

In `src/obsidian_intake_agent/main.py`, pass the configured processor and auto-commit callback:

```python
            def process_capture(raw_path: Path):
                return WebClipProcessor(config).process_file(raw_path, dry_run=False)

            def on_processed(result: object) -> None:
                if getattr(result, "written_reference_notes", 0) > 0 or getattr(result, "archived_raw_captures", 0) > 0:
                    _maybe_auto_commit(config, vault_source_name="web clips")

            run_capture_server(
                host=config.web_clips_capture_host,
                port=config.web_clips_capture_port,
                intake_dir=intake_dir,
                token=token,
                vault_path=config.vault_path,
                process_capture=process_capture,
                on_processed=on_processed,
            )
```

- [ ] **Step 5: Verify capture tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_clip_capture_server.py -q
```

Expected: all capture server tests pass.

### Task 4: Bookmarklet Status Text

**Files:**
- Modify: `src/obsidian_intake_agent/web_clips/bookmarklet.py`
- Modify: `tests/test_web_clip_capture_server.py`

- [ ] **Step 1: Write failing bookmarklet status test**

Add this assertion to `test_bookmarklet_targets_configured_endpoint` or a new test in `tests/test_web_clip_capture_server.py`:

```python
    def test_bookmarklet_reports_processed_and_partial_failure_statuses(self) -> None:
        bookmarklet = render_bookmarklet(host="127.0.0.1", port=8765, token="secret-token")

        self.assertIn("Saved and processed.", bookmarklet)
        self.assertIn("Saved, but processing failed:", bookmarklet)
```

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_clip_capture_server.py::WebClipCaptureServerTests::test_bookmarklet_reports_processed_and_partial_failure_statuses -q
```

Expected: fail because the bookmarklet only renders `Saved.` today.

- [ ] **Step 3: Update bookmarklet response handling**

In `src/obsidian_intake_agent/web_clips/bookmarklet.py`, replace:

```javascript
    status.textContent = response.ok ? "Saved." : `Failed: ${await response.text()}`;
```

with:

```javascript
    const responseText = await response.text();
    let responseBody = {};
    try { responseBody = responseText ? JSON.parse(responseText) : {}; } catch (_) {}
    if (response.ok && responseBody.status === "processed") {
      status.textContent = "Saved and processed.";
    } else if (responseBody.status === "saved_processing_failed") {
      status.textContent = `Saved, but processing failed: ${responseBody.error || responseText}`;
    } else if (response.ok) {
      status.textContent = "Saved.";
    } else {
      status.textContent = `Failed: ${responseText}`;
    }
```

- [ ] **Step 4: Verify bookmarklet test**

Run the same focused pytest command from Step 2.

Expected: pass.

### Task 5: Docs and Final Verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update README**

In `README.md`, change the web clipper description from “Run process manually after capture” to:

```markdown
The capture server processes each newly saved clip immediately. A successful bookmarklet save writes a processed reference note under `10_References/Web Clips` and archives the raw capture under `_Archive/Intake/Web Clips`.

Use the manual processor when you want to retry older raw captures:
```

Keep the existing `obsidian-agent web-clips process --dry-run` and `obsidian-agent web-clips process` commands as retry/batch tools.

- [ ] **Step 2: Run focused web clip tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_clip_processor.py tests/test_web_clip_capture_server.py tests/test_main.py -q
```

Expected: pass.

- [ ] **Step 3: Run required checks**

Run:

```bash
make check
make test
make smoke
make build
```

Expected: pass. If unrelated dirty meeting-bundle files cause failures, report that explicitly and keep this feature diff separated.

- [ ] **Step 4: Review diff for unrelated changes and secrets**

Run:

```bash
git status --short --branch
git diff -- src/obsidian_intake_agent/processors/web_clip_processor.py src/obsidian_intake_agent/web_clips/capture_server.py src/obsidian_intake_agent/web_clips/bookmarklet.py src/obsidian_intake_agent/main.py tests/test_web_clip_processor.py tests/test_web_clip_capture_server.py README.md docs/superpowers/specs/2026-05-19-web-clipper-auto-processing-design.md docs/superpowers/plans/2026-05-19-web-clipper-auto-processing.md
rg -n "OBSIDIAN_WEB_CLIPPER_TOKEN=[A-Za-z0-9+/=]{20,}|<known-token-fragment>" .
```

Expected: only web clip auto-processing files plus the approved spec/plan are changed, and no token values appear.
