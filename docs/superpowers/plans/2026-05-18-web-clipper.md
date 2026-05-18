# Web Clipper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local bookmarklet-driven web clipping pipeline that captures passages into `00_Intake/Web Clips`, processes them into `10_References/Web Clips`, and surfaces relevant processed clips in weekly briefings and wraps.

**Architecture:** Implement web clipping as a sibling pipeline to meeting intake. A standard-library localhost HTTP server writes raw clip notes, a dedicated processor parses and enriches raw clips into canonical reference notes, and weekly generation pulls a small deterministic set of relevant processed clips into the existing source bundle.

**Tech Stack:** Python 3.11 standard library (`http.server`, `json`, `dataclasses`, `datetime`, `pathlib`, `re`, `urllib.parse`), existing repo utilities (`safe_write_text`, config loading, CLI), `unittest`, Ruff, mypy.

---

## File Structure

- Create `src/obsidian_intake_agent/web_clips/__init__.py`
  - Exports capture models and server entry points.
- Create `src/obsidian_intake_agent/web_clips/models.py`
  - Defines `WebClipCapture`, `WebClipPassage`, `ProcessedWebClip`, and validation helpers.
- Create `src/obsidian_intake_agent/web_clips/capture_server.py`
  - Runs the localhost capture server, validates POST payloads, and writes raw capture notes.
- Create `src/obsidian_intake_agent/web_clips/bookmarklet.py`
  - Renders the JavaScript bookmarklet string from configured host/port.
- Create `src/obsidian_intake_agent/rendering/web_clip_renderer.py`
  - Renders raw intake notes and processed reference notes.
- Create `src/obsidian_intake_agent/processors/web_clip_processor.py`
  - Finds unprocessed raw web clips, parses front matter/body sections, writes canonical notes, and archives raw captures.
- Create `src/obsidian_intake_agent/web_clips/retrieval.py`
  - Loads processed clips and selects relevant clips for weekly context with deterministic term overlap.
- Modify `src/obsidian_intake_agent/config.py`
  - Adds web clip folder, host, port, and weekly-result configuration.
- Modify `src/obsidian_intake_agent/main.py`
  - Adds `web-clips serve`, `web-clips bookmarklet`, and `web-clips process`.
- Modify `src/obsidian_intake_agent/weekly.py`
  - Appends a bounded processed-clip source block to briefing/wrap source material.
- Modify `config.example.yaml`
  - Documents reusable web clip defaults.
- Modify `README.md`
  - Adds setup and run commands for the web clipper.
- Create tests:
  - `tests/test_web_clip_renderer.py`
  - `tests/test_web_clip_capture_server.py`
  - `tests/test_web_clip_processor.py`
  - `tests/test_web_clip_retrieval.py`
  - Add focused cases to `tests/test_config.py`, `tests/test_main.py`, and `tests/test_weekly.py`.

## Implementation Decisions

- Default capture server: `127.0.0.1:8765`.
- Raw capture folder: `00_Intake/Web Clips`.
- Processed reference folder: `10_References/Web Clips`.
- Processed raw captures are archived under `z_Archive/Intake/Web Clips` after successful canonical note creation.
- Initial enrichment is deterministic, not LLM-based:
  - Summary uses the first passage or why-note excerpt.
  - Topics are extracted from repeated significant words in title, why-note, and passages.
  - Application is derived from Matthew's why-note.
- Weekly surfacing is deterministic term overlap against the existing weekly source bundle. Semantic retrieval is intentionally deferred.

---

### Task 1: Web Clip Configuration

**Files:**
- Modify: `src/obsidian_intake_agent/config.py`
- Modify: `config.example.yaml`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing config tests**

Add these tests to `tests/test_config.py` inside `ConfigCompatibilityTests`:

```python
    def test_web_clip_defaults_load_when_omitted(self) -> None:
        config_path = _write_config(self)

        loaded = Config.load(config_path)

        self.assertEqual(loaded.web_clips_intake_dir, "00_Intake/Web Clips")
        self.assertEqual(loaded.web_clips_references_dir, "10_References/Web Clips")
        self.assertEqual(loaded.web_clips_capture_host, "127.0.0.1")
        self.assertEqual(loaded.web_clips_capture_port, 8765)
        self.assertEqual(loaded.web_clips_max_weekly_results, 3)

    def test_web_clip_settings_can_be_overridden(self) -> None:
        config_path = _write_config(
            self,
            'web_clips_intake_dir: "00_Intake/Clips"',
            'web_clips_references_dir: "10_References/Clips"',
            'web_clips_capture_host: "127.0.0.1"',
            "web_clips_capture_port: 9876",
            "web_clips_max_weekly_results: 5",
        )

        loaded = Config.load(config_path)

        self.assertEqual(loaded.web_clips_intake_dir, "00_Intake/Clips")
        self.assertEqual(loaded.web_clips_references_dir, "10_References/Clips")
        self.assertEqual(loaded.web_clips_capture_host, "127.0.0.1")
        self.assertEqual(loaded.web_clips_capture_port, 9876)
        self.assertEqual(loaded.web_clips_max_weekly_results, 5)

    def test_web_clip_numeric_settings_must_be_positive(self) -> None:
        with self.assertRaisesRegex(ValueError, "web_clips_capture_port must be a positive integer"):
            Config.load(_write_config(self, "web_clips_capture_port: 0"))

        with self.assertRaisesRegex(ValueError, "web_clips_max_weekly_results must be a positive integer"):
            Config.load(_write_config(self, "web_clips_max_weekly_results: 0"))
```

