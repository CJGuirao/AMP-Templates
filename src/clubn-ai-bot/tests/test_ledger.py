"""The Discord state ledger: payloads, restore on startup, and writes."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import discord

from clubn_bot import ledger as L
from test_legacy_db import make_db


class SqlDB:
    """Just the clubn_web.db functions the ledger uses, on a real SQLite file with the real schema."""

    def __init__(self, path):
        self.path = path
        make_db(path).close()

    def _conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def get(self, uid):
        with self._conn() as c:
            r = c.execute("SELECT * FROM creators WHERE discord_id=?", (uid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["links"] = json.loads(d["links"] or "{}")
        return d

    def upsert(self, uid, **f):
        if "links" in f:
            f["links"] = json.dumps(f["links"])
        with self._conn() as c:
            if c.execute("SELECT 1 FROM creators WHERE discord_id=?", (uid,)).fetchone():
                c.execute(f"UPDATE creators SET {', '.join(k + '=?' for k in f)} WHERE discord_id=?", (*f.values(), uid))
            else:
                f.setdefault("status", "pending_links")
                c.execute(f"INSERT INTO creators (discord_id, {', '.join(f)}) VALUES (?{', ?' * len(f)})", (uid, *f.values()))
        return self.get(uid)

    def event_by(self, col, val):
        with self._conn() as c:
            r = c.execute(f"SELECT * FROM events WHERE {col}=?", (val,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["thread_ids"] = json.loads(d["thread_ids"] or "[]")
        return d

    def event_save(self, eid, **f):
        if "thread_ids" in f:
            f["thread_ids"] = json.dumps(f["thread_ids"])
        with self._conn() as c:
            if eid is None:
                eid = c.execute(f"INSERT INTO events ({', '.join(f)}) VALUES ({', '.join('?' * len(f))})",
                                tuple(f.values())).lastrowid
            else:
                c.execute(f"UPDATE events SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), eid))
        return self.event_by("id", eid)


class FakeChannel:
    def __init__(self, me):
        self.me, self.msgs, self.sent, self.edited, self.next_id = me, {}, 0, 0, 1000

    def add(self, content, author=None):
        self.next_id += 1
        m = SimpleNamespace(id=self.next_id, content=content, author=SimpleNamespace(id=author or self.me))
        self.msgs[m.id] = m
        return m

    async def send(self, content, **k):
        self.sent += 1
        return self.add(content)

    def get_partial_message(self, mid):
        async def edit(content):
            if mid not in self.msgs:
                raise discord.NotFound(SimpleNamespace(status=404, reason="nf"), "gone")
            self.edited += 1
            self.msgs[mid].content = content
            return self.msgs[mid]
        return SimpleNamespace(edit=edit)

    async def history(self, limit=None, oldest_first=True):
        for m in list(self.msgs.values()):
            yield m


def make(tmp_path):
    db = SqlDB(tmp_path / "clubn_web.db")
    ch = FakeChannel(me=1)
    client = SimpleNamespace(user=SimpleNamespace(id=1), get_channel=lambda i: ch)
    led = L.Ledger(client, db, 555, pause=0)
    led.install()
    return db, ch, led


def test_channel_setting():
    assert L.channel_id({}, default=1574896427719725137) == 1574896427719725137  # blank: the creator DB thread
    assert L.channel_id({"LEDGER_CHANNEL_ID": "42"}, default=7) == 42
    assert L.channel_id({"LEDGER_CHANNEL_ID": "0"}, default=7) == 0
    assert L.channel_id({"LEDGER_CHANNEL_ID": "off"}, default=7) == 0


def test_render_parse_roundtrip_and_size():
    p = L.creator_payload({"discord_id": 5, "display_name": "Flor", "status": "published", "wp_post_id": 9,
                           "links": {"twitch": "https://twitch.tv/" + "x" * 3000}})
    text = L.render(p)
    assert len(text) <= L.MAX_LEN and L.parse(text) == {k: v for k, v in p.items() if k != "links"}
    assert L.parse("🎥 **Flor** ```json\n{\"schema\": \"clubn-creator/1\", \"discord_id\": 5}\n```") is None  # live records
    assert L.parse(L.HEADER + "\n```json\nnot json\n```") is None


def test_changes_are_written_once_and_then_edited(tmp_path):
    db, ch, led = make(tmp_path)
    db.upsert(5, display_name="Flor", status="in_review", wp_post_id=9)
    db.upsert(5, feedback="[]")  # not a ledger field: nothing new to write
    assert led.dirty == {("creator", "5")}
    assert asyncio.run(led.flush()) == 1 and ch.sent == 1
    db.upsert(5, status="published")
    asyncio.run(led.flush())
    assert ch.sent == 1 and ch.edited == 1
    assert L.parse(next(iter(ch.msgs.values())).content)["status"] == "published"
    db.upsert(5, status="published")  # same payload: no Discord call
    asyncio.run(led.flush())
    assert ch.edited == 1
    db.event_save(None, key="msg:1", name="Torneo", status="new")
    asyncio.run(led.flush())
    assert ch.sent == 2


def test_deleted_message_is_sent_again(tmp_path):
    db, ch, led = make(tmp_path)
    db.upsert(5, display_name="Flor", status="published")
    asyncio.run(led.flush())
    ch.msgs.clear()
    db.upsert(5, wp_post_id=10)
    asyncio.run(led.flush())
    assert ch.sent == 2


def test_lost_database_is_rebuilt_from_discord(tmp_path):
    db, ch, led = make(tmp_path)
    db.upsert(5, display_name="Flor", status="published", wp_post_id=9)
    db.upsert(6, display_name="Leo", status="pending_links")
    db.event_save(None, key="se:7", name="Torneo", status="live", wp_post_id=70, thread_ids=[11], scheduled_id=7)
    asyncio.run(led.flush())
    ch.add(L.HEADER + "\n```json\n" + json.dumps({"t": "creator", "id": 8, "status": "published"}) + "\n```",
           author=2)  # someone else's message in the channel: ignored

    (tmp_path / "new").mkdir()  # the database file is gone: a fresh one
    db2, _, led2 = make(tmp_path / "new")
    led2.client, led2._channel = led.client, ch
    db2.upsert(6, display_name="Leo", status="in_review")  # newer than the ledger: kept
    counts = led2.restore(asyncio.run(led2.read()))
    assert counts == {"creators": 1, "events": 1}
    assert db2.get(5)["status"] == "published" and db2.get(5)["wp_post_id"] == 9
    assert db2.get(6)["status"] == "in_review" and db2.get(8) is None
    ev = db2.event_by("key", "se:7")
    assert ev["status"] == "live" and ev["wp_post_id"] == 70 and ev["thread_ids"] == [11] and ev["scheduled_id"] == 7

    led2.dirty |= led2.all_keys()
    asyncio.run(led2.flush())
    assert ch.sent == 3 and ch.edited == 1  # only Leo's message changed; nothing re-sent


def test_sweep_waits_for_the_ledger(tmp_path):
    db, ch, led = make(tmp_path)
    order = []

    async def sweep():
        order.append("sweep")

    async def go():
        gated = led.gate(sweep)
        t = asyncio.create_task(gated())
        await asyncio.sleep(0.01)
        order.append("ready")
        led.ready.set()
        await t

    asyncio.run(go())
    assert order == ["ready", "sweep"]


def test_merged_installs_the_ledger_in_the_creator_db_thread(tmp_path):
    from clubn_bot import ledger

    web = SimpleNamespace(config=SimpleNamespace(LIVE_DB_CHANNEL_ID=1574896427719725137), run_sweep=None)
    events = SimpleNamespace(sync_scheduled_events=None)

    async def noop():
        return None

    web.run_sweep = events.sync_scheduled_events = noop
    db = SqlDB(tmp_path / "x.db")
    led = ledger.setup(SimpleNamespace(), web, events, db, env={})
    assert led.channel_id == 1574896427719725137 and web.run_sweep is not noop
    assert ledger.setup(SimpleNamespace(), web, events, SqlDB(tmp_path / "y.db"), env={"LEDGER_CHANNEL_ID": "0"}) is None
