"""All settings come from environment variables (see .env.example)."""
import os


def _env(name: str, default: str = "") -> str:
    """Read a variable, ignoring a trailing '# comment' (docker compose keeps those on empty values)."""
    v = os.getenv(name, default) or default
    return v.split(" #", 1)[0].strip() if not v.lstrip().startswith("#") else default


def _int(name: str, default: int | None = None) -> int | None:
    v = _env(name)
    return int(v) if v else default


def _ids(name: str) -> set[int]:
    return {int(x) for x in _env(name).replace(" ", "").split(",") if x}


def _bool(name: str, default: bool = False) -> bool:
    v = _env(name).lower()
    return default if not v else v in {"1", "true", "yes", "si", "sí"}


# Discord
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = _int("GUILD_ID")
CREATOR_ROLE_NAME = os.getenv("CREATOR_ROLE_NAME", "creador de contenido")
CREATOR_ROLE_ID = _int("CREATOR_ROLE_ID")          # wins over the name if set
REVIEW_CHANNEL_ID = _int("REVIEW_CHANNEL_ID")
# Channel(s) where "X está en directo" announcements land; the bot mines them for social links.
ANNOUNCE_CHANNEL_IDS = _ids("ANNOUNCE_CHANNEL_IDS")
ANNOUNCE_HISTORY_LIMIT = _int("ANNOUNCE_HISTORY_LIMIT", 3000)
MOD_ROLE_IDS = _ids("MOD_ROLE_IDS")                # who may approve / request changes / dismiss
BACKFILL_EXISTING = _bool("BACKFILL_EXISTING", True)
# Existing creators are drip-fed so the mod channel isn't flooded: at most N per daily sweep.
BACKFILL_PER_SWEEP = _int("BACKFILL_PER_SWEEP", 3)
DAILY_SWEEP_HOUR = _int("DAILY_SWEEP_HOUR", 9)
TIMEZONE = os.getenv("TIMEZONE", "Europe/Madrid")

# Events workflow (bot/events.py). Channels/forums whose new messages and threads may announce an event.
# Defaults: anuncios, eventos, org_eventos (forum), servidores-gratuitos-del-club (forum).
EVENTS_ENABLED = _bool("EVENTS_ENABLED", True)
EVENT_CHANNEL_IDS = _ids("EVENT_CHANNEL_IDS") or {761675354049216584, 761675354049216587,
                                                   1465433666716045428, 1431767478056390838}
EVENT_REVIEW_CHANNEL_ID = _int("EVENT_REVIEW_CHANNEL_ID") or REVIEW_CHANNEL_ID  # mod channel for event cards
EVENT_TRACK_DAYS_AFTER = _int("EVENT_TRACK_DAYS_AFTER", 7)   # keep collecting news this long after it ends
EVENT_DEFAULT_HOURS = _int("EVENT_DEFAULT_HOURS", 2)         # length when the announcement gives no end
WP_EVENTS_CATEGORY = _env("WP_EVENTS_CATEGORY", "Eventos")
CALENDAR_URL = _env("CALENDAR_URL", "/calendario/")

# Momentazos (bot/momentazos.py): adds new clip compilations from YouTube to the /momentazos/ page. No AI.
MOMENTAZOS_ENABLED = _bool("MOMENTAZOS_ENABLED", True)
MOMENTAZOS_PAGE_ID = _int("MOMENTAZOS_PAGE_ID", 0)
MOMENTAZOS_CHANNEL_ID = _env("MOMENTAZOS_CHANNEL_ID", "UCXW5p9ZwR1kVsN2sEHNHylg")      # Club Ñ YouTube channel
MOMENTAZOS_PLAYLIST_ID = _env("MOMENTAZOS_PLAYLIST_ID", "PLYEqrW81W849ASmCYn_9uYClE_jn3D3J8")
MOMENTAZOS_INTERVAL_HOURS = _int("MOMENTAZOS_INTERVAL_HOURS", 6)
# Matched against the accent-less, lower-case title of each channel upload.
MOMENTAZOS_TITLE_REGEX = _env("MOMENTAZOS_TITLE_REGEX", r"momentazo|momentos de (streamers|creadores)|anuario momentazo")
MOMENTAZOS_ALLOW_DRAFT = _bool("MOMENTAZOS_ALLOW_DRAFT", False)   # only for testing on a draft page
MOMENTAZOS_NOTIFY_CHANNEL_ID = _int("MOMENTAZOS_NOTIFY_CHANNEL_ID", 0)  # 0 = don't post a note in Discord

