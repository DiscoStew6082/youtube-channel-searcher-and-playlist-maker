import asyncio
import ipaddress
import os
import secrets
import signal
import threading
import time
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import db
from app.config import (
    BASE_DIR,
    get_allow_remote_thumbnails,
    get_api_key,
    get_database_path,
    get_idle_shutdown_seconds,
)
from app.youtube import YouTubeAPIError, YouTubeClient


@asynccontextmanager
async def lifespan(application: FastAPI):
    with db.connect(get_database_path()) as conn:
        db.init_db(conn)
    application.state.last_request_at = time.monotonic()
    idle_task = None
    idle_seconds = get_idle_shutdown_seconds()
    if idle_seconds:
        idle_task = asyncio.create_task(_shutdown_after_idle(idle_seconds))

    try:
        yield
    finally:
        if idle_task is not None:
            idle_task.cancel()
            with suppress(asyncio.CancelledError):
                await idle_task


app = FastAPI(title="Local YouTube Metadata Search", lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.state.csrf_token = secrets.token_urlsafe(32)
app.state.mutation_lock = threading.Lock()
app.state.last_mutation_at = 0.0

LOCAL_HOSTS = {"127.0.0.1", "localhost"}
SAFE_FETCH_SITES = {"", "none", "same-origin"}
MUTATION_COOLDOWN_SECONDS = 2.0


@app.middleware("http")
async def protect_local_app(request: Request, call_next):
    fetch_site = request.headers.get("sec-fetch-site", "").lower()
    origin = request.headers.get("origin", "")
    if not _is_loopback_client(request):
        return PlainTextResponse("Non-loopback request rejected.", status_code=403)
    if fetch_site not in SAFE_FETCH_SITES or (
        origin and not _origin_matches_request(origin, request)
    ):
        return PlainTextResponse("Cross-site request rejected.", status_code=403)

    app.state.last_request_at = time.monotonic()
    response = await call_next(request)
    app.state.last_request_at = time.monotonic()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'; object-src 'none'; style-src 'self'; "
        "img-src 'self' https://i.ytimg.com data:"
    )
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=sorted(LOCAL_HOSTS),
    www_redirect=False,
)


async def _shutdown_after_idle(idle_seconds: int) -> None:
    while True:
        await asyncio.sleep(min(5, idle_seconds))
        idle_for = time.monotonic() - app.state.last_request_at
        if idle_for >= idle_seconds:
            os.kill(os.getpid(), signal.SIGTERM)


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request, q: str = "", message: str = "", error: str = ""
) -> HTMLResponse:
    with db.connect(get_database_path()) as conn:
        db.init_db(conn)
        channel = db.get_channel(conn)
        video_count = db.count_videos(conn)
        results = db.search_videos(conn, q) if q.strip() else []

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "query": q,
            "results": results,
            "channel": channel,
            "video_count": video_count,
            "message": message,
            "error": error,
            "has_api_key": bool(get_api_key()),
            "csrf_token": app.state.csrf_token,
            "allow_remote_thumbnails": get_allow_remote_thumbnails(),
        },
    )


@app.get("/playlist", response_class=HTMLResponse)
def playlist(request: Request, q: str = "") -> HTMLResponse:
    with db.connect(get_database_path()) as conn:
        db.init_db(conn)
        channel = db.get_channel(conn)
        video_count = db.count_videos(conn)
        results = db.search_videos(conn, q) if q.strip() else []

    playlist_url = _youtube_watch_playlist_url(
        [result["video_id"] for result in results]
    )
    return templates.TemplateResponse(
        request=request,
        name="playlist.html",
        context={
            "query": q,
            "results": results,
            "channel": channel,
            "video_count": video_count,
            "playlist_url": playlist_url,
            "allow_remote_thumbnails": get_allow_remote_thumbnails(),
        },
    )


@app.post("/import")
def import_channel(
    channel_url: str = Form(..., min_length=1, max_length=2048),
    csrf_token: str = Form(..., min_length=20, max_length=100),
) -> RedirectResponse:
    _require_csrf(csrf_token)
    _acquire_mutation_slot()
    try:
        api_key = get_api_key()
        if not api_key:
            return _redirect(
                error="Add YOUTUBE_API_KEY to your .env file before importing."
            )

        client = YouTubeClient(api_key)
        channel = client.resolve_channel(channel_url)
        with db.connect(get_database_path()) as conn:
            db.init_db(conn)
            channel_id, uploads_playlist_id = db.upsert_channel(
                conn, channel, channel_url
            )
            videos = client.fetch_channel_videos(uploads_playlist_id)
            imported_count = db.upsert_videos(conn, channel_id, videos)
    except (ValueError, YouTubeAPIError) as exc:
        return _redirect(error=str(exc))
    finally:
        _release_mutation_slot()

    return _redirect(message=f"Imported {imported_count} videos.")


@app.post("/refresh")
def refresh_channel(
    csrf_token: str = Form(..., min_length=20, max_length=100),
) -> RedirectResponse:
    _require_csrf(csrf_token)
    _acquire_mutation_slot()
    try:
        api_key = get_api_key()
        if not api_key:
            return _redirect(
                error="Add YOUTUBE_API_KEY to your .env file before refreshing."
            )

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
    finally:
        _release_mutation_slot()

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


def _origin_matches_request(origin: str, request: Request) -> bool:
    parsed = urlparse(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in LOCAL_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return False

    request_host = request.url.hostname
    if request_host not in LOCAL_HOSTS or parsed.scheme != request.url.scheme:
        return False

    return _effective_port(parsed.scheme, parsed.port) == _effective_port(
        request.url.scheme,
        request.url.port,
    )


def _effective_port(scheme: str, port: int | None) -> int:
    if port is not None:
        return port
    return 443 if scheme == "https" else 80


def _is_loopback_client(request: Request) -> bool:
    if request.client is None:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def _require_csrf(candidate: str) -> None:
    if not secrets.compare_digest(candidate, app.state.csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token.")


def _acquire_mutation_slot() -> None:
    if not app.state.mutation_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=429, detail="An import or refresh is already running."
        )

    elapsed = time.monotonic() - app.state.last_mutation_at
    if elapsed < MUTATION_COOLDOWN_SECONDS:
        app.state.mutation_lock.release()
        raise HTTPException(
            status_code=429, detail="Please wait before importing or refreshing again."
        )


def _release_mutation_slot() -> None:
    app.state.last_mutation_at = time.monotonic()
    app.state.mutation_lock.release()