- [ ] **Step 2: Run config tests to verify failure**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_config.ConfigCompatibilityTests -v
```

Expected: FAIL with `AttributeError` for missing `web_clips_*` fields.

- [ ] **Step 3: Add config fields and loading**

In `src/obsidian_intake_agent/config.py`, add these dataclass fields after `weekly_reviews_dir`:

```python
    web_clips_intake_dir: str = "00_Intake/Web Clips"
    web_clips_references_dir: str = "10_References/Web Clips"
    web_clips_capture_host: str = "127.0.0.1"
    web_clips_capture_port: int = 8765
    web_clips_max_weekly_results: int = 3
```

In `Config.load`, add these keyword arguments after `weekly_reviews_dir=...`:

```python
            web_clips_intake_dir=str(data.get("web_clips_intake_dir", "00_Intake/Web Clips")),
            web_clips_references_dir=str(data.get("web_clips_references_dir", "10_References/Web Clips")),
            web_clips_capture_host=str(data.get("web_clips_capture_host", "127.0.0.1")),
            web_clips_capture_port=_positive_int(data.get("web_clips_capture_port", 8765), "web_clips_capture_port"),
            web_clips_max_weekly_results=_positive_int(
                data.get("web_clips_max_weekly_results", 3),
                "web_clips_max_weekly_results",
            ),
```

- [ ] **Step 4: Document config defaults**

Add these keys to `config.example.yaml` after `weekly_reviews_dir`:

```yaml
web_clips_intake_dir: "00_Intake/Web Clips"
web_clips_references_dir: "10_References/Web Clips"
web_clips_capture_host: "127.0.0.1"
web_clips_capture_port: 8765
web_clips_max_weekly_results: 3
```

- [ ] **Step 5: Run config tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_config.ConfigCompatibilityTests -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/obsidian_intake_agent/config.py config.example.yaml tests/test_config.py
git commit -m "feat: add web clip config defaults"
```

---

### Task 2: Web Clip Models And Rendering

**Files:**
- Create: `src/obsidian_intake_agent/web_clips/__init__.py`
- Create: `src/obsidian_intake_agent/web_clips/models.py`
- Create: `src/obsidian_intake_agent/rendering/web_clip_renderer.py`
- Test: `tests/test_web_clip_renderer.py`

- [ ] **Step 1: Write failing renderer tests**

Create `tests/test_web_clip_renderer.py`:

```python
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from obsidian_intake_agent.rendering.web_clip_renderer import (
    render_processed_web_clip_note,
    render_raw_web_clip_note,
)
from obsidian_intake_agent.web_clips.models import ProcessedWebClip, WebClipCapture, WebClipPassage


class WebClipRendererTests(unittest.TestCase):
    def test_render_raw_note_preserves_passages_and_source_metadata(self) -> None:
        capture = WebClipCapture(
            source_url="https://example.com/article",
            source_title="Article Title",
            captured_at=datetime(2026, 5, 18, 14, 30, tzinfo=timezone.utc),
            why="Use this for CRM adoption messaging.",
            passages=[
                WebClipPassage(text="First exact passage."),
                WebClipPassage(text="Second exact passage."),
            ],
        )

        rendered = render_raw_web_clip_note(capture)

        self.assertIn("type: web_clip_intake", rendered)
        self.assertIn("status: unprocessed", rendered)
        self.assertIn('source_url: "https://example.com/article"', rendered)
        self.assertIn("# Article Title", rendered)
        self.assertIn("Use this for CRM adoption messaging.", rendered)
        self.assertIn("> First exact passage.", rendered)
        self.assertIn("> Second exact passage.", rendered)
        self.assertTrue(rendered.endswith("\n"))

    def test_render_processed_note_preserves_original_passages(self) -> None:
        processed = ProcessedWebClip(
            source_url="https://example.com/article",
            source_title="Article Title",
            captured_at="2026-05-18T14:30:00+00:00",
            why="Use this for CRM adoption messaging.",
            summary="This source explains adoption pressure.",
            passages=["First exact passage.", "Second exact passage."],
            topics=["adoption", "crm"],
            application="Use this for CRM adoption messaging.",
            related=[],
            intake_source="z_Archive/Intake/Web Clips/Article Title.md",
        )

        rendered = render_processed_web_clip_note(processed)

        self.assertIn("type: web_clip", rendered)
        self.assertIn("- adoption", rendered)
        self.assertIn("## Application", rendered)
        self.assertIn("Use this for CRM adoption messaging.", rendered)
        self.assertIn("> First exact passage.", rendered)
        self.assertIn("> Second exact passage.", rendered)
        self.assertIn("[[z_Archive/Intake/Web Clips/Article Title.md]]", rendered)
```

- [ ] **Step 2: Run renderer tests to verify failure**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_web_clip_renderer -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Create models**

Create `src/obsidian_intake_agent/web_clips/__init__.py`:

```python
"""Web clip capture, processing, and retrieval support."""
```

Create `src/obsidian_intake_agent/web_clips/models.py`:

```python
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
```

- [ ] **Step 4: Create renderer**

Create `src/obsidian_intake_agent/rendering/web_clip_renderer.py`:

