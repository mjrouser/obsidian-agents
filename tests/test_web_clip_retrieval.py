from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from obsidian_intake_agent.web_clips.retrieval import find_relevant_web_clips
from tests.helpers import config


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

    def test_find_relevant_web_clips_skips_symlink_outside_vault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            vault = tmp_path / "vault"
            clips = vault / "10_References" / "Web Clips"
            outside = tmp_path / "outside"
            clips.mkdir(parents=True)
            outside.mkdir()
            outside_clip = outside / "Outside CRM.md"
            outside_clip.write_text(_clip("Outside CRM", ["crm", "adoption"]), encoding="utf-8")
            try:
                (clips / "Outside CRM.md").symlink_to(outside_clip)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")

            results = find_relevant_web_clips(
                config(vault, dry_run=False),
                query_text="CRM adoption planning",
            )

            self.assertEqual(results, [])

    def test_find_relevant_web_clips_skips_malformed_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir) / "vault"
            clips = vault / "10_References" / "Web Clips"
            clips.mkdir(parents=True)
            (clips / "Broken.md").write_text(
                '---\nsource_title: "Broken"\ntopics: [crm\n---\n\n# Broken\n\ncrm adoption\n',
                encoding="utf-8",
            )
            (clips / "CRM Adoption.md").write_text(_clip("CRM Adoption", ["crm", "adoption"]), encoding="utf-8")

            results = find_relevant_web_clips(
                config(vault, dry_run=False),
                query_text="CRM adoption planning",
            )

            self.assertEqual([result.title for result in results], ["CRM Adoption"])


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
