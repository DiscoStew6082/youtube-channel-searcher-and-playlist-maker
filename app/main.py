import asyncio
import os
import signal
import time
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db
from app.config import BASE_DIR, get_api_key, get_database_path, get_idle_shutdown_seconds
from app.youtube import YouTubeAPIError, YouTubeClient


app = FastAPI(title="Local YouTube Metadata Search")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def startup() -> None:
    with db.connect(get_database_path()) as conn:
        db.init_db(conn)
    app.state.last_request_at = time.monotonic()
    idle_seconds = get_idle_shutdown_seconds()
    if idle_seconds:
        app.state.idle_shutdown_task = asyncio.create_task(_shutdown_after_idle(idle_seconds))


@app.middleware("http")
async def track_activity(request: Request, call_next):
    app.state.last_request_at = time.monotonic()
    response = await call_next(request)
    app.state.last_request_at = time.monotonic()
    return response


async def _shutdown_after_idle(idle_seconds: int) -> None:
    while True:
        await asyncio.sleep(min(5, idle_seconds))
        idle_for = time.monotonic() - app.state.last_request_at
        if idle_for >= idle_seconds:
            os.kill(os.getpid(), signal.SIGTERM)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", message: str = "", error: str = "") -> HTMLResponse:
    with db.connect(get_database_path()) as conn:
        db.init_db(conn)
        channel = db.get_channel(conn)
        video_count = db.count_videos(conn)
        results = db.search_videos(conn, q) if q.strip() else []

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "query": q,
            "results": results,
            "channel": channel,
            "video_count": video_count,
            "message": message,
            "error": error,
            "has_api_key": bool(get_api_key()),
        },
    )


@app.get("/playlist", response_class=HTMLResponse)
def playlist(request: Request, q: str = "") -> HTMLResponse:
    with db.connect(get_database_path()) as conn:
        db.init_db(conn)
        channel = db.get_channel(conn)
        video_count = db.count_videos(conn)
        results = db.search_videos(conn, q) if q.strip() else []

    playlist_url = _youtube_watch_playlist_url([result["video_id"] for result in results])
    return templates.TemplateResponse(
        "playlist.html",
        {
            "request": request,
            "query": q,
            "results": results,
            "channel": channel,
            "video_count": video_count,
            "playlist_url": playlist_url,
        },
    )


@app.post("/import")
def import_channel(channel_url: str = Form(...)) -> RedirectResponse:
    api_key = get_api_key()
    if not api_key:
        return _redirect(error="Add YOUTUBE_API_KEY to your .env file before importing.")

    try:
        client = YouTubeClient(api_key)
        channel = client.resolve_channel(channel_url)
        with db.connect(get_database_path()) as conn:
            db.init_db(conn)
            channel_id, uploads_playlist_id = db.upsert_channel(conn, channel, channel_url)
            videos = client.fetch_channel_videos(uploads_playlist_id)
            imported_count = db.upsert_videos(conn, channel_id, videos)
    except (ValueError, YouTubeAPIError) as exc:
        return _redirect(error=str(exc))

    return _redirect(message=f"Imported {imported_count} videos.")


@app.post("/refresh")
def refresh_channel() -> RedirectResponse:
    api_key = get_api_key()
    if not api_key:
        return _redirect(error="Add YOUTUBE_API_KEY to your .env file before refreshing.")

    try:
        client = YouTubeClient(api_key)
        with db.connect(get_database_path()) as conn:
            db.init_db(conn)
            channel = db.get_channel(conn)
            if channel is None:
                return _redirect(error="Import a channel before refreshing.")
            videos = client.fetch_channel_videos(channel["uploads_playlist_id"])
            imported_count = db.upsert_videos(conn, channel["id"], videos)
            conn.execute(
                "UPDATE channels SET last_refreshed_at = ? WHERE id = ?",
                (db.current_timestamp(), channel["id"]),
            )
            conn.commit()
    except YouTubeAPIError as exc:
        return _redirect(error=str(exc))

    return _redirect(message=f"Refreshed {imported_count} videos.")


def _youtube_watch_playlist_url(video_ids: list[str]) -> str:
    if not video_ids:
        return ""
    return "https://www.youtube.com/watch_videos?" + urlencode(
        {"video_ids": ",".join(video_ids[:50])}
    )


def _redirect(message: str = "", error: str = "") -> RedirectResponse:
    params = []
    if message:
        params.append(("message", message))
    if error:
        params.append(("error", error))
    query = ""
    if params:
        query = "?" + urlencode(params)
    return RedirectResponse(url="/" + query, status_code=303)
