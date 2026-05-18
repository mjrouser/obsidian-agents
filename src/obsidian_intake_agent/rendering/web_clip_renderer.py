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
    related = "\n".join(f'  - "{_yaml_escape(item)}"' for item in clip.related)
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
