import json
import sqlite3
from html import escape
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.snapshot import ChannelMetadata, ChannelSnapshot


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

        CREATE INDEX IF NOT EXISTS videos_channel_id ON videos(channel_id);

        CREATE TABLE IF NOT EXISTS active_channel (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            channel_id TEXT REFERENCES channels(id)
        );

        -- Migrate the previous display choice once. Ties use insertion order.
        INSERT OR IGNORE INTO active_channel (singleton, channel_id)
        VALUES (1, (
            SELECT id FROM channels
            ORDER BY last_refreshed_at DESC, rowid DESC LIMIT 1
        ));

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


def replace_channel_snapshot(conn: sqlite3.Connection, snapshot: ChannelSnapshot) -> int:
    """Atomically publish metadata, the complete video/FTS set, and active scope."""
    now = current_timestamp()
    with conn:
        _upsert_channel(conn, snapshot.channel, now)
        _replace_videos(conn, snapshot, now)
        conn.execute(
            "UPDATE active_channel SET channel_id = ? WHERE singleton = 1",
            (snapshot.channel.id,),
        )
    return len(snapshot.videos)


def _upsert_channel(
    conn: sqlite3.Connection, channel: ChannelMetadata, refreshed_at: str
) -> None:
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
        (channel.id, channel.title, channel.input_url, channel.uploads_playlist_id, refreshed_at),
    )


def _replace_videos(conn: sqlite3.Connection, snapshot: ChannelSnapshot, fetched_at: str) -> None:
    channel_id = snapshot.channel.id
    conn.execute(
        "DELETE FROM videos_fts WHERE video_id IN (SELECT video_id FROM videos WHERE channel_id = ?)",
        (channel_id,),
    )
    conn.execute("DELETE FROM videos WHERE channel_id = ?", (channel_id,))
    for video in snapshot.videos:
        conn.execute(
            """
            INSERT INTO videos (
                video_id, channel_id, title, description, tags_json,
                published_at, thumbnail_url, youtube_url, raw_json, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                video.video_id, channel_id, video.title, video.description,
                video.tags_json, video.published_at, video.thumbnail_url,
                video.youtube_url, video.raw_json, fetched_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO videos_fts (video_id, title, description, tags)
            VALUES (?, ?, ?, ?)
            """,
            (video.video_id, video.title, video.description, video.tags_text),
        )


def get_channel(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT c.* FROM channels c
        JOIN active_channel a ON a.channel_id = c.id
        WHERE a.singleton = 1
        """
    ).fetchone()


def count_videos(conn: sqlite3.Connection, channel_id: str | None) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM videos WHERE channel_id = ?", (channel_id,)
    ).fetchone()[0]


def search_videos(
    conn: sqlite3.Connection, channel_id: str | None, query: str, limit: int = 50
) -> list[dict[str, Any]]:
    normalized = query.strip()
    expression = _fts_query(normalized)
    if channel_id is None or not expression:
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
        WHERE videos_fts MATCH ? AND v.channel_id = ?
        ORDER BY rank, v.published_at DESC
        LIMIT ?
        """,
        (expression, channel_id, limit),
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
