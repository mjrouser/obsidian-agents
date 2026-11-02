"""Microsoft Graph HTTP transport, lookups, and error/permission messages for meeting sync.

Split out of ``meetings/sync.py``; ``sync`` re-exports these names for existing callers.
Tests patch ``urlopen`` on this module.
"""

from __future__ import annotations

import binascii
import json
import socket
from base64 import urlsafe_b64decode
from collections.abc import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .graph_parsing import _graph_value_items
from .sync_models import _optional_string

GRAPH_REQUEST_TIMEOUT_SECONDS = 30
GRAPH_REQUEST_TIMEOUT_ATTEMPTS = 2


class MeetingSyncGraphTimeoutError(RuntimeError):
    def __init__(self, *, operation: str, timeout_seconds: int) -> None:
        self.operation = operation
        self.timeout_seconds = timeout_seconds
        super().__init__(f"Microsoft Graph did not respond within {timeout_seconds} seconds while {operation}.")


def _is_timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError | socket.timeout):
        return True
    if isinstance(exc, URLError):
        reason = exc.reason
        if isinstance(reason, TimeoutError | socket.timeout):
            return True
        return "timed out" in str(reason).lower()
    return "timed out" in str(exc).lower()


class GraphOnlineMeetingNotFoundError(ValueError):
    pass


def _resolve_graph_online_meeting_id(
    *,
    api_base_url: str,
    user_path: str,
    access_token: str,
    join_url: str,
    fetch_json: Callable[[str, str], dict[str, object]],
) -> str:
    lookup_url = f"{api_base_url}{user_path}/onlineMeetings?" + urlencode(
        {
            "$filter": f"JoinWebUrl eq '{_escape_graph_odata_literal(join_url)}'",
        }
    )
    payload = fetch_json(lookup_url, access_token)
    try:
        items = _graph_value_items(payload)
    except ValueError as exc:
        raise ValueError("Graph online meeting lookup response did not contain a value list.") from exc
    if not items:
        raise GraphOnlineMeetingNotFoundError("Graph did not find an online meeting record for this Outlook event.")
    raw_id = items[0].get("id")
    meeting_id = _optional_string(raw_id if isinstance(raw_id, str) else None)
    if meeting_id is None:
        raise ValueError("Graph online meeting lookup did not return an id.")
    return meeting_id


def _resolve_graph_me_user_id(
    *,
    api_base_url: str,
    access_token: str,
    fetch_json: Callable[[str, str], dict[str, object]],
) -> str:
    payload = fetch_json(f"{api_base_url}/me?$select=id", access_token)
    raw_id = payload.get("id") if isinstance(payload, dict) else None
    user_id = _optional_string(str(raw_id or ""))
    if user_id is None:
        raise ValueError("Graph /me lookup did not return a user id.")
    return user_id


def _escape_graph_odata_literal(value: str) -> str:
    return value.replace("'", "''")


def _graph_resource_path_id(value: str) -> str:
    return quote(value, safe=":@*")


def _fetch_graph_json(
    url: str,
    access_token: str,
    *,
    prefer_outlook_timezone: bool = False,
) -> dict[str, object]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    if prefer_outlook_timezone:
        headers["Prefer"] = 'outlook.timezone="UTC"'
    request = Request(
        url,
        headers=headers,
    )
    response_bytes = _read_graph_response_bytes(request, operation="Microsoft Graph JSON request")
    return json.loads(response_bytes.decode("utf-8"))


def _fetch_graph_bytes(url: str, access_token: str) -> bytes:
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "text/vtt",
        },
    )
    return _read_graph_response_bytes(request, operation="Microsoft Graph content request")


def _read_graph_response_bytes(request: Request, *, operation: str) -> bytes:
    last_timeout: BaseException | None = None
    for _attempt in range(GRAPH_REQUEST_TIMEOUT_ATTEMPTS):
        try:
            with urlopen(request, timeout=GRAPH_REQUEST_TIMEOUT_SECONDS) as response:
                return response.read()
        except (TimeoutError, socket.timeout, URLError) as exc:
            if not _is_timeout_error(exc):
                raise
            last_timeout = exc
    raise MeetingSyncGraphTimeoutError(
        operation=operation,
        timeout_seconds=GRAPH_REQUEST_TIMEOUT_SECONDS,
    ) from last_timeout


def _graph_http_warning(error: HTTPError) -> str:
    if error.code in {401, 403}:
        return _graph_http_error_detail(
            error,
            default=(
                f"Graph calendar discovery failed with HTTP {error.code}. "
                "Check delegated calendar/transcript permissions and refresh Graph auth with `obsidian-agent graph login`."
            ),
        )
    return _graph_http_error_detail(
        error,
        default=f"Graph calendar discovery failed with HTTP {error.code}.",
    )


def _graph_calendar_scope_warning(access_token: str) -> str | None:
    claims = _decode_jwt_claims(access_token)
    if claims is None:
        return None

    grants = _claim_grants(claims.get("scp")) | _claim_grants(claims.get("roles"))
    if not grants:
        return None

    calendar_grants = {
        "Calendars.ReadBasic",
        "Calendars.Read",
        "Calendars.Read.Shared",
        "Calendars.ReadWrite",
        "Calendars.ReadWrite.Shared",
    }
    if grants & calendar_grants:
        return None

    grant_summary = _format_graph_grants(grants)
    return (
        "Graph token is missing delegated Microsoft Graph calendar permission. "
        "Calendar discovery needs Calendars.Read for full meeting context; "
        "Calendars.ReadBasic may only support limited metadata. "
        "Request a token with https://graph.microsoft.com/Calendars.Read. "
        f"Current token grants: {grant_summary}."
    )


def _decode_jwt_claims(access_token: str) -> dict[str, object] | None:
    parts = access_token.split(".")
    if len(parts) < 2:
        return None

    payload = parts[1] + ("=" * (-len(parts[1]) % 4))
    try:
        decoded = urlsafe_b64decode(payload.encode()).decode()
        claims = json.loads(decoded)
    except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    return claims if isinstance(claims, dict) else None


def _claim_grants(raw_value: object) -> set[str]:
    if isinstance(raw_value, str):
        return {part for part in raw_value.split() if part}
    if isinstance(raw_value, list):
        return {str(part) for part in raw_value if str(part).strip()}
    return set()


def _format_graph_grants(grants: set[str]) -> str:
    sorted_grants = sorted(grants)
    if len(sorted_grants) <= 8:
        return ", ".join(sorted_grants)
    shown = ", ".join(sorted_grants[:8])
    return f"{shown}, ... ({len(sorted_grants)} total)"


def _graph_http_error_detail(error: HTTPError, *, default: str) -> str:
    message = _graph_http_error_message(error)
    if message is None:
        return default
    return f"{default} Graph said: {message}"


def _graph_http_error_message(error: HTTPError) -> str | None:
    if error.fp is None:
        return None
    try:
        body = error.fp.read()
    except OSError:
        return None
    if not body:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        text = body.decode("utf-8", errors="ignore").strip()
        return text or None
    if not isinstance(payload, dict):
        return None
    top_level_message = _optional_string(payload.get("message") if isinstance(payload.get("message"), str) else None)
    if top_level_message is not None:
        return top_level_message
    raw_error = payload.get("error")
    if not isinstance(raw_error, dict):
        return None
    code = _optional_string(raw_error.get("code") if isinstance(raw_error.get("code"), str) else None)
    message = _optional_string(raw_error.get("message") if isinstance(raw_error.get("message"), str) else None)
    if code and message:
        return f"{code}: {message}"
    return message or code