# Ahora en directo (bot/live.py): who is live on Twitch / YouTube, shown on /ahora-en-directo/. No AI.
LIVE_ENABLED = _bool("LIVE_ENABLED", False)   # off until set in .env
# Private Discord thread/channel that holds the creator database (one bot message per creator).
LIVE_DB_CHANNEL_ID = _int("LIVE_DB_CHANNEL_ID", 1574896427719725137)
LIVE_DB_RELOAD_MIN = _int("LIVE_DB_RELOAD_MIN", 30)      # re-read the database this often
LIVE_PAGE_ID = _int("LIVE_PAGE_ID", 0)                   # WordPress page /ahora-en-directo/
LIVE_INTERVAL_MIN = _int("LIVE_INTERVAL_MIN", 2)         # Twitch check (and page update) interval
LIVE_YT_INTERVAL_MIN = _int("LIVE_YT_INTERVAL_MIN", 6)   # YouTube check interval (RSS + 1 quota unit / 50 videos)
LIVE_YT_FEED_ITEMS = _int("LIVE_YT_FEED_ITEMS", 4)       # newest uploads per channel to look at

# /mis_redes (bot/mis_redes.py): creators propose changes to their own entry in the creator database;
# a moderator approves each change before it is written. No AI.
MIS_REDES_ENABLED = _bool("MIS_REDES_ENABLED", False)          # off until set in .env
MIS_REDES_REVIEW_CHANNEL_ID = _int("MIS_REDES_REVIEW_CHANNEL_ID") or REVIEW_CHANNEL_ID
MIS_REDES_COOLDOWN_MIN = _int("MIS_REDES_COOLDOWN_MIN", 10)    # minimum gap between two requests of one creator
MIS_REDES_MAX_PER_DAY = _int("MIS_REDES_MAX_PER_DAY", 3)       # requests per creator in 24 h
MIS_REDES_MAX_PENDING = _int("MIS_REDES_MAX_PENDING", 25)      # open requests across everyone (protects the mod channel)
MIS_REDES_EXPIRE_DAYS = _int("MIS_REDES_EXPIRE_DAYS", 14)      # older open requests can no longer be approved
MIS_REDES_SELF_APPROVE = _bool("MIS_REDES_SELF_APPROVE", False)  # may a moderator approve their own request?
MIS_REDES_DM = _bool("MIS_REDES_DM", True)                     # tell the creator the outcome by DM

# WordPress
WP_URL = os.getenv("WP_URL", "https://wp.clubn.online").rstrip("/")
WP_FALLBACK_URL = os.getenv("WP_FALLBACK_URL", "https://clubn.online").rstrip("/")
WP_USER = os.environ["WP_USER"]
WP_APP_PASSWORD = os.environ["WP_APP_PASSWORD"]
WP_CATEGORY_NAME = os.getenv("WP_CATEGORY_NAME", "Creadores_de_contenido")
WP_FEATURED_TAG = os.getenv("WP_FEATURED_TAG", "destacado")   # added on top of "sticky"
WP_FRONT_BLOCK_TITLE = os.getenv("WP_FRONT_BLOCK_TITLE", "Nuevo creador en Club Ñ")

# Claude
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = os.getenv("CLAUDE_EFFORT", "high")
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")

# DeepSeek (fallback, or the only provider if ANTHROPIC_API_KEY is empty)
DEEPSEEK_API_KEY = _env("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL = _env("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")

# Optional social APIs (better data than scraping public pages)
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "")

DB_PATH = os.getenv("DB_PATH", "/data/bot.db")