```python
from __future__ import annotations

from obsidian_intake_agent.web_clips.models import ProcessedWebClip, WebClipCapture, validate_capture


def render_raw_web_clip_note(capture: WebClipCapture) -> str:
    validate_capture(capture)
    title = capture.source_title.strip()
    passages = "\n\n".join(_blockquote(passage.text) for passage in capture.passages)
    return (
        "---\n"
        "type: web_clip_intake\n"
        "status: unprocessed\n"
        f'captured_at: "{capture.captured_at.isoformat()}"\n'
        f'source_url: "{_yaml_escape(capture.source_url.strip())}"\n'
        f'source_title: "{_yaml_escape(title)}"\n'
        "---\n\n"
        f"# {title}\n\n"
        f"Source: {capture.source_url.strip()}\n\n"
        "## Why This Matters\n\n"
        f"{capture.why.strip()}\n\n"
        "## Captured Passages\n\n"
        f"{passages}\n"
    )


def render_processed_web_clip_note(clip: ProcessedWebClip) -> str:
    topics = "\n".join(f"  - {_yaml_escape(topic)}" for topic in clip.topics)
    related = "\n".join(f"  - \"{_yaml_escape(item)}\"" for item in clip.related)
    topics_block = topics if topics else "  []"
    related_block = related if related else "  []"
    passages = "\n\n".join(_blockquote(passage) for passage in clip.passages)
    intake_link = f"[[{clip.intake_source}]]" if clip.intake_source else ""
    return (
        "---\n"
        "type: web_clip\n"
        f'source_url: "{_yaml_escape(clip.source_url)}"\n'
        f'source_title: "{_yaml_escape(clip.source_title)}"\n'
        f'captured_at: "{_yaml_escape(clip.captured_at)}"\n'
        "topics:\n"
        f"{topics_block}\n"
        "related:\n"
        f"{related_block}\n"
        "---\n\n"
        f"# {clip.source_title}\n\n"
        "## Why This Matters\n\n"
        f"{clip.why.strip()}\n\n"
        "## Summary\n\n"
        f"{clip.summary.strip()}\n\n"
        "## Captured Passages\n\n"
        f"{passages}\n\n"
        "## Application\n\n"
        f"{clip.application.strip()}\n\n"
        "## Related Context\n\n"
        f"{intake_link}\n"
    )


def _blockquote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.strip().splitlines())


def _yaml_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')
```

- [ ] **Step 5: Run renderer tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_web_clip_renderer -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/obsidian_intake_agent/web_clips src/obsidian_intake_agent/rendering/web_clip_renderer.py tests/test_web_clip_renderer.py
git commit -m "feat: render web clip notes"
```

---

### Task 3: Capture Server And Bookmarklet

**Files:**
- Create: `src/obsidian_intake_agent/web_clips/capture_server.py`
- Create: `src/obsidian_intake_agent/web_clips/bookmarklet.py`
- Test: `tests/test_web_clip_capture_server.py`

- [ ] **Step 1: Write failing capture tests**

Create `tests/test_web_clip_capture_server.py`:

```python
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from obsidian_intake_agent.web_clips.bookmarklet import render_bookmarklet
from obsidian_intake_agent.web_clips.capture_server import capture_payload_to_note, sanitize_clip_filename


class WebClipCaptureServerTests(unittest.TestCase):
    def test_sanitize_clip_filename_keeps_safe_title_and_date(self) -> None:
        captured_at = datetime(2026, 5, 18, 14, 30, tzinfo=timezone.utc)

        name = sanitize_clip_filename("A/B: Useful * Article?", captured_at)

        self.assertEqual(name, "2026-05-18 - A-B Useful Article.md")

    def test_capture_payload_to_note_writes_raw_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            intake_dir = Path(tmp_dir) / "00_Intake" / "Web Clips"
            payload = {
                "source_url": "https://example.com/article",
                "source_title": "Article Title",
                "captured_at": "2026-05-18T14:30:00+00:00",
                "why": "Use this for CRM adoption messaging.",
                "passages": ["First exact passage.", "Second exact passage."],
            }

            written = capture_payload_to_note(payload, intake_dir=intake_dir)

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
            capture_payload_to_note(payload, intake_dir=Path("/tmp/not-used"))

    def test_bookmarklet_targets_configured_endpoint(self) -> None:
        bookmarklet = render_bookmarklet(host="127.0.0.1", port=8765)

        self.assertTrue(bookmarklet.startswith("javascript:"))
        self.assertIn("http://127.0.0.1:8765/capture", bookmarklet)
        self.assertIn("Add passage", bookmarklet)
```

- [ ] **Step 2: Run capture tests to verify failure**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_web_clip_capture_server -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement payload writing and filename sanitization**

Create `src/obsidian_intake_agent/web_clips/capture_server.py`:

```python
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from obsidian_intake_agent.rendering.web_clip_renderer import render_raw_web_clip_note
from obsidian_intake_agent.utils.fs import safe_write_text
from obsidian_intake_agent.web_clips.models import WebClipCapture, WebClipPassage

SAFE_FILENAME = re.compile(r"[^A-Za-z0-9 ._-]+")


def sanitize_clip_filename(title: str, captured_at: datetime) -> str:
    cleaned = SAFE_FILENAME.sub("-", title).strip(" ._-")
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"-+", "-", cleaned).strip(" ._-")
    if not cleaned:
        cleaned = "Untitled Web Clip"
    return f"{captured_at.date().isoformat()} - {cleaned[:80]}.md"


def capture_payload_to_note(payload: dict[str, Any], *, intake_dir: Path) -> Path:
    capture = _capture_from_payload(payload)
    note = render_raw_web_clip_note(capture)
    intake_dir.mkdir(parents=True, exist_ok=True)
    path = _unique_path(intake_dir / sanitize_clip_filename(capture.source_title, capture.captured_at))
    safe_write_text(path, note)
    return path


