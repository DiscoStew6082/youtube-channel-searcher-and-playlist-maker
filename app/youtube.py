import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx


YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"


class YouTubeAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChannelRef:
    kind: str
    value: str


def parse_channel_ref(raw: str) -> ChannelRef:
    value = raw.strip()
    if not value:
        raise ValueError("Paste a YouTube channel URL, handle, or channel ID.")

    if re.fullmatch(r"UC[\w-]{20,}", value):
        return ChannelRef("id", value)

    if value.startswith("@"):
        return ChannelRef("handle", value)

    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = parsed.netloc.lower().removeprefix("www.")
    if host not in {"youtube.com", "m.youtube.com", "youtu.be"}:
        raise ValueError("That does not look like a YouTube channel URL.")

    path_parts = [part for part in parsed.path.split("/") if part]
    if not path_parts:
        raise ValueError("The YouTube URL does not include a channel.")

    first = path_parts[0]
    if first.startswith("@"):
        return ChannelRef("handle", first)
    if first == "channel" and len(path_parts) >= 2:
        return ChannelRef("id", path_parts[1])
    if first in {"c", "user"} and len(path_parts) >= 2:
        return ChannelRef("query", path_parts[1])

    query = parse_qs(parsed.query)
    if "channel_id" in query and query["channel_id"]:
        return ChannelRef("id", query["channel_id"][0])

    return ChannelRef("query", path_parts[-1])


class YouTubeClient:
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=30)

    def resolve_channel(self, raw: str) -> dict[str, Any]:
        ref = parse_channel_ref(raw)
        if ref.kind == "id":
            channels = self._channels_by_id(ref.value)
            if channels:
                return channels[0]
            raise YouTubeAPIError("No channel found for that channel ID.")

        if ref.kind == "handle":
            channels = self._channels_by_handle(ref.value)
            if channels:
                return channels[0]

        channels = self._search_channels(ref.value.lstrip("@"))
        if channels:
            channel_id = channels[0]["id"]["channelId"]
            full = self._channels_by_id(channel_id)
            if full:
                return full[0]

        raise YouTubeAPIError("No channel found for that URL or handle.")

    def fetch_channel_videos(self, uploads_playlist_id: str) -> list[dict[str, Any]]:
        video_ids = self._playlist_video_ids(uploads_playlist_id)
        videos: list[dict[str, Any]] = []
        for index in range(0, len(video_ids), 50):
            batch = video_ids[index : index + 50]
            response = self._get(
                "videos",
                {
                    "part": "snippet,contentDetails,statistics",
                    "id": ",".join(batch),
                    "maxResults": "50",
                },
            )
            videos.extend(response.get("items", []))
        return videos

    def _playlist_video_ids(self, playlist_id: str) -> list[str]:
        ids: list[str] = []
        next_page_token: str | None = None
        while True:
            params = {
                "part": "contentDetails",
                "playlistId": playlist_id,
                "maxResults": "50",
            }
            if next_page_token:
                params["pageToken"] = next_page_token
            response = self._get("playlistItems", params)
            for item in response.get("items", []):
                video_id = item.get("contentDetails", {}).get("videoId")
                if video_id:
                    ids.append(video_id)
            next_page_token = response.get("nextPageToken")
            if not next_page_token:
                return ids

    def _channels_by_id(self, channel_id: str) -> list[dict[str, Any]]:
        response = self._get(
            "channels",
            {"part": "snippet,contentDetails", "id": channel_id},
        )
        return response.get("items", [])

    def _channels_by_handle(self, handle: str) -> list[dict[str, Any]]:
        response = self._get(
            "channels",
            {"part": "snippet,contentDetails", "forHandle": handle.lstrip("@")},
        )
        return response.get("items", [])

    def _search_channels(self, query: str) -> list[dict[str, Any]]:
        response = self._get(
            "search",
            {"part": "snippet", "q": query, "type": "channel", "maxResults": "1"},
        )
        return response.get("items", [])

    def _get(self, endpoint: str, params: dict[str, str]) -> dict[str, Any]:
        merged = dict(params)
        merged["key"] = self.api_key
        response = self.client.get(f"{YOUTUBE_API_BASE}/{endpoint}", params=merged)
        try:
            payload = response.json()
        except ValueError as exc:
            raise YouTubeAPIError("YouTube returned a non-JSON response.") from exc

        if response.status_code >= 400:
            message = (
                payload.get("error", {})
                .get("errors", [{}])[0]
                .get("message")
                or payload.get("error", {}).get("message")
                or "YouTube API request failed."
            )
            raise YouTubeAPIError(message)
        return payload
