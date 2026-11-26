"""SuperAntonia's bot.db merge and the clubn_web safety nets (web_guards)."""
import ast
import asyncio
import datetime as dt
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from clubn_bot import legacy_db, web_guards

WEB = Path(__file__).resolve().parents[1] / "clubn_web"


def _schema(module: str, name: str) -> str:
    tree = ast.parse((WEB / f"{module}.py").read_text(encoding="utf-8"))
    return next(n.value.value for n in tree.body if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == name for t in n.targets))


ALL = [("db", "SCHEMA"), ("db", "EVENTS_SCHEMA"), ("momentazos", "SCHEMA"), ("mis_redes", "SCHEMA")]


def make_db(path: Path, parts=ALL) -> sqlite3.Connection:
    c = sqlite3.connect(path, isolation_level=None)
    for mod, name in parts:
        c.executescript(_schema(mod, name))
    return c


def creator(c, uid, status, **kw):
    row = {"discord_id": uid, "display_name": f"c{uid}", "status": status, **kw}
    c.execute(f"INSERT INTO creators ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))


def event(c, key, status, **kw):
    row = {"key": key, "name": key, "status": status, **kw}
    return c.execute(f"INSERT INTO events ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                     tuple(row.values())).lastrowid


def test_merge_restores_creators_events_and_history(tmp_path):
    target, legacy = tmp_path / "clubn_web.db", tmp_path / "bot.db"
    old = make_db(legacy)
    creator(old, 1, "published", wp_post_id=10)
    creator(old, 2, "published", wp_post_id=20)
    creator(old, 3, "pending_links")
    creator(old, 4, "in_review", wp_post_id=40)
    old_live = event(old, "msg:1", "live", wp_post_id=100, facts=json.dumps([{"url": "a"}]), facts_used=1)
    event(old, "se:2", "done")
    event(old, "msg:3", "live")
    old.execute("INSERT INTO media_cache VALUES ('img', 5, 'u')")
    old.execute("INSERT INTO momentazos_seen VALUES ('v1', 't', 'added', 1)")
    old.execute("INSERT INTO social_requests (discord_id, changes, before, status, nonce, created_at) "
                "VALUES (1, '{}', '{}', 'approved', 'n1', 1)")
    old.close()

    new = make_db(target, ALL[:2])  # momentazos / mis_redes tables not created yet
    creator(new, 2, "in_review", wp_post_id=99, review_message_id=555)  # re-asked after the restart
    creator(new, 4, "published", wp_post_id=40)  # moved on since the copy: kept
    creator(new, 5, "published")
    event(new, "msg:9", "new")  # takes id 1, the old live event's number
    event(new, "msg:1", "new", facts=json.dumps([{"url": "a"}, {"url": "b"}]))  # re-detected from scratch
    new.close()

    rep = legacy_db.merge(target, legacy)
    c = sqlite3.connect(target)
    c.row_factory = sqlite3.Row
    st = {r["discord_id"]: (r["status"], r["wp_post_id"]) for r in c.execute("SELECT * FROM creators")}
    assert st == {1: ("published", 10), 2: ("published", 20), 3: ("pending_links", None),
                  4: ("published", 40), 5: ("published", None)}
    assert rep["creators_added"] == 2
    assert [(r["discord_id"], r["duplicate_wp_post_id"]) for r in rep["creators_restored"]] == [(2, 99)]

    evs = {r["key"]: dict(r) for r in c.execute("SELECT * FROM events")}
    assert evs["msg:1"]["status"] == "live" and evs["msg:1"]["wp_post_id"] == 100 and evs["msg:1"]["id"] == 2
    assert [f["url"] for f in json.loads(evs["msg:1"]["facts"])] == ["a", "b"]  # "b" is news for the next update
    assert evs["se:2"]["status"] == "done" and evs["msg:3"]["status"] == "live"
    assert rep["events_added"] == 2 and old_live == 1
    assert c.execute("SELECT media_id FROM media_cache").fetchone()[0] == 5
    assert c.execute("SELECT count(*) FROM momentazos_seen").fetchone()[0] == 1
    assert c.execute("SELECT nonce FROM social_requests").fetchone()[0] == "n1"

    legacy_db.merge(target, legacy)  # running it twice changes nothing
    assert c.execute("SELECT count(*) FROM creators").fetchone()[0] == 5
    assert c.execute("SELECT count(*) FROM events").fetchone()[0] == 4
    assert c.execute("SELECT count(*) FROM social_requests").fetchone()[0] == 1


def test_import_runs_once_and_keeps_a_backup(tmp_path):
    target, legacy = tmp_path / "clubn_web.db", tmp_path / "bot.db"
    old = make_db(legacy)
    creator(old, 1, "published")
    old.close()
    make_db(target).close()
    assert legacy_db.import_if_present(target)["creators_added"] == 1
    assert not legacy.exists()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert any(n.startswith("bot.db.imported-") for n in names)
    assert any(n.startswith("clubn_web.db.before-import-") for n in names)
    assert legacy_db.import_if_present(target) is None


def test_no_legacy_file_or_same_file(tmp_path):
    assert legacy_db.import_if_present(tmp_path / "clubn_web.db") is None
    make_db(tmp_path / "bot.db").close()
    assert legacy_db.import_if_present(tmp_path / "bot.db") is None  # DB_PATH pointing at bot.db itself


def test_failed_merge_leaves_both_files(tmp_path, monkeypatch):
    target, legacy = tmp_path / "clubn_web.db", tmp_path / "bot.db"
    old = make_db(legacy)
    creator(old, 1, "published")
    old.close()
    make_db(target).close()

    def boom(*a):
        raise RuntimeError("x")

    monkeypatch.setattr(legacy_db, "_merge_events", boom)
    try:
        legacy_db.import_if_present(target)
    except RuntimeError:
        pass
    assert legacy.exists()
    assert sqlite3.connect(target).execute("SELECT count(*) FROM creators").fetchone()[0] == 0  # rolled back


# ---------- web_guards ----------

class FakeDB:
    def __init__(self):
        self.rows, self.events = {}, {}

    def get(self, uid):
        return self.rows.get(uid)

    def upsert(self, uid, **f):
        self.rows.setdefault(uid, {}).update(f)

    def event_by(self, col, val):
        return next((e for e in self.events.values() if e.get(col) == val), None)

    def event_save(self, eid, **f):
        self.events[eid].update(f)


class FakeWP:
    def __init__(self, posts):
        self.posts, self.calls = posts, []

    async def category_id(self):
        return 7

    async def _req(self, method, path, params=None):
        self.calls.append(params)
        return self.posts


def guards(posts, events_list=()):
    db = FakeDB()
    called = []

    async def handle_new_creator(member):
        called.append(("creator", member.id))

    async def on_scheduled_event(se, deleted=False):
        called.append(("se", se.id))

    web = SimpleNamespace(handle_new_creator=handle_new_creator, bot=SimpleNamespace(wp=FakeWP(posts)),
                          article_title=lambda n: f"Conozcamos más sobre {n} y su contenido")
    for e in events_list:
        db.events[e["id"]] = e
    status = SimpleNamespace(cancelled="c", completed="d", scheduled="s")
    events = SimpleNamespace(on_scheduled_event=on_scheduled_event, active_events=lambda: list(db.events.values()),
                             config=SimpleNamespace(EVENTS_ENABLED=True, GUILD_ID=1),
                             discord=SimpleNamespace(EventStatus=status),
                             _parse=lambda s: dt.datetime.fromisoformat(s) if s else None)
    web_guards.install(web, events, db)
    return web, events, db, called


MEMBER = SimpleNamespace(id=1, display_name="Flor", global_name="Flor", name="flor")


def test_published_article_on_wordpress_is_not_asked_again():
    web, _, db, called = guards([{"id": 9, "status": "publish", "title": {"raw": "Conozcamos más sobre Flor y su contenido"}}])
    asyncio.run(web.handle_new_creator(MEMBER))
    assert called == [] and db.rows[1]["status"] == "published" and db.rows[1]["wp_post_id"] == 9
    assert web.bot.wp.calls[0]["categories"] == 7


def test_existing_draft_is_reused_not_duplicated():
    web, _, db, called = guards([{"id": 8, "status": "draft", "title": {"raw": "Conozcamos más sobre Flor y su contenido"}}])
    asyncio.run(web.handle_new_creator(MEMBER))
    assert called == [] and db.rows[1]["wp_post_id"] == 8 and db.rows[1]["status"] == "error"


def test_new_creator_goes_through_and_known_ones_skip_the_check():
    web, _, db, called = guards([{"id": 9, "status": "publish", "title": {"raw": "Conozcamos más sobre Otra y su contenido"}}])
    asyncio.run(web.handle_new_creator(MEMBER))
    assert called == [("creator", 1)]
    db.rows[2] = {"status": "skipped_existing"}
    n = len(web.bot.wp.calls)
    asyncio.run(web.handle_new_creator(SimpleNamespace(id=2, display_name="X", global_name=None, name="x")))
    assert called[-1] == ("creator", 2) and len(web.bot.wp.calls) == n


def test_wordpress_down_skips_until_next_sweep():
    web, _, db, called = guards([])

    async def down(*a, **k):
        raise RuntimeError("503")

    web.bot.wp._req = down
    asyncio.run(web.handle_new_creator(MEMBER))
    assert called == [] and db.rows == {}


def test_scheduled_event_joins_the_event_followed_from_a_message():
    start = dt.datetime(2026, 12, 5, 19, tzinfo=dt.timezone.utc)
    ev = {"id": 3, "name": "Torneo de Navidad", "start_utc": start.isoformat(), "scheduled_id": None}
    _, events, db, called = guards([], [ev])
    se = SimpleNamespace(id=77, name="Torneo Navidad Club Ñ", start_time=start + dt.timedelta(hours=1),
                         status="s", guild_id=1)
    asyncio.run(events.on_scheduled_event(se))
    assert db.events[3]["scheduled_id"] == 77 and called == [("se", 77)]

    other = SimpleNamespace(id=78, name="Noche de cine", start_time=start, status="s", guild_id=1)
    asyncio.run(events.on_scheduled_event(other))
    assert db.events[3]["scheduled_id"] == 77  # unrelated event: left to the original as a new one
