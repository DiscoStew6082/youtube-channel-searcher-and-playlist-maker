from app.main import _youtube_watch_playlist_url


def test_youtube_watch_playlist_url_uses_video_ids():
    url = _youtube_watch_playlist_url(["abc123", "def456"])

    assert url == "https://www.youtube.com/watch_videos?video_ids=abc123%2Cdef456"


def test_youtube_watch_playlist_url_limits_to_50_videos():
    ids = [f"video{i}" for i in range(55)]
    url = _youtube_watch_playlist_url(ids)

    assert "video49" in url
    assert "video50" not in url


def test_youtube_watch_playlist_url_empty_without_results():
    assert _youtube_watch_playlist_url([]) == ""
