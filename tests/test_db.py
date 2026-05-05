import sqlite3

from app import db


def test_upsert_and_search_videos(tmp_path):
    database_path = tmp_path / "test.db"
    conn = db.connect(database_path)
    db.init_db(conn)

    channel = {
        "id": "UCtest",
        "snippet": {"title": "Test Channel"},
        "contentDetails": {"relatedPlaylists": {"uploads": "UUtest"}},
    }
    channel_id, _ = db.upsert_channel(conn, channel, "https://www.youtube.com/@test")
    db.upsert_videos(
        conn,
        channel_id,
        [
            {
                "id": "video1",
                "snippet": {
                    "title": "Monarch butterfly migration",
                    "description": "Danaus plexippus appears in the meadow.",
                    "tags": ["insects", "milkweed"],
                    "publishedAt": "2025-01-02T00:00:00Z",
                    "thumbnails": {"high": {"url": "https://img.example/video1.jpg"}},
                },
            }
        ],
    )

    results = db.search_videos(conn, "Danaus")

    assert len(results) == 1
    assert results[0]["video_id"] == "video1"
    assert results[0]["youtube_url"] == "https://www.youtube.com/watch?v=video1"
    assert results[0]["tags"] == ["insects", "milkweed"]


def test_upsert_videos_does_not_duplicate(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.init_db(conn)
    channel = {
        "id": "UCtest",
        "snippet": {"title": "Test Channel"},
        "contentDetails": {"relatedPlaylists": {"uploads": "UUtest"}},
    }
    channel_id, _ = db.upsert_channel(conn, channel, "https://www.youtube.com/@test")
    video = {
        "id": "video1",
        "snippet": {
            "title": "Original title",
            "description": "",
            "tags": [],
            "publishedAt": "2025-01-02T00:00:00Z",
            "thumbnails": {},
        },
    }

    db.upsert_videos(conn, channel_id, [video])
    video["snippet"]["title"] = "Updated monarch title"
    db.upsert_videos(conn, channel_id, [video])

    assert db.count_videos(conn) == 1
    assert db.search_videos(conn, "monarch")[0]["title"] == "Updated monarch title"
