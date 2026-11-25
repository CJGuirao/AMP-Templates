"""Social posts: text limits, routes, the publishing clients (mocked HTTP) and the review flow (fake Discord)."""
import asyncio
import datetime as dt
import json
from types import SimpleNamespace

import discord
import httpx
import pytest

from clubn_bot import social_posts as sp
from clubn_bot.social_networks import (LIMIT, Post, Router, Settings, final_text, fit, hashtag_facets, length,
                                       x_length)


def run(coro):
    return asyncio.run(coro)


# ---------- settings ----------

def test_defaults_are_off_and_in_test_mode():
    s = Settings.from_env({})
    assert s.enabled is False and s.dry_run is True
    assert s.networks == ("x", "bluesky", "threads", "facebook", "instagram", "tiktok")
    assert s.hashtags == "#ClubÑ" and s.wp_categories == ("Creadores_de_contenido", "Eventos")
    assert not any(s.direct(n) for n in s.networks)


def test_settings_parsing():
    s = Settings.from_env({"SOCIAL_ENABLED": "true", "SOCIAL_DRY_RUN": "false", "SOCIAL_NETWORKS": "X, bluesky,nope",
                           "SOCIAL_WP_CATEGORIES": "*", "SOCIAL_HASHTAGS": "ClubÑ, #Gaming", "SOCIAL_POLL_MIN": "x",
                           "SOCIAL_TIKTOK_METHOD": "weird", "BLUESKY_HANDLE": "@club.bsky.social",
                           "BLUESKY_APP_PASSWORD": "pw"})
    assert s.enabled and not s.dry_run and s.networks == ("x", "bluesky") and s.wp_categories == ()
    assert s.hashtags == "#ClubÑ #Gaming" and s.poll_min == 10 and s.tiktok_method == "UPLOAD"
    assert s.bluesky_handle == "club.bsky.social" and s.direct("bluesky") and not s.direct("threads")
    assert "pw" not in repr(s)


# ---------- text ----------

def test_x_counts_links_as_23_and_emoji_double():
    assert x_length("hola https://example.com/a/very/long/path/that/is/long") == 5 + 23
    assert x_length("🎮") == 2


def test_fit_keeps_every_network_within_its_limit():
    url = "https://wp.clubn.online/2026/11/25/un-articulo-con-un-slug-muy-largo/"
    long = ("Conoce a nuestra nueva creadora 🎮 que hace directos de terror cada viernes " * 60) + " #ClubÑ"
    for net in LIMIT:
        t = fit(net, long + " https://evil.example/x", url)
        assert "evil.example" not in t
        assert length(net, final_text(net, t, url)) <= LIMIT[net], net
        assert t.endswith("…")
    assert final_text("x", "hola", url).endswith(url) and final_text("bluesky", "hola", url) == "hola"
    assert "bio" in final_text("instagram", "hola", url)


def test_bluesky_hashtag_facets_use_utf8_byte_offsets():
    text = "Año nuevo en el #ClubÑ y #Gaming."
    facets = hashtag_facets(text)
    raw = text.encode()
    assert [raw[f["index"]["byteStart"]:f["index"]["byteEnd"]].decode() for f in facets] == ["#ClubÑ", "#Gaming"]
    assert facets[0]["features"][0]["tag"] == "ClubÑ"


# ---------- routes and clients ----------

class Recorder:
    def __init__(self, handler):
        self.requests = []
        self.handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def router(settings: Settings, handler) -> tuple[Router, Recorder]:
    rec = Recorder(handler)
    store = {}

    async def no_sleep(_s):
        return None

    r = Router(settings, http=httpx.AsyncClient(transport=httpx.MockTransport(rec)),
               token_store=(store.get, store.__setitem__), sleep=no_sleep)
    return r, rec


POSTIZ_CHANNELS = [{"id": "ig1", "identifier": "instagram", "disabled": False},
                   {"id": "x1", "identifier": "x", "disabled": False},
                   {"id": "tt1", "identifier": "tiktok", "disabled": True}]


def postiz_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    assert request.headers["Authorization"] == "pk"
    if path.endswith("/integrations"):
        return httpx.Response(200, json=POSTIZ_CHANNELS)
    if path.endswith("/upload-from-url"):
        return httpx.Response(200, json={"id": "m1", "path": "https://uploads.postiz.com/m1.jpg"})
    if path.endswith("/posts"):
        return httpx.Response(200, json=[{"postId": "p1", "integration": "x"}])
    return httpx.Response(404)


