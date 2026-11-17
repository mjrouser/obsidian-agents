"""Microsoft Graph fallback meeting summary client.

Builds a fallback recap from Copilot AI insights when no processor-ready
transcript is available, and renders it as Markdown with recap provenance.
Occurrence-scoped Teams meeting chat is not fetched, so the rendered recap omits
chat enrichment. ``meetings.sync`` re-exports these names.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse

from .bundle_paths import (
    _fallback_summary_relative_path,
    _normalized_bundle_title,
)
from .graph_http import (
    GraphOnlineMeetingNotFoundError,
    _fetch_graph_json,
    _graph_http_error_detail,
    _graph_resource_path_id,
    _resolve_graph_me_user_id,
    _resolve_graph_online_meeting_id,
)
from .graph_parsing import (
    _graph_ai_action_items,
    _graph_ai_mentions,
    _graph_meeting_notes,
)
from .occurrence_assignment import (
    selection_bounds,
)
from .recap_provenance import RECAP_SOURCE, create_recap_provenance, read_recap_provenance
from .recap_selection import select_recap, validate_detail
from .recap_validation import (
    _recap_occurrences,
)
from .sync_models import (
    ArtifactStatus,
    MeetingArtifact,
    OutlookMeetingCandidate,
)


class GraphMeetingFallbackSummaryClient:
    uses_network = True
    provides_summary_fallback = True

    def __init__(
        self,
        *,
        access_token: str,
        intake_root: Path,
        api_base_url: str = "https://graph.microsoft.com/v1.0",
        user_id: str | None = None,
        fetch_json: Callable[[str, str], dict[str, object]] | None = None,
    ) -> None:
        self._access_token = access_token
        self._intake_root = intake_root
        self._api_base_url = api_base_url.rstrip("/")
        self._graph_user_path = f"/users/{quote(user_id, safe='')}" if user_id else "/me"
        self._copilot_user_id = user_id
        self._fetch_json = fetch_json or _fetch_graph_json

    def discover_artifacts(
        self,
        *,
        meeting: OutlookMeetingCandidate,
    ) -> tuple[MeetingArtifact, ...]:
        if _meeting_has_processor_ready_transcript_source(meeting):
            return (
                MeetingArtifact(
                    "Copilot recap / AI summary",
                    "not_attempted",
                    "Transcript-priority source is already available; recap fallback was not needed.",
                ),
                MeetingArtifact(
                    "Teams meeting chat",
                    "not_attempted",
                    "Transcript-priority source is already available; chat fallback was not needed.",
                ),
            )

        fallback_path = self._intake_root / _fallback_summary_relative_path(meeting)
        target, context = _recap_occurrences(meeting)
        if fallback_path.exists() or fallback_path.is_symlink():
            try:
                provenance = read_recap_provenance(fallback_path, target, context)
                return (
                    MeetingArtifact(
                        RECAP_SOURCE,
                        "available",
                        "Validated cached recap.",
                        matched_paths=(fallback_path,),
                        recap_provenance=provenance,
                    ),
                )
            except ValueError as exc:
                return (
                    MeetingArtifact(
                        RECAP_SOURCE,
                        "not_attempted",
                        f"manual_review_required: {exc}",
                        recap_diagnostics={"reason": "unverified_cached_recap"},
                    ),
                )

        join_url = meeting.detected_join_url()
        if join_url is None:
            detail = "Outlook metadata did not include a Teams join URL."
            return (
                MeetingArtifact("Copilot recap / AI summary", "not_attempted", detail),
                MeetingArtifact("Teams meeting chat", "not_attempted", detail),
            )

        try:
            online_meeting_id = _resolve_graph_online_meeting_id(
                api_base_url=self._api_base_url,
                user_path=self._graph_user_path,
                access_token=self._access_token,
                join_url=join_url,
                fetch_json=self._fetch_json,
            )
        except HTTPError as exc:
            recap_artifact = self._graph_fallback_http_error_artifact(
                exc,
                source_name="Copilot recap / AI summary",
                action="online meeting lookup",
            )
            return (
                recap_artifact,
                MeetingArtifact("Teams meeting chat", "not_attempted", "Recap lookup did not complete."),
            )
        except GraphOnlineMeetingNotFoundError:
            return (
                MeetingArtifact(
                    "Copilot recap / AI summary",
                    "missing",
                    "Graph did not find an online meeting record for this Outlook event.",
                ),
                MeetingArtifact(
                    "Teams meeting chat",
                    "not_attempted",
                    "No online meeting record was available for meeting chat lookup.",
                ),
            )
        except (URLError, ValueError) as exc:
            return (
                MeetingArtifact(
                    "Copilot recap / AI summary",
                    "not_attempted",
                    f"Graph recap discovery failed: {exc}",
                ),
                MeetingArtifact("Teams meeting chat", "not_attempted", "Recap lookup did not complete."),
            )

        try:
            user_id = self._copilot_user_id or _resolve_graph_me_user_id(
                api_base_url=self._api_base_url,
                access_token=self._access_token,
                fetch_json=self._fetch_json,
            )
            records = self._recap_records(user_id=user_id, online_meeting_id=online_meeting_id)
            selection = select_recap(records, target, context)
            diagnostics: dict[str, object] = {
                "reason": selection.reason,
                "candidate_count": selection.candidate_count,
                "rejected_ids": list(selection.rejected),
                "conflicting_occurrences": list(selection.conflicts),
                "occurrence_id": target.occurrence_id,
                "selection_start_at": selection_bounds(target)[0].isoformat(),
                "selection_end_at": selection_bounds(target)[1].isoformat(),
                "context_start_at": min(item.scheduled_start for item in context).isoformat(),
                "context_end_at": max(item.scheduled_end for item in context).isoformat(),
            }
            if selection.selected is None:
                status: ArtifactStatus = "missing" if selection.reason == "no_matching_recap" else "not_attempted"
                return (MeetingArtifact(RECAP_SOURCE, status, selection.reason, recap_diagnostics=diagnostics),)
            detail_url = f"{self._recap_list_url(user_id, online_meeting_id)}/{_graph_resource_path_id(str(selection.selected['id']))}"
            detail_cache = getattr(self, "_recap_detail_cache", {})
            ai_insight = detail_cache.get(detail_url)
            if ai_insight is None:
                ai_insight = self._fetch_json(detail_url, self._access_token)
            selected_metadata = validate_detail(selection.selected, ai_insight)
            detail_cache[detail_url] = ai_insight
            self._recap_detail_cache = detail_cache
            diagnostics["selected_insight"] = selected_metadata
        except HTTPError as exc:
            recap_artifact = self._graph_fallback_http_error_artifact(
                exc,
                source_name="Copilot recap / AI summary",
                action="recap discovery",
            )
            return (
                recap_artifact,
                MeetingArtifact(
                    "Teams meeting chat",
                    "not_attempted",
                    "Chat lookup was skipped because no recap was available.",
                ),
            )
        except (URLError, ValueError) as exc:
            return (
                MeetingArtifact(
                    "Copilot recap / AI summary",
                    "not_attempted",
                    f"Graph recap discovery failed: {exc}",
                ),
                MeetingArtifact(
                    "Teams meeting chat",
                    "not_attempted",
                    "Chat lookup was skipped because no recap was available.",
                ),
            )

        if ai_insight is None:
            return (
                MeetingArtifact(
                    "Copilot recap / AI summary",
                    "missing",
                    "Graph recap discovery returned no AI insights for this meeting.",
                ),
                MeetingArtifact(
                    "Teams meeting chat",
                    "not_attempted",
                    "Chat lookup was skipped because no recap was available.",
                ),
            )

        chat_artifact = MeetingArtifact(
            "Teams meeting chat",
            "not_attempted",
            "Occurrence-scoped chat is unavailable; recurring-thread enrichment omitted.",
        )
        chat_messages: tuple[dict[str, str], ...] = ()
        planned_content = _render_fallback_summary_markdown(
            meeting=meeting,
            ai_insight=ai_insight,
            chat_messages=chat_messages,
            chat_artifact=chat_artifact,
        )
        return (
            MeetingArtifact(
                "Copilot recap / AI summary",
                "available",
                "Discovered Copilot recap fallback summary; local persistence is planned for explicit bundle write.",
                matched_paths=(fallback_path,),
                planned_content=planned_content,
                recap_provenance=create_recap_provenance(
                    content=planned_content.encode("utf-8"),
                    target=target,
                    context=context,
                    insight=selected_metadata,
                    online_meeting_id=online_meeting_id,
                ),
                recap_diagnostics=diagnostics,
            ),
            chat_artifact,
        )

    def reset_run_cache(self) -> None:
        self._recap_cache: dict[str, list[dict[str, object]]] = {}
        self._recap_detail_cache = {}

    def _recap_list_url(self, user_id: str, online_meeting_id: str) -> str:
        return (
            f"{self._api_base_url}/copilot/users/{quote(user_id, safe='')}/onlineMeetings/"
            f"{_graph_resource_path_id(online_meeting_id)}/aiInsights"
        )

    def _recap_records(self, *, user_id: str, online_meeting_id: str) -> list[dict[str, object]]:
        url = self._recap_list_url(user_id, online_meeting_id)
        cache = getattr(self, "_recap_cache", {})
        if url in cache:
            return cache[url]
        records: list[dict[str, object]] = []
        seen: set[str] = set()
        next_url: str | None = url
        expected = urlparse(self._api_base_url)
        while next_url:
            parsed = urlparse(next_url)
            if (
                parsed.scheme != "https"
                or parsed.username
                or parsed.password
                or parsed.fragment
                or (parsed.hostname, parsed.port or 443) != (expected.hostname, expected.port or 443)
                or next_url in seen
            ):
                raise ValueError("recap_discovery_incomplete: unsafe pagination URL")
            seen.add(next_url)
            page = self._fetch_json(next_url, self._access_token)
            if not isinstance(page, dict):
                raise ValueError("recap_discovery_incomplete: malformed page")
            rows = page.get("value")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("recap_discovery_incomplete: malformed page")
            records.extend(rows)
            link = page.get("@odata.nextLink")
            if link is not None and (not isinstance(link, str) or not link):
                raise ValueError("recap_discovery_incomplete: malformed nextLink")
            next_url = link
        cache[url] = records
        self._recap_cache = cache
        return records

    def _graph_fallback_http_error_artifact(
        self,
        error: HTTPError,
        *,
        source_name: str,
        action: str,
    ) -> MeetingArtifact:
        if error.code in {401, 403}:
            status: ArtifactStatus = "permission_blocked"
        elif error.code == 404:
            status = "missing"
        else:
            status = "not_attempted"
        return MeetingArtifact(
            source_name,
            status,
            _graph_http_error_detail(error, default=f"Graph {action} failed with HTTP {error.code}."),
        )


def _meeting_has_processor_ready_transcript_source(meeting: OutlookMeetingCandidate) -> bool:
    for artifact in meeting.discovered_artifacts:
        if (
            artifact.source_name in {"Teams .vtt transcript", "Teams transcript text"}
            and artifact.status == "available"
            and artifact.matched_paths
        ):
            return True
    return False


def _render_fallback_summary_markdown(
    *,
    meeting: OutlookMeetingCandidate,
    ai_insight: dict[str, object],
    chat_messages: tuple[dict[str, str], ...],
    chat_artifact: MeetingArtifact,
) -> str:
    lines = [
        f"# {meeting.start_at.date().isoformat()} - Teams - {_normalized_bundle_title(meeting.subject)}",
        "",
        "## Context",
        f"- Subject: {meeting.subject}",
        f"- Start: {meeting.start_at.isoformat()}",
        f"- End: {meeting.end_at.isoformat()}",
        f"- Organizer: {meeting.organizer or 'Unknown'}",
        f"- Your Response: {meeting.response_status or 'Unknown'}",
        "",
        "## Summary",
    ]
    meeting_notes = _graph_meeting_notes(ai_insight)
    if meeting_notes:
        lines.extend(meeting_notes)
    else:
        lines.append("- No AI-generated meeting notes were returned.")

    lines.extend(["", "## Action Items"])
    action_items = _graph_ai_action_items(ai_insight)
    if action_items:
        lines.extend(action_items)
    else:
        lines.append("- No AI-generated action items were returned.")

    lines.extend(["", "## Mentions"])
    mentions = _graph_ai_mentions(ai_insight)
    if mentions:
        lines.extend(mentions)
    else:
        lines.append("- No AI-generated mentions were returned.")

    lines.extend(["", "## Meeting Chat"])
    if chat_messages:
        for message in chat_messages:
            lines.append(f"- {message['sender']}: {message['text']}")
    else:
        if chat_artifact.detail:
            lines.append(f"- {chat_artifact.status}: {chat_artifact.detail}")
        else:
            lines.append("- No meeting chat context was available.")

    lines.extend(
        [
            "",
            "## Source",
            "- Source Used: Copilot recap / AI summary",
        ]
    )
    if chat_artifact.status == "available":
        lines.append("- Source Used: Teams meeting chat")
    lines.append("- Source Used: Outlook calendar metadata")
    lines.append("- Attendance Confidence: partial_visibility")
    lines.append(
        "- Source Limitation: Fallback artifact synthesized from summary-oriented meeting sources, not a verbatim transcript."
    )
    return "\n".join(lines) + "\n"
