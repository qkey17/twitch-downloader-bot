import os
import re
from dataclasses import dataclass
from pathlib import Path

# --- Credentials ---
ENV_TOKEN = "TELEGRAM_BOT_TOKEN_TWITCH"
ENV_TWITCH_CLIENT_ID = "TWITCH_CLIENT_ID"
ENV_TWITCH_GQL_CLIENT_ID = "TWITCH_GQL_CLIENT_ID"
ENV_TWITCH_ACCESS_TOKEN = "TWITCH_ACCESS_TOKEN"
DEFAULT_TWITCH_GQL_CLIENT_ID = "kd1unb4b3q4t58fwlpcbzcbnm76a8fp"


class ConfigurationError(RuntimeError):
    """Raised when required application configuration is missing."""


@dataclass(frozen=True)
class TwitchCredentials:
    client_id: str
    access_token: str


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        raise ConfigurationError(f"Missing required environment variable: {name}")
    return value.strip()


def get_telegram_bot_token() -> str:
    return _required_env(ENV_TOKEN)


def get_twitch_client_id() -> str:
    """Return the application Client ID used for authenticated Helix requests."""
    return _required_env(ENV_TWITCH_CLIENT_ID)


def get_twitch_gql_client_id() -> str:
    """Return the separately configured Client ID used for Twitch GraphQL."""
    value = os.getenv(ENV_TWITCH_GQL_CLIENT_ID)
    if value is None:
        return DEFAULT_TWITCH_GQL_CLIENT_ID
    if not value.strip():
        raise ConfigurationError(
            f"Environment variable {ENV_TWITCH_GQL_CLIENT_ID} must not be empty"
        )
    return value.strip()


def get_twitch_credentials() -> TwitchCredentials:
    return TwitchCredentials(
        client_id=get_twitch_client_id(),
        access_token=_required_env(ENV_TWITCH_ACCESS_TOKEN),
    )


def validate_credentials() -> None:
    get_telegram_bot_token()
    get_twitch_credentials()
    if HTML_PUBLISH_MODE not in {"local", "public"}:
        raise ConfigurationError("TCD_HTML_PUBLISH_MODE must be 'local' or 'public'")
    if HTML_PUBLISH_MODE == "public" and (not PUBLIC_HTML_DIR or not PUBLIC_BASE_URL):
        raise ConfigurationError(
            "Public HTML mode requires TCD_PUBLIC_HTML_DIR and TCD_PUBLIC_BASE_URL"
        )

# --- Twitch GraphQL (unofficial) ---
GQL_ENDPOINT = "https://gql.twitch.tv/gql"
VIDEO_COMMENTS_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"

# Strict URL format
VOD_URL_RE = re.compile(r"^https://www\.twitch\.tv/(?:videos/|[^/]+/v/)(\d+)")

# Project-relative defaults
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Output
OUT_DIR = str(PROJECT_ROOT / "_out")

# Temporary chat storage must reside on disk, separately from public HTML.
_spool_dir = Path(os.getenv("TCD_SPOOL_DIR") or "_work").expanduser()
SPOOL_DIR = str(_spool_dir if _spool_dir.is_absolute() else PROJECT_ROOT / _spool_dir)


def _positive_int_env(name, default):
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ConfigurationError(f"Environment variable {name} must be a positive integer") from None
    if value <= 0:
        raise ConfigurationError(f"Environment variable {name} must be a positive integer")
    return value


SPOOL_BATCH_MESSAGES = _positive_int_env("TCD_SPOOL_BATCH_MESSAGES", 5000)
SPOOL_BATCH_BYTES = _positive_int_env("TCD_SPOOL_BATCH_BYTES", 4 * 1024 * 1024)

# Progress
PROGRESS_INTERVAL = 3.0

# Pending selection TTL
PENDING_TTL_SECONDS = 180

# Personal history is independent of the technical VOD cache.
HISTORY_ENABLED = os.getenv("TCD_HISTORY_ENABLED", "false").strip().lower() == "true"

# DB file
DB_PATH = os.getenv("TCD_DB_PATH") or str(PROJECT_ROOT / "vod_history.db")

# Cache TTL in DB (seconds) — can prune old cache later if you want
CACHE_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days

# GQL tuning
FETCH_DELAY_BASE = 0.05
FETCH_DELAY_MAX = 1.5
GQL_MAX_RETRIES = 6
GQL_TIMEOUT_S = 25

# Logging
LOG_LEVEL = os.getenv("TCD_LOG_LEVEL", "INFO")
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"

# HTML publication
HTML_PUBLISH_MODE = os.getenv("TCD_HTML_PUBLISH_MODE", "local").strip().lower()
PUBLIC_HTML_DIR = os.getenv("TCD_PUBLIC_HTML_DIR")
PUBLIC_BASE_URL = os.getenv("TCD_PUBLIC_BASE_URL")
PUBLIC_HTML_URL_PREFIX = os.getenv("TCD_PUBLIC_HTML_URL_PREFIX", "/public_html")
