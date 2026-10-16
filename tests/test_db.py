from app import db
from app.snapshot import ChannelMetadata, ChannelSnapshot


def test_snapshot_and_search_videos(tmp_path):
    database_path = tmp_path / "test.db"
    conn = db.connect(database_path)
    db.init_db(conn)

    channel = {
        "id": "UCtest",
        "snippet": {"title": "Test Channel"},
        "contentDetails": {"relatedPlaylists": {"uploads": "UUtest"}},
    }
    snapshot = ChannelSnapshot.from_api(
        ChannelMetadata.from_api(channel, "https://www.youtube.com/@test"),
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

    db.replace_channel_snapshot(conn, snapshot)
    results = db.search_videos(conn, "UCtest", "Danaus")

    assert len(results) == 1
    assert results[0]["video_id"] == "video1"
    assert results[0]["youtube_url"] == "https://www.youtube.com/watch?v=video1"
    assert results[0]["tags"] == ["insects", "milkweed"]


def test_snapshot_reimport_does_not_duplicate(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.init_db(conn)
    channel = {
        "id": "UCtest",
        "snippet": {"title": "Test Channel"},
        "contentDetails": {"relatedPlaylists": {"uploads": "UUtest"}},
    }
    metadata = ChannelMetadata.from_api(channel, "https://www.youtube.com/@test")
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

    db.replace_channel_snapshot(conn, ChannelSnapshot.from_api(metadata, [video]))
    video["snippet"]["title"] = "Updated monarch title"
    db.replace_channel_snapshot(conn, ChannelSnapshot.from_api(metadata, [video]))

    assert db.count_videos(conn, "UCtest") == 1
    assert db.search_videos(conn, "UCtest", "monarch")[0]["title"] == "Updated monarch title"
