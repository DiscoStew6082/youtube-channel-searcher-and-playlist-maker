import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_PATH = BASE_DIR / "data" / "youtube_species.db"


def get_api_key() -> str:
    return os.getenv("YOUTUBE_API_KEY", "").strip()


def get_database_path() -> Path:
    configured = os.getenv("DATABASE_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_DATABASE_PATH


def get_idle_shutdown_seconds() -> int:
    configured = os.getenv("IDLE_SHUTDOWN_SECONDS", "").strip()
    if not configured:
        return 0
    try:
        return max(0, int(configured))
    except ValueError:
        return 0
