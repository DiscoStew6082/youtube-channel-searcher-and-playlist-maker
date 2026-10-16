import sqlite3
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app import db, importer, main
from app.snapshot import ChannelMetadata, ChannelSnapshot
from app.youtube import YouTubeAPIError


def snapshot(channel_id, *video_ids):
    channel = ChannelMetadata(channel_id, f"Channel {channel_id}", f"@{channel_id}", f"UU{channel_id}")
    return ChannelSnapshot.from_api(channel, [
        {"id": video_id, "snippet": {"title": f"Monarch {video_id}", "channelId": channel_id}}
        for video_id in video_ids
    ])


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "test.db"
    with closing(db.connect(path)) as conn:
        db.init_db(conn)
        db.replace_channel_snapshot(conn, snapshot("A", "old-a"))
    return path


def index_state(conn):
    channel = db.get_channel(conn)
    channel_id = channel["id"] if channel else None
    return channel_id, db.count_videos(conn, channel_id), [
        row["video_id"] for row in db.search_videos(conn, channel_id, "monarch")
    ]


def test_successful_import_has_explicit_channel_scope(database):
    with closing(db.connect(database)) as conn:
        db.replace_channel_snapshot(conn, snapshot("B", "new-b"))
        assert index_state(conn) == ("B", 1, ["new-b"])
        # Historical metadata is retained, but cannot leak into B's count/results.
        assert db.count_videos(conn, "A") == 1
        assert db.count_videos(conn, None) == 0
        assert db.search_videos(conn, None, "monarch") == []
        conn.execute("UPDATE channels SET last_refreshed_at = '9999' WHERE id = 'A'")
        conn.commit()
        db.init_db(conn)
        assert db.get_channel(conn)["id"] == "B"


@pytest.mark.parametrize("channel_id", ["A", "B"])
@pytest.mark.parametrize("failure_point", ["video", "promotion"])
def test_transaction_rolls_back_videos_fts_metadata_and_promotion(
    database, channel_id, failure_point, monkeypatch
):
    monkeypatch.setattr(db, "current_timestamp", lambda: "2099-01-01T00:00:00+00:00")
    with closing(db.connect(database)) as conn:
        before = list(conn.iterdump())
        if failure_point == "video":
            conn.execute("""
                CREATE TEMP TRIGGER fail_video BEFORE INSERT ON videos
                WHEN NEW.video_id = 'fail'
                BEGIN SELECT RAISE(ABORT, 'injected write failure'); END
            """)
        else:
            conn.execute("""
                CREATE TEMP TRIGGER fail_promotion BEFORE UPDATE ON active_channel
                BEGIN SELECT RAISE(ABORT, 'injected promotion failure'); END
            """)
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            db.replace_channel_snapshot(conn, snapshot(channel_id, "partial", "fail"))
        assert not conn.in_transaction
        assert list(conn.iterdump()) == before
        assert index_state(conn) == ("A", 1, ["old-a"])
        assert db.search_videos(conn, channel_id, "partial") == []


def test_refresh_replaces_removed_videos_and_fts(database):
    with closing(db.connect(database)) as conn:
        db.replace_channel_snapshot(conn, snapshot("A", "new-a"))
        assert index_state(conn) == ("A", 1, ["new-a"])
        assert conn.execute("SELECT video_id FROM videos_fts").fetchall()[0][0] == "new-a"
        assert conn.execute("SELECT COUNT(*) FROM videos_fts").fetchone()[0] == 1
        db.replace_channel_snapshot(conn, snapshot("A"))
        assert index_state(conn) == ("A", 0, [])
        assert conn.execute("SELECT COUNT(*) FROM videos_fts").fetchone()[0] == 0


def test_legacy_migration_is_once_only_and_preserves_rows(database):
    with closing(db.connect(database)) as conn:
        db.replace_channel_snapshot(conn, snapshot("B", "new-b"))
        # Return the populated database to the previous schema (no active pointer).
        conn.execute("DROP TABLE active_channel")
        conn.execute("DROP INDEX videos_channel_id")
        conn.execute("UPDATE channels SET last_refreshed_at = '2026-01-01'")
        conn.commit()
        db.init_db(conn)
        # Same-second historical imports use deterministic insertion order.
        assert index_state(conn) == ("B", 1, ["new-b"])
        assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 2
        conn.execute("UPDATE channels SET last_refreshed_at = '2027-01-01' WHERE id = 'A'")
        conn.commit()
        db.init_db(conn)
        assert db.get_channel(conn)["id"] == "B"


@pytest.mark.parametrize("query", ['', '   ', '"', '" "', '\t""\n'])
def test_empty_compiled_fts_queries_return_no_results(database, query):
    with closing(db.connect(database)) as conn:
        assert db.search_videos(conn, "A", query) == []


