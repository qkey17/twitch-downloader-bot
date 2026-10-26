from typing import Optional, Any, Dict, List, Tuple
from src.config import VIDEO_COMMENTS_HASH
import aiohttp

from .client import TwitchAPIError, gql_post_json

async def gql_fetch_page(
    session: aiohttp.ClientSession,
    client_id: str,
    vod_id: str,
    cursor: Optional[str],
    start_offset: int,
    page_number: int = 1,
) -> Tuple[List[dict], Optional[str], bool]:
    headers = {
        "Client-Id": client_id,
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json",
    }
    variables: Dict[str, Any] = {"videoID": vod_id}
    if cursor:
        variables["cursor"] = cursor
    else:
        variables["contentOffsetSeconds"] = int(start_offset)

    payload = [{
        "operationName": "VideoCommentsByOffsetOrCursor",
        "variables": variables,
        "extensions": {
            "persistedQuery": {"version": 1, "sha256Hash": VIDEO_COMMENTS_HASH},
        },
    }]

    data = await gql_post_json(session, headers, payload, page_number=page_number)
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise TwitchAPIError("Twitch returned an unexpected comments response")

    response_data = data[0].get("data")
    if not isinstance(response_data, dict):
        raise TwitchAPIError("Twitch comments response has no data object")
    video = response_data.get("video")
    if not isinstance(video, dict):
        raise TwitchAPIError("Twitch VOD was not found or is unavailable")
    comments = video.get("comments")
    if not isinstance(comments, dict):
        raise TwitchAPIError("Twitch comments response has no comments object")

    edges = comments.get("edges")
    page_info = comments.get("pageInfo")
    if not isinstance(edges, list) or not isinstance(page_info, dict):
        raise TwitchAPIError("Twitch returned an unexpected comments page")
    if any(not isinstance(edge, dict) or not isinstance(edge.get("node"), dict) for edge in edges):
        raise TwitchAPIError("Twitch returned an invalid comment entry")
    # A missing message must not be silently skipped as blank text. Validate
    # the entire page before emitting any rows, preserving optional metadata.
    for edge in edges:
        message = edge["node"].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("fragments"), list):
            raise TwitchAPIError("Twitch returned incomplete comment message data")
        if any(not isinstance(fragment, dict) or not isinstance(fragment.get("text"), str)
               for fragment in message["fragments"]):
            raise TwitchAPIError("Twitch returned invalid comment fragments")

    has_next_value = page_info.get("hasNextPage")
    if not isinstance(has_next_value, bool):
        raise TwitchAPIError("Twitch returned invalid comments pagination metadata")
    has_next = has_next_value
    next_cursor = edges[-1].get("cursor") if edges else None
    if has_next and (not isinstance(next_cursor, str) or not next_cursor):
        raise TwitchAPIError("Twitch comments page has no continuation cursor")

    return edges, next_cursor, has_next
