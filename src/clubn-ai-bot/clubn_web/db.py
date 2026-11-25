"""Tiny SQLite store: one row per creator, tracking where they are in the review flow."""
import json
import sqlite3
import time
from pathlib import Path

from . import config

# status values:
#   skipped_existing  had the role before the bot started (and BACKFILL_EXISTING is off)
#   pending_links     waiting for /mis-redes (no social links known yet)
#   drafting          article being generated
#   in_review         draft in WordPress, review message posted in Discord
#   published         approved and live
#   dismissed         mods dismissed it
#   error             generation failed (see last_error)

SCHEMA = """
CREATE TABLE IF NOT EXISTS creators (
    discord_id        INTEGER PRIMARY KEY,
    display_name      TEXT,
    status            TEXT NOT NULL,
    links             TEXT NOT NULL DEFAULT '{}',
    wp_post_id        INTEGER,
    review_message_id INTEGER,
    version           INTEGER NOT NULL DEFAULT 0,
    feedback          TEXT NOT NULL DEFAULT '[]',
    article           TEXT,
    last_error        TEXT,
    manual_links      INTEGER NOT NULL DEFAULT 0,
    updated_at        REAL
);
CREATE INDEX IF NOT EXISTS idx_review_msg ON creators(review_message_id);
-- Images already uploaded to WordPress, so regenerating an article doesn't re-upload them.
CREATE TABLE IF NOT EXISTS media_cache (
    source     TEXT PRIMARY KEY,
    media_id   INTEGER NOT NULL,
    url        TEXT NOT NULL
);
"""

_JSON_COLS = {"links", "feedback", "article"}


def _conn() -> sqlite3.Connection:
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    with _conn() as c:
        c.executescript(SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(creators)")}
        if "manual_links" not in cols:  # upgrade databases created by the first version
            c.execute("ALTER TABLE creators ADD COLUMN manual_links INTEGER NOT NULL DEFAULT 0")


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in _JSON_COLS:
        if d.get(k):
            d[k] = json.loads(d[k])
    d.setdefault("links", {})
    d["links"] = d["links"] or {}
    d["feedback"] = d.get("feedback") or []
    return d


def get(discord_id: int) -> dict | None:
    with _conn() as c:
        return _row(c.execute("SELECT * FROM creators WHERE discord_id=?", (discord_id,)).fetchone())


def by_review_message(message_id: int) -> dict | None:
    with _conn() as c:
        return _row(c.execute("SELECT * FROM creators WHERE review_message_id=?", (message_id,)).fetchone())


def upsert(discord_id: int, **fields) -> dict:
    for k in _JSON_COLS & fields.keys():
        fields[k] = json.dumps(fields[k], ensure_ascii=False)
    fields["updated_at"] = time.time()
    with _conn() as c:
        if c.execute("SELECT 1 FROM creators WHERE discord_id=?", (discord_id,)).fetchone():
            sets = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE creators SET {sets} WHERE discord_id=?", (*fields.values(), discord_id))
        else:
            fields.setdefault("status", "pending_links")
            cols = ", ".join(["discord_id", *fields])
            qs = ", ".join("?" * (len(fields) + 1))
            c.execute(f"INSERT INTO creators ({cols}) VALUES ({qs})", (discord_id, *fields.values()))
    return get(discord_id)


def cached_media(source: str) -> tuple[int, str] | None:
    with _conn() as c:
        r = c.execute("SELECT media_id, url FROM media_cache WHERE source=?", (source,)).fetchone()
        return (r[0], r[1]) if r else None


def cache_media(source: str, media_id: int, url: str) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO media_cache (source, media_id, url) VALUES (?, ?, ?)",
                  (source, media_id, url))


def known_ids() -> set[int]:
    with _conn() as c:
        return {r[0] for r in c.execute("SELECT discord_id FROM creators")}


# ----------------------------------------------------------------------------- events
# One row per Club Ñ event the bot follows (bot/events.py).
# status values:
#   new        first draft written, waiting for the mods' ✅ (nothing public yet)
#   live       published; the bot keeps collecting news and proposing updates
#   done       tracking finished (EVENT_TRACK_DAYS_AFTER days after it ended)
#   dismissed  mods discarded the first draft
#   error      the first draft could not be written (see last_error)
# A pending update on a live event is stored in `draft` with review_message_id set.

EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    key               TEXT UNIQUE NOT NULL,   -- se:<scheduled event id> or msg:<message id>
    name              TEXT NOT NULL,
    start_utc         TEXT,                   -- ISO 8601, UTC
    end_utc           TEXT,
    location          TEXT,
    discord_url       TEXT,                   -- announcement / poll / discussion message
    thread_ids        TEXT NOT NULL DEFAULT '[]',  -- threads and channels whose messages are news for it
    scheduled_id      INTEGER,
    cancelled         INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL,
    wp_post_id        INTEGER,
    wp_link           TEXT,
    tec_id            INTEGER,
    tec_link          TEXT,
    review_message_id INTEGER,
    version           INTEGER NOT NULL DEFAULT 0,
    feedback          TEXT NOT NULL DEFAULT '[]',
    facts             TEXT NOT NULL DEFAULT '[]',  -- everything the bot learned, oldest first
    facts_used        INTEGER NOT NULL DEFAULT 0,  -- how many facts the published article already covers
    article           TEXT,                        -- last published article
    draft             TEXT,                        -- article waiting for review
    last_error        TEXT,
    last_draft_at     REAL,
    updated_at        REAL
);
CREATE INDEX IF NOT EXISTS idx_event_review ON events(review_message_id);
CREATE INDEX IF NOT EXISTS idx_event_sched ON events(scheduled_id);
"""
_EV_JSON = {"thread_ids", "feedback", "facts", "article", "draft"}


def events_init() -> None:
    with _conn() as c:
        c.executescript(EVENTS_SCHEMA)


def _ev(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in _EV_JSON:
        d[k] = json.loads(d[k]) if d.get(k) else ([] if k in ("thread_ids", "feedback", "facts") else None)
    return d


def event(eid: int) -> dict | None:
    with _conn() as c:
        return _ev(c.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone())


def event_by(column: str, value) -> dict | None:
    assert column in {"key", "scheduled_id", "review_message_id"}
    with _conn() as c:
        return _ev(c.execute(f"SELECT * FROM events WHERE {column}=?", (value,)).fetchone())


def events_with_status(*statuses: str) -> list[dict]:
    with _conn() as c:
        q = ",".join("?" * len(statuses))
        return [_ev(r) for r in c.execute(f"SELECT * FROM events WHERE status IN ({q}) ORDER BY start_utc", statuses)]


def event_save(eid: int | None, **fields) -> dict:
    for k in _EV_JSON & fields.keys():
        fields[k] = None if fields[k] is None else json.dumps(fields[k], ensure_ascii=False)
    fields["updated_at"] = time.time()
    with _conn() as c:
        if eid is None:
            cols = ", ".join(fields)
            cur = c.execute(f"INSERT INTO events ({cols}) VALUES ({', '.join('?' * len(fields))})",
                            tuple(fields.values()))
            eid = cur.lastrowid
        else:
            sets = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE events SET {sets} WHERE id=?", (*fields.values(), eid))
    return event(eid)


def event_add_fact(eid: int, fact: dict) -> dict:
    """Append something new (a message, a result, a time change). Duplicates by message url are skipped."""
    ev = event(eid)
    if fact.get("url") and any(f.get("url") == fact["url"] for f in ev["facts"]):
        return ev
    return event_save(eid, facts=ev["facts"] + [fact])
