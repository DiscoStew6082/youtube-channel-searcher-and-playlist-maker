"""Fetch a complete snapshot, then publish it through one transaction boundary."""

from contextlib import closing
from pathlib import Path

from app import db
from app.snapshot import ChannelMetadata, ChannelSnapshot
from app.youtube import YouTubeClient


def import_channel(client: YouTubeClient, database_path: Path, input_url: str) -> int:
    channel = ChannelMetadata.from_api(client.resolve_channel(input_url), input_url)
    return _fetch_and_store(client, database_path, channel)


def refresh_channel(client: YouTubeClient, database_path: Path) -> int:
    with closing(db.connect(database_path)) as conn:
        db.init_db(conn)
        active = db.get_channel(conn)
    if active is None:
        raise ValueError("Import a channel before refreshing.")
    channel = ChannelMetadata(
        id=active["id"],
        title=active["title"],
        input_url=active["input_url"],
        uploads_playlist_id=active["uploads_playlist_id"],
    )
    return _fetch_and_store(client, database_path, channel)


def _fetch_and_store(
    client: YouTubeClient, database_path: Path, channel: ChannelMetadata
) -> int:
    videos = client.fetch_channel_videos(channel.uploads_playlist_id)
    snapshot = ChannelSnapshot.from_api(channel, videos)
    # No database transaction is held open during API calls or normalization.
    with closing(db.connect(database_path)) as conn:
        db.init_db(conn)
        return db.replace_channel_snapshot(conn, snapshot)