POST = Post("Conozcamos a Flor", "Directos de terror", "https://wp.clubn.online/flor/", "https://wp.clubn.online/f.jpg")


def test_routes_prefer_direct_then_postiz_then_manual():
    s = Settings(postiz_key="pk", bluesky_handle="h", bluesky_password="p")
    r, _ = router(s, postiz_handler)
    routes = run(r.routes())
    assert routes == {"x": "postiz", "bluesky": "direct", "threads": "manual", "facebook": "manual",
                      "instagram": "postiz", "tiktok": "manual"}  # disabled Postiz channel = manual


def test_dry_run_calls_no_publishing_endpoint():
    r, rec = router(Settings(postiz_key="pk", dry_run=True), postiz_handler)
    res = run(r.publish("x", "hola", POST))
    assert res.ok and res.route == "postiz" and "prueba" in res.detail
    assert all(not q.url.path.endswith(("/posts", "/upload-from-url")) for q in rec.requests)
    assert not run(r.publish("threads", "hola", POST)).ok  # manual is never "done"


def test_postiz_instagram_uploads_the_image_and_sends_the_settings():
    r, rec = router(Settings(postiz_key="pk", dry_run=False), postiz_handler)
    res = run(r.publish("instagram", "Pie de foto #ClubÑ", POST))
    assert res.ok and res.remote_id == "p1"
    body = json.loads(next(q for q in rec.requests if q.url.path.endswith("/posts")).content)
    item = body["posts"][0]
    assert body["type"] == "now" and item["integration"] == {"id": "ig1"}
    assert item["settings"]["__type"] == "instagram" and item["settings"]["post_type"] == "post"
    assert item["value"][0]["image"][0]["path"] == "https://uploads.postiz.com/m1.jpg"
    assert item["value"][0]["content"].endswith("Enlace en la bio.")


def test_postiz_x_text_carries_the_link():
    r, rec = router(Settings(postiz_key="pk", dry_run=False), postiz_handler)
    assert run(r.publish("x", "Nuevo artículo", POST)).ok
    body = json.loads(next(q for q in rec.requests if q.url.path.endswith("/posts")).content)
    assert body["posts"][0]["settings"] == {"__type": "x", "who_can_reply_post": "everyone"}
    assert body["posts"][0]["value"][0]["content"] == "Nuevo artículo\n\nhttps://wp.clubn.online/flor/"
    assert not any(q.url.path.endswith("/upload-from-url") for q in rec.requests)


def test_instagram_without_image_is_refused_before_any_call():
    r, rec = router(Settings(postiz_key="pk", dry_run=False), postiz_handler)
    res = run(r.publish("instagram", "hola", Post("t", "s", "https://x.y/", None)))
    assert not res.ok and "imagen" in res.detail
    assert all(q.url.path.endswith("/integrations") for q in rec.requests)