def test_snapshot_deduplicates_ids_and_is_detached_from_api_payload():
    channel = snapshot("A").channel
    raw = {"id": "one", "snippet": {"title": "Original", "tags": ["monarch"]}}
    prepared = ChannelSnapshot.from_api(channel, [raw, raw])
    raw["snippet"]["title"] = "Changed"
    raw["snippet"]["tags"].append("extra")
    assert len(prepared.videos) == 1
    assert prepared.videos[0].title == "Original"
    assert prepared.videos[0].tags_text == "monarch"


class StubYouTubeClient:
    def resolve_channel(self, raw):
        return {
            "id": "B", "snippet": {"title": "Channel B"},
            "contentDetails": {"relatedPlaylists": {"uploads": "UUB"}},
        }

    def fetch_channel_videos(self, uploads_playlist_id):
        return [{"id": "new-b", "snippet": {"title": "Monarch new-b"}}]


@pytest.mark.parametrize("failure", ["network", "invalid_id", "invalid_channel", "invalid_tags"])
def test_fetch_and_normalization_fail_before_opening_database(database, monkeypatch, failure):
    client = StubYouTubeClient()

    def fetch(_):
        if failure == "network":
            raise YouTubeAPIError("YouTube unavailable")
        if failure == "invalid_id":
            return [{"snippet": {}}]
        if failure == "invalid_channel":
            return [{"id": "bad", "snippet": {"channelId": "C"}}]
        return [{"id": "bad", "snippet": {"tags": [None]}}]

    def unexpected_connect(_):
        pytest.fail("Database opened before the snapshot was ready")

    with monkeypatch.context() as patch:
        patch.setattr(client, "fetch_channel_videos", fetch)
        patch.setattr(db, "connect", unexpected_connect)
        with pytest.raises((ValueError, YouTubeAPIError)):
            importer.import_channel(client, database, "@B")
    with closing(db.connect(database)) as conn:
        assert index_state(conn) == ("A", 1, ["old-a"])


def test_service_import_and_refresh_share_snapshot_boundary(database):
    client = StubYouTubeClient()
    assert importer.import_channel(client, database, "@B") == 1
    with closing(db.connect(database)) as conn:
        assert index_state(conn) == ("B", 1, ["new-b"])
    client.fetch_channel_videos = lambda _: []
    assert importer.refresh_channel(client, database) == 0
    with closing(db.connect(database)) as conn:
        assert index_state(conn) == ("B", 0, [])
        assert db.count_videos(conn, "A") == 1


@pytest.fixture
def web_client(database, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(database))
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    monkeypatch.setenv("IDLE_SHUTDOWN_SECONDS", "0")
    monkeypatch.setattr(main, "YouTubeClient", lambda _: StubYouTubeClient())
    main.app.state.last_mutation_at = 0.0
    with TestClient(main.app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        yield client


def test_import_routes_search_and_queue_are_channel_scoped(web_client):
    response = web_client.post("/import", data={
        "channel_url": "@B", "csrf_token": main.app.state.csrf_token,
    })
    assert response.status_code == 200
    assert "Imported 1 videos." in response.text
    for route in ("/", "/playlist"):
        response = web_client.get(route, params={"q": "monarch"})
        assert "Channel B · 1 videos indexed" in response.text
        assert "new-b" in response.text
        assert "old-a" not in response.text
        empty = web_client.get(route, params={"q": '" "'})
        assert empty.status_code == 200
        assert "watch_videos?" not in empty.text


@pytest.mark.parametrize("route", ["/import", "/refresh"])
def test_failed_fetch_keeps_previous_active_index(web_client, monkeypatch, database, route):
    def fail(*_):
        raise YouTubeAPIError("YouTube unavailable")

    monkeypatch.setattr(StubYouTubeClient, "fetch_channel_videos", fail)
    response = web_client.post(route, data={
        "channel_url": "@B", "csrf_token": main.app.state.csrf_token,
    })
    assert response.status_code == 200
    assert "YouTube unavailable" in response.text
    assert "Channel A · 1 videos indexed" in response.text
    with closing(db.connect(database)) as conn:
        assert index_state(conn) == ("A", 1, ["old-a"])


def test_page_reads_use_one_committed_snapshot(database, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(database))
    original_count = db.count_videos

    def switch_channel_between_reads(conn, channel_id):
        with closing(db.connect(database)) as writer:
            db.replace_channel_snapshot(writer, snapshot("B", "new-b"))
        return original_count(conn, channel_id)

    with monkeypatch.context() as patch:
        patch.setattr(db, "count_videos", switch_channel_between_reads)
        channel, count, results = main._read_index("monarch")
    assert channel["id"] == "A"
    assert count == 1
    assert [row["video_id"] for row in results] == ["old-a"]
    assert main._read_index("monarch")[0]["id"] == "B"
