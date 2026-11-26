"""A copy of the important creator and event state in a private Discord channel (the "ledger").

SQLite (clubn_web.db) stays the working database. The ledger keeps, per creator and per event, one
small message written by the bot with what must never be forgotten: status, WordPress post, review
card, and for events their dates, threads and calendar entry. Bulky working data (event news, drafts,
mod feedback) stays in SQLite only.

- Writing: clubn_web.db.upsert / event_save are wrapped, so any change to those fields marks the row;
  a background loop edits (or first sends) its message a few seconds later, gently rate-limited.
- Startup: before the first creator sweep or scheduled-event sync, the bot reads the ledger and fills
  in what SQLite is missing (or is behind on: e.g. "published" beats "pending_links"). If clubn_web.db
  is lost, moved or replaced, nobody is asked twice and followed events keep their article.
- Then every row whose ledger message is missing or stale is written, so the first start fills it.

By default it lives in the private creator database thread (LIVE_DB_CHANNEL_ID), next to the records
the live checker keeps there: each side only reads its own kind of message (the live checker wants
"schema": "clubn-creator/1", the ledger wants the "clubn-ledger" header). LEDGER_CHANNEL_ID (AMP: Club Ñ
Web > Discord and creators > State Ledger Channel ID) picks another private channel; 0 turns it off.
Only the bot's own messages are read.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

import discord

from .legacy_db import CREATOR_RANK, EVENT_RANK

log = logging.getLogger("clubn_bot.ledger")

HEADER = "🗂️ clubn-ledger"
_BODY = re.compile(r"```json\n(.*)\n```", re.S)
MAX_LEN = 1900

CREATOR_FIELDS = ("display_name", "status", "wp_post_id", "review_message_id", "links", "manual_links")
EVENT_FIELDS = ("name", "start_utc", "end_utc", "location", "discord_url", "thread_ids", "scheduled_id",
                "cancelled", "status", "wp_post_id", "wp_link", "tec_id", "tec_link")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_messages (
    kind        TEXT NOT NULL,     -- creator | event
    key         TEXT NOT NULL,     -- discord id | event key
    message_id  INTEGER NOT NULL,
    payload     TEXT NOT NULL,     -- what the message says now
    PRIMARY KEY (kind, key)
);
"""


def channel_id(env: dict | None = None, default: int | None = None) -> int:
    """LEDGER_CHANNEL_ID; blank = `default` (the creator database thread), 0 or "off" = no ledger."""
    v = ((os.environ if env is None else env).get("LEDGER_CHANNEL_ID") or "").split("#", 1)[0].strip().lower()
    if not v:
        return default or 0
    return int(v) if v.isdigit() else 0


# ----------------------------------------------------------------------------- payloads

def creator_payload(row: dict) -> dict:
    p = {"t": "creator", "id": row["discord_id"], **{k: row.get(k) for k in CREATOR_FIELDS}}
    return {k: v for k, v in p.items() if v not in (None, {}, [], "")}


def event_payload(ev: dict) -> dict:
    p = {"t": "event", "key": ev["key"], **{k: ev.get(k) for k in EVENT_FIELDS}}
    return {k: v for k, v in p.items() if v not in (None, {}, [], "")}


def render(payload: dict) -> str:
    def text(p):
        return f"{HEADER}\n```json\n{json.dumps(p, ensure_ascii=False, sort_keys=True)}\n```"

    out = text(payload)
    for drop in ("links", "location", "discord_url"):  # never needed to stop a re-ask; dropped if too long
        if len(out) <= MAX_LEN:
            break
        payload = {k: v for k, v in payload.items() if k != drop}
        out = text(payload)
    if len(out) > MAX_LEN and "thread_ids" in payload:
        payload = {**payload, "thread_ids": payload["thread_ids"][-20:]}
        out = text(payload)
    return out


def parse(content: str) -> dict | None:
    if not content.startswith(HEADER):
        return None
    m = _BODY.search(content)
    try:
        p = json.loads(m[1]) if m else None
    except ValueError:
        return None
    if not isinstance(p, dict) or p.get("t") not in ("creator", "event"):
        return None
    if p["t"] == "creator" and not isinstance(p.get("id"), int) or p["t"] == "event" and not p.get("key"):
        return None
    return p


def key_of(p: dict) -> tuple[str, str]:
    return (p["t"], str(p["id"] if p["t"] == "creator" else p["key"]))


# ----------------------------------------------------------------------------- the ledger

