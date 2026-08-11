import re

from fastapi.testclient import TestClient

from app import db
from app.main import app


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    app.state.last_mutation_at = 0.0
    return TestClient(
        app,
        base_url="http://localhost",
        client=("127.0.0.1", 50000),
    )


def test_rejects_untrusted_host(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/", headers={"host": "attacker.example"})

    assert response.status_code == 400


def test_rejects_non_loopback_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    with TestClient(
        app,
        base_url="http://localhost",
        client=("192.0.2.10", 50000),
    ) as client:
        response = client.get("/")

    assert response.status_code == 403


def test_rejects_cross_site_request(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/", headers={"sec-fetch-site": "cross-site"})

    assert response.status_code == 403


def test_rejects_cross_origin_post_even_with_valid_token(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/refresh",
            data={"csrf_token": app.state.csrf_token},
            headers={"origin": "https://attacker.example"},
        )

    assert response.status_code == 403


def test_requires_csrf_token_for_refresh(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/refresh",
            data={"csrf_token": "not-the-process-token"},
            headers={"origin": "http://localhost"},
        )

    assert response.status_code == 403


def test_accepts_same_origin_refresh_with_page_token(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        page = client.get("/")
        token = re.search(r'name="csrf_token" type="hidden" value="([^"]+)"', page.text)
        assert token is not None

        response = client.post(
            "/refresh",
            data={"csrf_token": token.group(1)},
            headers={"origin": "http://localhost"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/?error=")


def test_rate_limits_back_to_back_mutations(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        form = {"csrf_token": app.state.csrf_token}
        headers = {"origin": "http://localhost"}
        first = client.post(
            "/refresh",
            data=form,
            headers=headers,
            follow_redirects=False,
        )
        second = client.post(
            "/refresh",
            data=form,
            headers=headers,
            follow_redirects=False,
        )

    assert first.status_code == 303
    assert second.status_code == 429


def test_sets_browser_security_headers(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/")

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"].startswith("default-src 'self'")
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


def test_remote_thumbnails_are_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("ALLOW_REMOTE_THUMBNAILS", raising=False)
    database_path = tmp_path / "test.db"
    _add_searchable_video(database_path)
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/?q=monarch")

    assert "i.ytimg.com" not in response.text


def test_remote_thumbnails_require_explicit_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOW_REMOTE_THUMBNAILS", "true")
    database_path = tmp_path / "test.db"
    _add_searchable_video(database_path)
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/?q=monarch")

    assert 'src="https://i.ytimg.com/vi/video1/hqdefault.jpg"' in response.text
    assert 'referrerpolicy="no-referrer"' in response.text


def _add_searchable_video(database_path):
    with db.connect(database_path) as conn:
        db.init_db(conn)
        channel = {
            "id": "UCtest",
            "snippet": {"title": "Test Channel"},
            "contentDetails": {"relatedPlaylists": {"uploads": "UUtest"}},
        }
        channel_id, _ = db.upsert_channel(conn, channel, "@test")
        db.upsert_videos(
            conn,
            channel_id,
            [
                {
                    "id": "video1",
                    "snippet": {
                        "title": "Monarch migration",
                        "description": "Monarch butterfly",
                        "tags": ["monarch"],
                        "thumbnails": {
                            "high": {
                                "url": "https://i.ytimg.com/vi/video1/hqdefault.jpg"
                            }
                        },
                    },
                }
            ],
        )