def run_capture_server(*, host: str, port: int, intake_dir: Path) -> None:
    class WebClipCaptureHandler(BaseHTTPRequestHandler):
        def do_OPTIONS(self) -> None:
            self._send_json({"ok": True}, status=204)

        def do_POST(self) -> None:
            if self.path != "/capture":
                self._send_json({"ok": False, "error": "not found"}, status=404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("payload must be a JSON object.")
                written = capture_payload_to_note(payload, intake_dir=intake_dir)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
                return
            self._send_json({"ok": True, "path": str(written)})

        def log_message(self, format: str, *args: object) -> None:
            return

        def _send_json(self, payload: dict[str, object], *, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if status != 204:
                self.wfile.write(body)

    server = HTTPServer((host, port), WebClipCaptureHandler)
    server.serve_forever()


def _capture_from_payload(payload: dict[str, Any]) -> WebClipCapture:
    captured_at_value = str(payload.get("captured_at") or "")
    captured_at = datetime.fromisoformat(captured_at_value) if captured_at_value else datetime.now(timezone.utc)
    passages_value = payload.get("passages")
    if not isinstance(passages_value, list):
        raise ValueError("passages must be a list.")
    return WebClipCapture(
        source_url=str(payload.get("source_url") or ""),
        source_title=str(payload.get("source_title") or ""),
        captured_at=captured_at,
        why=str(payload.get("why") or ""),
        passages=[WebClipPassage(text=str(item)) for item in passages_value],
    )


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem} {index}{suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Could not create unique web clip filename for {path}")
```

- [ ] **Step 4: Implement bookmarklet renderer**

Create `src/obsidian_intake_agent/web_clips/bookmarklet.py`:

```python
from __future__ import annotations

from urllib.parse import quote


def render_bookmarklet(*, host: str, port: int) -> str:
    endpoint = f"http://{host}:{port}/capture"
    script = f"""
(() => {{
  const endpoint = "{endpoint}";
  const passages = [];
  const panel = document.createElement("div");
  panel.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483647;background:#fff;color:#111;border:1px solid #888;padding:12px;width:320px;font:14px system-ui,sans-serif;box-shadow:0 4px 20px rgba(0,0,0,.2)";
  panel.innerHTML = '<strong>Clip to Obsidian</strong><p><button id="obs-add">Add passage</button> <button id="obs-save">Save</button> <button id="obs-close">Close</button></p><textarea id="obs-why" aria-label="Why this matters" style="width:100%;height:80px"></textarea><p id="obs-count">0 passages</p>';
  document.body.appendChild(panel);
  const count = panel.querySelector("#obs-count");
  panel.querySelector("#obs-add").onclick = () => {{
    const text = String(window.getSelection());
    if (text.trim()) {{
      passages.push(text.trim());
      count.textContent = passages.length + " passages";
    }}
  }};
  panel.querySelector("#obs-close").onclick = () => panel.remove();
  panel.querySelector("#obs-save").onclick = async () => {{
    const why = panel.querySelector("#obs-why").value.trim();
    const response = await fetch(endpoint, {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{
        source_url: location.href,
        source_title: document.title || location.href,
        captured_at: new Date().toISOString(),
        why,
        passages
      }})
    }});
    const result = await response.json();
    count.textContent = result.ok ? "Saved" : ("Error: " + result.error);
  }};
}})();
"""
    return "javascript:" + quote(" ".join(script.split()))
```

- [ ] **Step 5: Run capture tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_web_clip_capture_server -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/obsidian_intake_agent/web_clips/capture_server.py src/obsidian_intake_agent/web_clips/bookmarklet.py tests/test_web_clip_capture_server.py
git commit -m "feat: capture web clips locally"
```

---

### Task 4: Web Clip Processor

**Files:**
- Create: `src/obsidian_intake_agent/processors/web_clip_processor.py`
- Test: `tests/test_web_clip_processor.py`

- [ ] **Step 1: Write failing processor tests**

Create `tests/test_web_clip_processor.py`:

```python
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import config
from obsidian_intake_agent.processors.web_clip_processor import WebClipProcessor


class WebClipProcessorTests(unittest.TestCase):
    def test_dry_run_reports_planned_outputs_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            raw = vault / "00_Intake" / "Web Clips" / "2026-05-18 - Article.md"
            raw.parent.mkdir(parents=True)
            raw.write_text(_raw_note(), encoding="utf-8")
            processor = WebClipProcessor(config(vault, dry_run=False))

            result = processor.process_all(dry_run=True)

            self.assertEqual(result.processed_files, 1)
            self.assertFalse((vault / "10_References" / "Web Clips").exists())
            self.assertTrue(raw.exists())

    def test_process_writes_reference_note_and_archives_raw_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            raw = vault / "00_Intake" / "Web Clips" / "2026-05-18 - Article.md"
            raw.parent.mkdir(parents=True)
            raw.write_text(_raw_note(), encoding="utf-8")
            processor = WebClipProcessor(config(vault, dry_run=False))

            result = processor.process_all(dry_run=False)

            self.assertEqual(result.processed_files, 1)
            output = vault / "10_References" / "Web Clips" / "2026-05-18 - Article.md"
            archived = vault / "_Archive" / "Intake" / "Web Clips" / "2026-05-18 - Article.md"
            self.assertTrue(output.exists())
            self.assertTrue(archived.exists())
            self.assertFalse(raw.exists())
            text = output.read_text(encoding="utf-8")
            self.assertIn("type: web_clip", text)
            self.assertIn("> First exact passage.", text)
            self.assertIn("[[_Archive/Intake/Web Clips/2026-05-18 - Article.md]]", text)

    def test_rerun_is_idempotent_when_raw_capture_was_archived(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            raw = vault / "00_Intake" / "Web Clips" / "2026-05-18 - Article.md"
            raw.parent.mkdir(parents=True)
            raw.write_text(_raw_note(), encoding="utf-8")
            processor = WebClipProcessor(config(vault, dry_run=False))

            first = processor.process_all(dry_run=False)
            second = processor.process_all(dry_run=False)

            self.assertEqual(first.processed_files, 1)
            self.assertEqual(second.processed_files, 0)


def _raw_note() -> str:
    return """---
type: web_clip_intake
status: unprocessed
captured_at: "2026-05-18T14:30:00+00:00"
source_url: "https://example.com/article"
source_title: "Article"
---

# Article

Source: https://example.com/article

## Why This Matters

Use this for CRM adoption messaging.

## Captured Passages

> First exact passage.

> Second exact passage.
"""
```

- [ ] **Step 2: Run processor tests to verify failure**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_web_clip_processor -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement processor**

Create `src/obsidian_intake_agent/processors/web_clip_processor.py`:

```python
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from obsidian_intake_agent.config import Config
from obsidian_intake_agent.rendering.web_clip_renderer import render_processed_web_clip_note
from obsidian_intake_agent.utils.fs import safe_write_text
from obsidian_intake_agent.web_clips.models import ProcessedWebClip

FRONT_MATTER = re.compile(r"^---\n(?P<meta>.*?)\n---\n(?P<body>.*)$", re.DOTALL)
SIGNIFICANT_WORD = re.compile(r"[A-Za-z][A-Za-z0-9-]{3,}")
STOP_WORDS = {
    "about", "after", "also", "because", "been", "from", "have", "into", "more",
    "that", "their", "this", "with", "would", "your", "what", "when", "where",
}


@dataclass(slots=True)
class WebClipProcessingSummary:
    processed_files: int = 0
    written_reference_notes: int = 0
    archived_raw_captures: int = 0


class WebClipProcessor:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.intake_path = config.vault_path / config.web_clips_intake_dir
        self.references_path = config.vault_path / config.web_clips_references_dir
        self.archive_path = config.vault_path / config.archive_intake_dir / "Web Clips"

    def process_all(self, *, dry_run: bool | None = None) -> WebClipProcessingSummary:
        effective_dry_run = self.config.dry_run if dry_run is None else dry_run
        summary = WebClipProcessingSummary()
        if not self.intake_path.exists():
            return summary
        for path in sorted(self.intake_path.glob("*.md")):
            raw = path.read_text(encoding="utf-8")
            if "type: web_clip_intake" not in raw or "status: unprocessed" not in raw:
                continue
            processed = self._processed_clip_from_raw(path, raw)
            output_path = self.references_path / path.name
            archived_path = self.archive_path / path.name
            summary.processed_files += 1
            if effective_dry_run:
                print(f"DRY RUN — would write: {output_path}")
                print(f"DRY RUN — would archive: {path} -> {archived_path}")
                continue
            self.references_path.mkdir(parents=True, exist_ok=True)
            safe_write_text(output_path, render_processed_web_clip_note(processed))
            self.archive_path.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(archived_path))
            summary.written_reference_notes += 1
            summary.archived_raw_captures += 1
        return summary

    def _processed_clip_from_raw(self, path: Path, raw: str) -> ProcessedWebClip:
        metadata, body = _split_front_matter(raw)
        why = _section(body, "Why This Matters")
        passages = _passages(_section(body, "Captured Passages"))
        source_title = metadata.get("source_title", path.stem)
        source_url = metadata.get("source_url", "")
        captured_at = metadata.get("captured_at", "")
        topics = _topics(" ".join([source_title, why, " ".join(passages)]))
        archive_relative = f"{self.config.archive_intake_dir}/Web Clips/{path.name}"
        summary = passages[0] if passages else why
        application = why or "Review this clip when related work comes up."
        return ProcessedWebClip(
            source_url=source_url,
            source_title=source_title,
            captured_at=captured_at,
            why=why,
            summary=summary,
            passages=passages,
            topics=topics,
            application=application,
            related=[],
            intake_source=archive_relative,
        )


def _split_front_matter(raw: str) -> tuple[dict[str, str], str]:
    match = FRONT_MATTER.match(raw)
    if match is None:
        return {}, raw
    metadata: dict[str, str] = {}
    for line in match.group("meta").splitlines():
        key, separator, value = line.partition(":")
        if separator:
            metadata[key.strip()] = value.strip().strip('"')
    return metadata, match.group("body")


def _section(body: str, heading: str) -> str:
    pattern = re.compile(rf"^## {re.escape(heading)}\n\n(?P<content>.*?)(?=^## |\Z)", re.DOTALL | re.MULTILINE)
    match = pattern.search(body)
    return match.group("content").strip() if match else ""


def _passages(section_text: str) -> list[str]:
    passages: list[str] = []
    current: list[str] = []
    for line in section_text.splitlines():
        if line.startswith(">"):
            current.append(line.removeprefix(">").removeprefix(" "))
        elif current:
            passages.append("\n".join(current).strip())
            current = []
    if current:
        passages.append("\n".join(current).strip())
    return [passage for passage in passages if passage]


def _topics(text: str) -> list[str]:
    counts: dict[str, int] = {}
    for match in SIGNIFICANT_WORD.finditer(text.lower()):
        word = match.group(0)
        if word not in STOP_WORDS:
            counts[word] = counts.get(word, 0) + 1
    ranked = sorted(counts, key=lambda word: (-counts[word], word))
    return ranked[:5]
```

- [ ] **Step 4: Run processor tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_web_clip_processor -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/obsidian_intake_agent/processors/web_clip_processor.py tests/test_web_clip_processor.py
git commit -m "feat: process web clip intake notes"
```

---

### Task 5: Web Clip CLI

**Files:**
- Modify: `src/obsidian_intake_agent/main.py`
- Test: `tests/test_main.py`

- [ ] **Step 1: Write failing CLI tests**

Add these tests to `MainCliTests` in `tests/test_main.py`:

```python
    def test_web_clips_bookmarklet_prints_javascript_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            config_path = _write_config(repo, vault)

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                exit_code = main(["--config", str(config_path), "web-clips", "bookmarklet"])

            self.assertEqual(exit_code, 0)
            self.assertIn("javascript:", stdout.getvalue())
            self.assertIn("127.0.0.1:8765/capture", stdout.getvalue())

    def test_web_clips_process_prints_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            vault = repo / "vault"
            raw = vault / "00_Intake" / "Web Clips" / "2026-05-18 - Article.md"
            raw.parent.mkdir(parents=True)
            raw.write_text(
                '---\ntype: web_clip_intake\nstatus: unprocessed\ncaptured_at: "2026-05-18T14:30:00+00:00"\n'
                'source_url: "https://example.com"\nsource_title: "Article"\n---\n\n'
                '# Article\n\n## Why This Matters\n\nUse this.\n\n## Captured Passages\n\n> Exact passage.\n',
                encoding="utf-8",
            )
            config_path = _write_config(repo, vault)

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                exit_code = main(["--config", str(config_path), "web-clips", "process", "--dry-run"])

            self.assertEqual(exit_code, 0)
            output = stdout.getvalue()
            self.assertIn("web_clips_processed_files: 1", output)
            self.assertIn("DRY RUN", output)
```

- [ ] **Step 2: Run focused CLI tests to verify failure**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_main.MainCliTests.test_web_clips_bookmarklet_prints_javascript_url tests.test_main.MainCliTests.test_web_clips_process_prints_summary -v
```

Expected: FAIL with argparse invalid choice for `web-clips`.

- [ ] **Step 3: Add CLI parser and dispatch**

In `src/obsidian_intake_agent/main.py`, add imports:

```python
from .processors.web_clip_processor import WebClipProcessor
from .web_clips.bookmarklet import render_bookmarklet
from .web_clips.capture_server import run_capture_server
```

In `build_parser`, add before `graph_parser`:

```python
    web_clips_parser = subparsers.add_parser("web-clips", help="Capture and process web clips.")
    web_clips_subparsers = web_clips_parser.add_subparsers(dest="web_clips_command", required=True)
    web_clips_subparsers.add_parser("bookmarklet", help="Print the browser bookmarklet JavaScript URL.")
    web_clips_subparsers.add_parser("serve", help="Run the local web clip capture server.")
    web_clips_process_parser = web_clips_subparsers.add_parser(
        "process",
        help="Process raw web clip intake notes into reference notes.",
    )
    web_clips_process_parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Print planned web clip writes without modifying the vault.",
    )
```

In `main`, add before `if args.command == "graph":`:

```python
    if args.command == "web-clips":
        if args.web_clips_command == "bookmarklet":
            print(render_bookmarklet(host=config.web_clips_capture_host, port=config.web_clips_capture_port))
            return 0
        if args.web_clips_command == "serve":
            intake_dir = config.vault_path / config.web_clips_intake_dir
            print(f"web_clips_capture_server: http://{config.web_clips_capture_host}:{config.web_clips_capture_port}")
            print(f"web_clips_intake_dir: {intake_dir}")
            run_capture_server(
                host=config.web_clips_capture_host,
                port=config.web_clips_capture_port,
                intake_dir=intake_dir,
            )
            return 0
        if args.web_clips_command == "process":
            summary = WebClipProcessor(config).process_all(dry_run=args.dry_run)
            print(f"web_clips_processed_files: {summary.processed_files}")
            print(f"web_clips_written_reference_notes: {summary.written_reference_notes}")
            print(f"web_clips_archived_raw_captures: {summary.archived_raw_captures}")
            if summary.processed_files > 0 and not args.dry_run:
                _maybe_auto_commit(config, vault_source_name="web clips")
            return 0
```

- [ ] **Step 4: Run focused CLI tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_main.MainCliTests.test_web_clips_bookmarklet_prints_javascript_url tests.test_main.MainCliTests.test_web_clips_process_prints_summary -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/obsidian_intake_agent/main.py tests/test_main.py
git commit -m "feat: add web clip CLI commands"
```

---

### Task 6: Weekly Clip Retrieval

**Files:**
- Create: `src/obsidian_intake_agent/web_clips/retrieval.py`
- Modify: `src/obsidian_intake_agent/weekly.py`
- Test: `tests/test_web_clip_retrieval.py`
- Test: `tests/test_weekly.py`

- [ ] **Step 1: Write retrieval tests**

Create `tests/test_web_clip_retrieval.py`:

```python
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import config
from obsidian_intake_agent.web_clips.retrieval import find_relevant_web_clips


class WebClipRetrievalTests(unittest.TestCase):
    def test_find_relevant_web_clips_ranks_topic_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            clips = vault / "10_References" / "Web Clips"
            clips.mkdir(parents=True)
            (clips / "CRM Adoption.md").write_text(_clip("CRM Adoption", ["crm", "adoption"]), encoding="utf-8")
            (clips / "Cooking.md").write_text(_clip("Cooking", ["kitchen"]), encoding="utf-8")

            results = find_relevant_web_clips(
                config(vault, dry_run=False),
                query_text="This week includes CRM adoption planning and stakeholder messaging.",
            )

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].title, "CRM Adoption")
            self.assertIn("crm", results[0].reason)