class Ledger:
    def __init__(self, client: discord.Client, db, channel: int, *, pause: float = 1.2, every: float = 5.0) -> None:
        self.client, self.db, self.channel_id = client, db, channel
        self.pause, self.every = pause, every
        self.ready = asyncio.Event()
        self.dirty: set[tuple[str, str]] = set()
        self._channel = None
        self._task: asyncio.Task | None = None
        self._upsert, self._event_save = db.upsert, db.event_save
        with db._conn() as c:
            c.executescript(SCHEMA)

    # -- hooks ---------------------------------------------------------------
    def install(self) -> None:
        db, upsert, event_save = self.db, self._upsert, self._event_save

        def upsert_and_mark(discord_id, **fields):
            tracked = bool(set(fields) & set(CREATOR_FIELDS))
            row = upsert(discord_id, **fields)
            if tracked:
                self.dirty.add(("creator", str(discord_id)))
            return row

        def event_save_and_mark(eid, **fields):
            tracked = eid is None or bool(set(fields) & set(EVENT_FIELDS))
            ev = event_save(eid, **fields)
            if tracked and ev:
                self.dirty.add(("event", ev["key"]))
            return ev

        db.upsert, db.event_save = upsert_and_mark, event_save_and_mark

    def gate(self, fn):
        """fn, waiting first (up to 2 minutes) for the ledger to be read."""
        async def gated(*a, **k):
            try:
                await asyncio.wait_for(self.ready.wait(), 120)
            except asyncio.TimeoutError:
                log.warning("Ledger not read after 2 minutes; going on without it")
            return await fn(*a, **k)
        return gated

    # -- stored message ids ----------------------------------------------------
    def _stored(self) -> dict[tuple[str, str], tuple[int, str]]:
        with self.db._conn() as c:
            return {(r[0], r[1]): (r[2], r[3]) for r in c.execute("SELECT kind, key, message_id, payload FROM ledger_messages")}

    def _remember(self, k: tuple[str, str], message_id: int, text: str) -> None:
        with self.db._conn() as c:
            c.execute("INSERT OR REPLACE INTO ledger_messages VALUES (?, ?, ?, ?)", (*k, message_id, text))

    def current(self, k: tuple[str, str]) -> str | None:
        kind, key = k
        if kind == "creator":
            row = self.db.get(int(key))
            return render(creator_payload(row)) if row else None
        ev = self.db.event_by("key", key)
        return render(event_payload(ev)) if ev else None

    # -- startup ---------------------------------------------------------------
    async def channel(self):
        if self._channel is None:
            self._channel = self.client.get_channel(self.channel_id) or await self.client.fetch_channel(self.channel_id)
        if isinstance(self._channel, discord.Thread) and self._channel.archived:
            await self._channel.edit(archived=False)
        return self._channel

    async def read(self) -> dict[tuple[str, str], tuple[int, dict, str]]:
        """(kind, key) -> (message id, payload, text) of the bot's newest ledger message for it."""
        found = {}
        ch = await self.channel()
        me = self.client.user.id
        async for m in ch.history(limit=None, oldest_first=True):
            if m.author.id == me and (p := parse(m.content)):
                found[key_of(p)] = (m.id, p, m.content)
        return found

    def restore(self, found: dict) -> dict:
        """Fill SQLite from the ledger: missing rows, and rows behind the ledger. Returns counts."""
        out = {"creators": 0, "events": 0}
        for k, (mid, p, text) in found.items():
            self._remember(k, mid, text)
            if p["t"] == "creator":
                row = self.db.get(p["id"])
                if row is None or CREATOR_RANK.get(p.get("status"), 0) > CREATOR_RANK.get(row["status"], 0):
                    fields = {f: p[f] for f in CREATOR_FIELDS if f in p}
                    fields.setdefault("status", "pending_links")
                    self._upsert(p["id"], **fields)
                    out["creators"] += 1
                    log.info("Ledger: creator %s (%s) restored as %s", p.get("display_name"), p["id"], fields["status"])
            else:
                ev = self.db.event_by("key", p["key"])
                if ev is None or EVENT_RANK.get(p.get("status"), 0) > EVENT_RANK.get(ev["status"], 0):
                    fields = {f: p[f] for f in EVENT_FIELDS if f in p}
                    fields.setdefault("name", p["key"])
                    fields.setdefault("status", "new")
                    if ev is None:
                        self._event_save(None, key=p["key"], **fields)
                    else:
                        self._event_save(ev["id"], **fields)
                    out["events"] += 1
                    log.info("Ledger: event %s restored as %s", fields["name"], fields["status"])
        return out

    def all_keys(self) -> set[tuple[str, str]]:
        with self.db._conn() as c:
            keys = {("creator", str(r[0])) for r in c.execute("SELECT discord_id FROM creators")}
            keys |= {("event", r[0]) for r in c.execute("SELECT key FROM events")}
        return keys

    async def start(self) -> None:
        try:
            await self.client.wait_until_ready()
            counts = self.restore(await self.read())
            log.info("Ledger read: %d creators and %d events filled in from Discord", counts["creators"], counts["events"])
            self.dirty |= self.all_keys()  # anything missing or stale gets written below
        except Exception:
            log.exception("Could not read the state ledger (channel %s); the bot goes on without it", self.channel_id)
        finally:
            self.ready.set()
        while True:
            await self.flush()
            await asyncio.sleep(self.every)

    # -- writing ---------------------------------------------------------------
    async def flush(self) -> int:
        if not self.dirty:
            return 0
        stored = self._stored()
        written = 0
        for k in sorted(self.dirty):
            self.dirty.discard(k)
            text = self.current(k)
            mid, old = stored.get(k, (None, None))
            if text is None or text == old:
                continue
            try:
                ch = await self.channel()
                msg = None
                if mid:
                    try:
                        msg = await ch.get_partial_message(mid).edit(content=text)
                    except discord.NotFound:
                        msg = None
                if msg is None:
                    msg = await ch.send(text, allowed_mentions=discord.AllowedMentions.none())
                self._remember(k, msg.id, text)
                written += 1
            except discord.HTTPException as e:
                self.dirty.add(k)
                log.warning("Ledger write failed for %s %s: %s; retrying in a minute", *k, e)
                await asyncio.sleep(60)
                break
            await asyncio.sleep(self.pause)
        return written


def setup(client: discord.Client, web, events, db, env: dict | None = None) -> Ledger | None:
    """Install the ledger unless it is turned off. Call ledger.start() as a task from setup_hook."""
    cid = channel_id(env, default=getattr(web.config, "LIVE_DB_CHANNEL_ID", None))
    if not cid:
        return None
    ledger = Ledger(client, db, cid)
    ledger.install()
    web.run_sweep = ledger.gate(web.run_sweep)                            # no creator sweep before the ledger is read
    events.sync_scheduled_events = ledger.gate(events.sync_scheduled_events)  # nor a scheduled-event sync
    return ledger
