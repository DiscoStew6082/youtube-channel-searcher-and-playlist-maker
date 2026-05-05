import json
import sqlite3
from html import escape
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode = WAL;

        CREATE TABLE IF NOT EXISTS channels (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            input_url TEXT NOT NULL,
            uploads_playlist_id TEXT NOT NULL,
            last_refreshed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS videos (
            video_id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            published_at TEXT NOT NULL DEFAULT '',
            thumbnail_url TEXT NOT NULL DEFAULT '',
            youtube_url TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            FOREIGN KEY(channel_id) REFERENCES channels(id)
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS videos_fts USING fts5(
            video_id UNINDEXED,
            title,
            description,
            tags
        );
        """
    )
    conn.commit()


def current_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert_channel(
    conn: sqlite3.Connection,
    channel: dict[str, Any],
    input_url: str,
) -> tuple[str, str]:
    snippet = channel.get("snippet", {})
    related = channel.get("contentDetails", {}).get("relatedPlaylists", {})
    channel_id = channel["id"]
    uploads_playlist_id = related.get("uploads", "")
    if not uploads_playlist_id:
        raise ValueError("YouTube did not return an uploads playlist for this channel.")

    conn.execute(
        """
        INSERT INTO channels (id, title, input_url, uploads_playlist_id, last_refreshed_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            input_url = excluded.input_url,
            uploads_playlist_id = excluded.uploads_playlist_id,
            last_refreshed_at = excluded.last_refreshed_at
        """,
        (
            channel_id,
            snippet.get("title", "Untitled channel"),
            input_url,
            uploads_playlist_id,
            current_timestamp(),
        ),
    )
    conn.commit()
    return channel_id, uploads_playlist_id


def upsert_videos(
    conn: sqlite3.Connection,
    channel_id: str,
    videos: list[dict[str, Any]],
) -> int:
    now = current_timestamp()
    for video in videos:
        snippet = video.get("snippet", {})
        video_id = video["id"]
        title = snippet.get("title", "Untitled video")
        description = snippet.get("description", "")
        tags = snippet.get("tags", [])
        thumbnail_url = _best_thumbnail(snippet.get("thumbnails", {}))
        youtube_url = f"https://www.youtube.com/watch?v={video_id}"
        tags_json = json.dumps(tags)

        conn.execute(
            """
            INSERT INTO videos (
                video_id, channel_id, title, description, tags_json,
                published_at, thumbnail_url, youtube_url, raw_json, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                title = excluded.title,
                description = excluded.description,
                tags_json = excluded.tags_json,
                published_at = excluded.published_at,
                thumbnail_url = excluded.thumbnail_url,
                youtube_url = excluded.youtube_url,
                raw_json = excluded.raw_json,
                fetched_at = excluded.fetched_at
            """,
            (
                video_id,
                channel_id,
                title,
                description,
                tags_json,
                snippet.get("publishedAt", ""),
                thumbnail_url,
                youtube_url,
                json.dumps(video),
                now,
            ),
        )
        conn.execute("DELETE FROM videos_fts WHERE video_id = ?", (video_id,))
        conn.execute(
            """
            INSERT INTO videos_fts (video_id, title, description, tags)
            VALUES (?, ?, ?, ?)
            """,
            (video_id, title, description, " ".join(tags)),
        )
    conn.commit()
    return len(videos)


def get_channel(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM channels ORDER BY last_refreshed_at DESC LIMIT 1"
    ).fetchone()


def count_videos(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]


def search_videos(conn: sqlite3.Connection, query: str, limit: int = 50) -> list[dict[str, Any]]:
    normalized = query.strip()
    if not normalized:
        return []

    rows = conn.execute(
        """
        SELECT
            v.video_id,
            v.title,
            v.description,
            v.tags_json,
            v.published_at,
            v.thumbnail_url,
            v.youtube_url,
            snippet(videos_fts, 2, '<mark>', '</mark>', '...', 24) AS match_snippet,
            bm25(videos_fts) AS rank
        FROM videos_fts
        JOIN videos v ON v.video_id = videos_fts.video_id
        WHERE videos_fts MATCH ?
        ORDER BY rank, v.published_at DESC
        LIMIT ?
        """,
        (_fts_query(normalized), limit),
    ).fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["tags"] = json.loads(item.pop("tags_json") or "[]")
        item["published_date"] = item["published_at"][:10]
        snippet = item["match_snippet"] or _plain_snippet(item["description"], normalized)
        item["match_snippet"] = _safe_highlight_snippet(snippet)
        results.append(item)
    return results


def _fts_query(query: str) -> str:
    terms = [term for term in query.replace('"', " ").split() if term]
    if not terms:
        return ""
    escaped = [f'"{term}"' for term in terms]
    return " AND ".join(escaped)


def _plain_snippet(text: str, query: str) -> str:
    if not text:
        return ""
    lowered = text.lower()
    needle = query.lower().split()[0]
    index = lowered.find(needle)
    if index < 0:
        return text[:220]
    start = max(index - 80, 0)
    end = min(index + 160, len(text))
    return text[start:end]


def _safe_highlight_snippet(snippet: str) -> str:
    escaped = escape(snippet)
    return (
        escaped.replace("&lt;mark&gt;", "<mark>")
        .replace("&lt;/mark&gt;", "</mark>")
    )


def _best_thumbnail(thumbnails: dict[str, Any]) -> str:
    for key in ("maxres", "standard", "high", "medium", "default"):
        url = thumbnails.get(key, {}).get("url")
        if url:
            return url
    return ""
