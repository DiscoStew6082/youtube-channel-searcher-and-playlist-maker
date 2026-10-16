"""Immutable, normalized metadata prepared before an import writes to SQLite."""

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChannelMetadata:
    id: str
    title: str
    input_url: str
    uploads_playlist_id: str

    @classmethod
    def from_api(cls, channel: dict[str, Any], input_url: str) -> "ChannelMetadata":
        channel_id = channel.get("id")
        uploads = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        if not isinstance(channel_id, str) or not channel_id:
            raise ValueError("YouTube did not return a channel ID.")
        if not isinstance(uploads, str) or not uploads:
            raise ValueError("YouTube did not return an uploads playlist for this channel.")
        return cls(
            id=channel_id,
            title=channel.get("snippet", {}).get("title", "Untitled channel"),
            input_url=input_url,
            uploads_playlist_id=uploads,
        )


@dataclass(frozen=True)
class VideoMetadata:
    video_id: str
    title: str
    description: str
    tags_json: str
    tags_text: str
    published_at: str
    thumbnail_url: str
    youtube_url: str
    raw_json: str

    @classmethod
    def from_api(cls, video: dict[str, Any], channel_id: str) -> "VideoMetadata":
        video_id = video.get("id")
        if not isinstance(video_id, str) or not video_id:
            raise ValueError("YouTube returned a video without an ID.")
        snippet = video.get("snippet", {})
        if snippet.get("channelId", channel_id) != channel_id:
            raise ValueError("YouTube returned a video from a different channel.")
        tags = snippet.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError("YouTube returned invalid video tags.")
        thumbnails = snippet.get("thumbnails", {})
        thumbnail_url = next(
            (
                thumbnails[key]["url"]
                for key in ("maxres", "standard", "high", "medium", "default")
                if thumbnails.get(key, {}).get("url")
            ),
            "",
        )
        return cls(
            video_id=video_id,
            title=snippet.get("title", "Untitled video"),
            description=snippet.get("description", ""),
            tags_json=json.dumps(tags),
            tags_text=" ".join(tags),
            published_at=snippet.get("publishedAt", ""),
            thumbnail_url=thumbnail_url,
            youtube_url=f"https://www.youtube.com/watch?v={video_id}",
            raw_json=json.dumps(video),
        )


@dataclass(frozen=True)
class ChannelSnapshot:
    channel: ChannelMetadata
    videos: tuple[VideoMetadata, ...]

    @classmethod
    def from_api(
        cls, channel: ChannelMetadata, videos: list[dict[str, Any]]
    ) -> "ChannelSnapshot":
        # An uploads playlist can repeat an ID; count and store each video once.
        normalized = {}
        for raw in videos:
            video = VideoMetadata.from_api(raw, channel.id)
            normalized[video.video_id] = video
        return cls(channel=channel, videos=tuple(normalized.values()))