def test_bluesky_direct_posts_with_facets_and_link_card():
    def h(request):
        p = request.url.path
        if p.endswith("createSession"):
            return httpx.Response(200, json={"accessJwt": "jwt", "did": "did:plc:1", "handle": "club.bsky.social"})
        if request.url.host == "wp.clubn.online":
            return httpx.Response(200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"})
        if p.endswith("uploadBlob"):
            assert request.headers["Authorization"] == "Bearer jwt"
            return httpx.Response(200, json={"blob": {"$type": "blob", "ref": "b"}})
        if p.endswith("createRecord"):
            return httpx.Response(200, json={"uri": "at://did:plc:1/app.bsky.feed.post/3abc", "cid": "c"})
        return httpx.Response(404)

    r, rec = router(Settings(dry_run=False, bluesky_handle="club.bsky.social", bluesky_password="pw"), h)
    res = run(r.publish("bluesky", "Conoce a Flor #ClubÑ", POST))
    assert res.ok and res.url == "https://bsky.app/profile/club.bsky.social/post/3abc"
    rec_body = json.loads(next(q for q in rec.requests if q.url.path.endswith("createRecord")).content)
    record = rec_body["record"]
    assert record["text"] == "Conoce a Flor #ClubÑ" and record["facets"][0]["features"][0]["tag"] == "ClubÑ"
    assert record["embed"]["external"]["uri"] == POST.url and record["embed"]["external"]["thumb"]["ref"] == "b"


def test_errors_become_a_failed_result_not_an_exception():
    def h(request):
        return httpx.Response(401, json={"error": {"message": "Invalid OAuth access token"}})

    r, _ = router(Settings(dry_run=False, facebook_page_id="1", meta_token="t"), h)
    res = run(r.publish("facebook", "hola", POST))
    assert not res.ok and res.route == "direct" and "Invalid OAuth" in res.detail and "t" != res.detail


def test_threads_token_refresh_is_kept_for_the_same_amp_token():
    def h(request):
        return httpx.Response(200, json={"access_token": "refreshed", "expires_in": 5184000})

    r, _ = router(Settings(dry_run=False, threads_token="original"), h)
    assert r.threads_token() == "original"
    assert run(r.refresh_threads_token())
    assert r.threads_token() == "refreshed"
    r.s.threads_token = "pasted-again-in-amp"  # a new token in AMP wins over the stored copy
    assert r.threads_token() == "pasted-again-in-amp"


# ---------- storage ----------

def test_claim_is_atomic_and_tied_to_the_current_card(tmp_path):
    st = sp.Store(tmp_path / "s.db")
    row = st.save(None, source="wp:1", title="t", status="in_review", review_message_id=10)
    assert not st.claim(row["id"], 99, "publishing")       # an old card
    assert st.claim(row["id"], 10, "publishing")
    assert not st.claim(row["id"], 10, "publishing")       # second click
    st.save(None, source="wp:2", title="t2", status="drafting")
    st.recover()
    assert st.get(row["id"])["status"] == "in_review" and st.by("source", "wp:2")["status"] == "error"


# ---------- review flow with a fake Discord ----------

class FakeMessage:
    _next = 100

    def __init__(self, **kw):
        FakeMessage._next += 1
        self.id = FakeMessage._next
        self.kw = kw
        self.edits = []
        self.embeds = [kw["embed"]] if kw.get("embed") else []

    async def edit(self, **kw):
        self.edits.append(kw)
        if kw.get("embed"):
            self.embeds = [kw["embed"]]


class FakeChannel:
    id = 555

    def __init__(self):
        self.sent: list[FakeMessage] = []

    async def send(self, content=None, **kw):
        m = FakeMessage(content=content, **kw)
        self.sent.append(m)
        return m

    async def fetch_message(self, mid):
        return next(m for m in self.sent if m.id == mid)


class FakeWP:
    def __init__(self, posts):
        self.posts = posts

    async def _req(self, method, path, params=None, **kw):
        if path == "categories":
            return [{"id": 7, "name": "Creadores_de_contenido"}, {"id": 8, "name": "Eventos"}]
        if path == "posts":
            return list(self.posts)
        if path.startswith("media/"):
            return {"mime_type": "image/webp", "source_url": "https://wp/x.webp",
                    "media_details": {"sizes": {"a": {"mime_type": "image/jpeg", "width": 300, "source_url": "https://wp/s.jpg"},
                                                "b": {"mime_type": "image/jpeg", "width": 1200, "source_url": "https://wp/l.jpg"}}}}
        raise AssertionError(path)


def wp_post(pid, hours_ago=1, cat=7):
    when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)
    return {"id": pid, "link": f"https://wp.clubn.online/p{pid}/", "title": {"rendered": f"Artículo &amp; {pid}"},
            "excerpt": {"rendered": "<p>Entradilla</p>"}, "content": {"rendered": "<p>Texto</p>"},
            "date_gmt": when.strftime("%Y-%m-%dT%H:%M:%S"), "featured_media": 3, "categories": [cat]}


def make_app(tmp_path, posts=(), handler=postiz_handler, **settings):
    s = Settings(postiz_key="pk", **settings)
    ch = FakeChannel()
    calls = []

    async def structured(system, prompt, schema):
        calls.append((system, prompt, schema))
        return schema(**{n: f"Texto {n} " + "muy largo " * 400 for n in schema.model_fields})

    r, rec = router(s, handler)
    app = sp.Social(SimpleNamespace(get_channel=lambda _id: ch), s, sp.Store(tmp_path / "s.db"), r, structured,
                    is_mod=lambda u: True, wp=FakeWP(posts), default_review_channel=555)
    sp._app = app
    return app, ch, calls, rec


def test_draft_posts_a_card_within_discord_limits(tmp_path):
    app, ch, calls, _ = make_app(tmp_path)

    async def go():
        return await app.create(source="manual:1", title="Torneo de Ñ", summary="Este sábado", body="Detalles",
                                link="https://wp.clubn.online/t/", image_url="https://wp/i.jpg")

    row = run(go())
    assert row["status"] == "in_review" and row["review_message_id"] == ch.sent[-1].id
    card = ch.sent[-1].kw
    assert len(card["embed"]) <= 6000 and len(card["embed"].fields) <= 25
    for n, t in row["texts"].items():
        assert length(n, final_text(n, t, row["link"])) <= LIMIT[n]
    system, prompt, schema = calls[0]
    assert "NO escribas enlaces" in system and "#ClubÑ" in prompt and "x=" in prompt
    ids = [c.custom_id for c in card["view"].children]
    assert ids[:4] == [f"social:{a}:{row['id']}" for a in ("publish", "changes", "edit", "dismiss")]