def _clip(title: str, topics: list[str]) -> str:
    topics_yaml = "\n".join(f"  - {topic}" for topic in topics)
    return f"""---
type: web_clip
source_url: "https://example.com/{title}"
source_title: "{title}"
captured_at: "2026-05-18T14:30:00+00:00"
topics:
{topics_yaml}
related:
  []
---

# {title}

## Why This Matters

Use this for {title}.

## Summary

{title} summary.

## Captured Passages

> Exact passage about {title}.

## Application

Apply this to {title}.
"""
```

- [ ] **Step 2: Add weekly source bundle test**

Add this test to `WeeklySnapshotTests` in `tests/test_weekly.py`:

```python
    def test_build_source_bundle_includes_relevant_web_clips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            (vault / "07_Actions").mkdir(parents=True)
            (vault / "10_References" / "Web Clips").mkdir(parents=True)
            (vault / "07_Actions" / "2026-03-30.md").write_text(
                "- [ ] Plan CRM adoption messaging\n",
                encoding="utf-8",
            )
            (vault / "10_References" / "Web Clips" / "CRM Adoption.md").write_text(
                '---\ntype: web_clip\nsource_title: "CRM Adoption"\ntopics:\n  - crm\n  - adoption\n'
                'source_url: "https://example.com"\ncaptured_at: "2026-05-18T14:30:00+00:00"\n---\n\n'
                '# CRM Adoption\n\n## Summary\n\nRelevant saved clip.\n',
                encoding="utf-8",
            )

            bundle = build_weekly_source_bundle(
                _config(vault),
                monday=date(2026, 3, 30),
                run_date=date(2026, 4, 3),
                mode="briefing",
            )

            self.assertIn("Relevant Saved Clips", bundle)
            self.assertIn("[[10_References/Web Clips/CRM Adoption.md]]", bundle)