def test_publish_partial_failure_retries_only_what_failed(tmp_path):
    state = {"x_fails": True}

    def h(request):
        if request.url.path.endswith("/posts"):
            body = json.loads(request.content)
            if body["posts"][0]["integration"]["id"] == "x1" and state["x_fails"]:
                return httpx.Response(500, json={"message": "boom"})
        return postiz_handler(request)

    app, ch, _, rec = make_app(tmp_path, handler=h, dry_run=False)
    mod = SimpleNamespace(id=42)

    async def go():
        row = await app.create(source="manual:2", title="T", link="https://wp/t/", image_url="https://wp/i.jpg")
        app.store.save(row["id"], networks=["x", "instagram", "threads"])
        row, lines = await app.publish(row["id"], mod)
        assert row["status"] == "in_review" and row["networks"] == ["x"]  # threads is manual, not "failed"
        assert row["results"]["instagram"]["ok"] and not row["results"]["x"]["ok"]
        state["x_fails"] = False
        assert app.store.claim(row["id"], row["review_message_id"], "publishing")
        row, _ = await app.publish(row["id"], mod)
        return row

    row = run(go())
    assert row["status"] == "done" and row["results"]["x"]["ok"]
    posted = [json.loads(q.content)["posts"][0]["integration"]["id"] for q in rec.requests if q.url.path.endswith("/posts")]
    assert posted.count("ig1") == 1 and posted.count("x1") == 2  # Instagram never posted twice


def test_done_posts_are_not_proposed_again(tmp_path):
    app, *_ = make_app(tmp_path)

    async def go():
        row = await app.create(source="wp:9", title="T")
        app.store.save(row["id"], status="done")
        with pytest.raises(RuntimeError, match="ya se publicó"):
            await app.create(source="wp:9", title="T")

    run(go())


def test_rewrite_keeps_feedback_and_replaces_the_card(tmp_path):
    app, ch, calls, _ = make_app(tmp_path)

    async def go():
        row = await app.create(source="manual:3", title="T")
        first = row["review_message_id"]
        assert app.store.claim(row["id"], first, "drafting")
        await app.draft(row["id"], "Mod: menos emojis")
        return first, app.store.get(row["id"])

    first, row = run(go())
    assert row["review_message_id"] != first and row["version"] == 2 and row["feedback"] == ["Mod: menos emojis"]
    assert "menos emojis" in calls[-1][1] and "Textos actuales" in calls[-1][1]
    old = next(m for m in ch.sent if m.id == first)
    assert old.edits[-1]["view"] is None  # old card closed


def test_poll_first_run_is_a_baseline_then_drafts_new_posts(tmp_path):
    app, ch, _, _ = make_app(tmp_path, posts=[wp_post(1), wp_post(2)])

    async def go():
        assert await app.poll() == 0 and not ch.sent       # existing posts are not news
        app.wp.posts = [wp_post(4, hours_ago=100), wp_post(3), wp_post(1), wp_post(2)]
        assert await app.poll() == 1                       # 3 is new; 4 is too old
        assert await app.poll() == 0                       # nothing twice

    run(go())
    row = app.store.by("source", "wp:3")
    assert row["kind"] == "creador" and row["title"] == "Artículo & 3" and row["image_url"] == "https://wp/l.jpg"
    assert app.store.by("source", "wp:4") is None


def test_wp_link_parsing():
    assert sp._wp_post_id("123") == ("id", "123")
    assert sp._wp_post_id("https://wp.clubn.online/?p=55&preview=true") == ("id", "55")
    assert sp._wp_post_id("https://wp.clubn.online/2026/11/flor-y-su-contenido/") == ("slug", "flor-y-su-contenido")


def test_setup_registers_the_mod_commands():
    async def go():
        client = discord.Client(intents=discord.Intents.none())
        client.tree = discord.app_commands.CommandTree(client)
        app = sp.setup(client, structured=None, is_mod=lambda u: True, wp=None, db_path=":memory:",
                       default_review_channel=1, settings=Settings(enabled=True))
        names = {c.name for c in client.tree.get_commands()}
        await app.router.aclose()
        return names

    assert {"redes-proponer", "redes-estado"} <= run(go())