```

- [ ] **Step 3: Run retrieval and weekly tests to verify failure**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_web_clip_retrieval tests.test_weekly.WeeklySnapshotTests.test_build_source_bundle_includes_relevant_web_clips -v
```

Expected: FAIL with missing retrieval module or missing web clip source block.

- [ ] **Step 4: Implement retrieval**

Create `src/obsidian_intake_agent/web_clips/retrieval.py`:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from obsidian_intake_agent.config import Config

WORD = re.compile(r"[A-Za-z][A-Za-z0-9-]{3,}")
STOP_WORDS = {"about", "after", "also", "from", "have", "that", "this", "with", "would", "your"}


@dataclass(frozen=True, slots=True)
class RelevantWebClip:
    path: Path
    title: str
    score: int
    reason: str


def find_relevant_web_clips(config: Config, *, query_text: str) -> list[RelevantWebClip]:
    root = config.vault_path / config.web_clips_references_dir
    if not root.exists():
        return []
    query_terms = _terms(query_text)
    results: list[RelevantWebClip] = []
    for path in sorted(root.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        clip_terms = _terms(text)
        overlap = sorted(query_terms & clip_terms)
        if not overlap:
            continue
        title = _title_from_note(text) or path.stem
        reason_terms = ", ".join(overlap[:3])
        results.append(
            RelevantWebClip(
                path=path,
                title=title,
                score=len(overlap),
                reason=f"Matches current weekly context on: {reason_terms}.",
            )
        )
    return sorted(results, key=lambda item: (-item.score, item.title))[: config.web_clips_max_weekly_results]


def render_relevant_web_clips_source(config: Config, *, query_text: str) -> str:
    clips = find_relevant_web_clips(config, query_text=query_text)
    if not clips:
        return ""
    lines = ["## Source: Relevant Saved Clips", ""]
    for clip in clips:
        relative = clip.path.relative_to(config.vault_path).as_posix()
        lines.append(f"- [[{relative}]] — {clip.reason}")
    return "\n".join(lines)


def _terms(text: str) -> set[str]:
    return {match.group(0).lower() for match in WORD.finditer(text) if match.group(0).lower() not in STOP_WORDS}


def _title_from_note(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line.removeprefix("# ").strip()
    return None
```

- [ ] **Step 5: Modify weekly source bundle**

In `src/obsidian_intake_agent/weekly.py`, add import:

```python
from .web_clips.retrieval import render_relevant_web_clips_source
```

In `build_weekly_source_bundle`, after gathering actions, previous review, and meetings, add:

```python
    base_bundle = "\n\n".join(parts)
    relevant_clips = render_relevant_web_clips_source(config, query_text=base_bundle)
    if relevant_clips:
        parts.append(relevant_clips)
```

Then keep the final `if not parts` and `return "\n\n".join(parts)` behavior. Ensure `base_bundle` is computed before the `if not parts` check so an empty weekly context does not surface arbitrary clips.

- [ ] **Step 6: Run retrieval and weekly tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest tests.test_web_clip_retrieval tests.test_weekly.WeeklySnapshotTests.test_build_source_bundle_includes_relevant_web_clips -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/obsidian_intake_agent/web_clips/retrieval.py src/obsidian_intake_agent/weekly.py tests/test_web_clip_retrieval.py tests/test_weekly.py
git commit -m "feat: surface relevant web clips in weekly context"
```

---

### Task 7: README And Smoke Coverage

**Files:**
- Modify: `README.md`
- Modify: `scripts/smoke_cli.py`
- Test: existing smoke command

- [ ] **Step 1: Add README web clip documentation**

In `README.md`, update the opening sentence to include web clips:

```markdown
`obsidian_intake_agent` watches or processes files from an Obsidian intake folder, writes canonical meeting notes into `01_Meetings`, appends a weekly actions note in `07_Actions`, can update weekly review notes in `09_Weekly Reviews`, and can capture web clips into `10_References`.
```

Add this command section after weekly commands:

````markdown
Print the browser bookmarklet:

```bash
obsidian-agent web-clips bookmarklet
```

Run the local web clip capture server:

```bash
obsidian-agent web-clips serve
```

Process raw web clips into reference notes:

```bash
obsidian-agent web-clips process --dry-run
obsidian-agent web-clips process
```
````

Add this behavior note near the existing behavior bullets:

```markdown
- Raw web clip captures are written to `vault_path/00_Intake/Web Clips`.
- Processed web clip reference notes are written to `vault_path/10_References/Web Clips`.
- Processed raw captures are archived under `archive_intake_dir/Web Clips`.
- Weekly briefing and wrap generation may include a bounded "Relevant Saved Clips" source block when processed clips match the current weekly context.
```

- [ ] **Step 2: Inspect smoke script**

Run:

```bash
sed -n '1,260p' scripts/smoke_cli.py
```

Expected: identify the temporary vault setup and CLI assertions.

- [ ] **Step 3: Add smoke assertions for web clip processing**

Modify `scripts/smoke_cli.py` to create a raw web clip intake note in the temporary vault, run:

```bash
obsidian-agent web-clips process
```

through the script's existing CLI invocation helper, then assert:

```python
assert (vault / "10_References" / "Web Clips" / "2026-05-18 - Smoke Web Clip.md").exists()
assert (vault / "z_Archive" / "Intake" / "Web Clips" / "2026-05-18 - Smoke Web Clip.md").exists()
```

Use the same config path and subprocess pattern already present in the smoke script.

- [ ] **Step 4: Run smoke**

Run:

```bash
make smoke
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add README.md scripts/smoke_cli.py
git commit -m "docs: document web clip workflow"
```

---

### Task 8: Full Validation

**Files:**
- No new files expected.

- [ ] **Step 1: Run focused web clip tests**

Run:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest \
  tests.test_web_clip_renderer \
  tests.test_web_clip_capture_server \
  tests.test_web_clip_processor \
  tests.test_web_clip_retrieval \
  -v
```

Expected: PASS.

- [ ] **Step 2: Run broader tests**

Run:

```bash
make test
```

Expected: PASS.

- [ ] **Step 3: Run lint, typecheck, and format checks**

Run:

```bash
make check
```

Expected: PASS.

- [ ] **Step 4: Run smoke and build**

Run:

```bash
make smoke
make build
```

Expected: PASS.

- [ ] **Step 5: Inspect final diff**

Run:

```bash
git status --short
git diff --stat HEAD
```

Expected: only intentional web clip files and docs are changed.

- [ ] **Step 6: Final implementation commit if previous task commits were skipped**

If the executing worker did not commit after each task, make one focused commit:

```bash
git add src tests scripts README.md config.example.yaml
git commit -m "feat: add local web clipper"
```

---

### Task 9: GitHub Issue Creation After Plan Approval

**Files:**
- No repo files required.

- [ ] **Step 1: Create epic issue**

Run after this plan is approved and before implementation starts:

```bash
gh issue create \
  --title "Epic: Local Obsidian web clipper" \
  --body "Build the local web clipper described in docs/superpowers/specs/2026-05-18-web-clipper-design.md and docs/superpowers/plans/2026-05-18-web-clipper.md. Scope: bookmarklet capture, localhost capture endpoint, raw web clip intake, processed reference notes, and weekly briefing/wrap surfacing."
```

Expected: GitHub prints the new issue URL.

- [ ] **Step 2: Create focused implementation issues**

Create issues using these titles and bodies:

```bash
gh issue create --title "Add web clip config defaults" --body "Add web clip intake/reference directories, localhost capture host/port, and max weekly result configuration. Include config tests and config.example.yaml updates."
gh issue create --title "Render raw and processed web clip notes" --body "Add web clip models and Markdown renderers for raw intake captures and processed reference notes. Preserve passages verbatim."
gh issue create --title "Add local web clip capture endpoint and bookmarklet" --body "Implement the localhost capture server, payload validation, filename sanitization, raw note writing, and bookmarklet JavaScript generation."
gh issue create --title "Process web clip intake into reference notes" --body "Add the web clip processor, dry-run behavior, deterministic enrichment, canonical note writing, raw capture archiving, and idempotence tests."
gh issue create --title "Add web clip CLI commands" --body "Add web-clips bookmarklet, serve, and process commands to obsidian-agent with focused CLI tests."
gh issue create --title "Surface relevant web clips in weekly context" --body "Add deterministic retrieval over processed web clips and include up to the configured limit in weekly briefing/wrap source bundles."
gh issue create --title "Document and smoke-test web clip workflow" --body "Update README and smoke coverage for the local web clip workflow."
```

Expected: GitHub prints one issue URL per command.

---

## Self-Review Notes

- Spec coverage:
  - Capture one or more passages: Task 3.
  - Preserve passages exactly: Tasks 2 and 4.
  - Raw folder `00_Intake/Web Clips`: Tasks 1, 3, and 4.
  - Processed folder `10_References/Web Clips`: Tasks 1 and 4.
  - Weekly surfacing first: Task 6.
  - No semantic retrieval or paid dependency: Implementation decisions and Task 6 deterministic retrieval.
  - Browser extension deferred: architecture uses bookmarklet and localhost server only.
  - GitHub issue timing: Task 9 after plan approval.
- Placeholder scan:
  - No incomplete markers or vague commands remain.
- Type consistency:
  - `WebClipCapture`, `WebClipPassage`, `ProcessedWebClip`, `WebClipProcessor`, `WebClipProcessingSummary`, and `RelevantWebClip` are defined before use.
  - Config names match the approved spec and are used consistently across config, CLI, processor, and retrieval tasks.
