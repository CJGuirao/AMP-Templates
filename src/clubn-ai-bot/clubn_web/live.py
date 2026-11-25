"""Ahora en directo: who of the Club Ñ creators is live on Twitch / YouTube right now. No AI.

Creator database
    Lives in a private Discord thread/channel (LIVE_DB_CHANNEL_ID), so it survives moving or rebuilding
    the bot and any other bot can read it. One message per creator, written by this bot:

        🎥 **Nombre** · <@123456789012345678>
        ```json
        {"schema": "clubn-creator/1", "discord_id": "123456789012345678", "name": "Nombre",
         "twitch": "login", "youtube_channel_id": "UC…", "youtube_url": "https://www.youtube.com/@x",
         "active": true, "updated": "2026-11-24"}
        ```

    Readers: take every message of that channel, keep the ```json block whose "schema" is
    "clubn-creator/1". If two messages have the same discord_id, the newest wins. Only messages written by
    SuperAntonia count, so edit the entries with the slash commands (/directo-creador, /directo-quitar,
    /directos-importar), not by hand.

Live check
    Twitch: Helix /streams for every login (100 per call), every LIVE_INTERVAL_MIN minutes.
    YouTube: each channel's public RSS feed (free) gives the newest uploads, and one videos.list call per
    50 ids (1 quota unit) says which one is live, every LIVE_YT_INTERVAL_MIN minutes.

Web
    The page LIVE_PAGE_ID (slug "ahora-en-directo…") keeps the streams as JSON plus a server-rendered list
    between <!-- cnlive:auto:start --> and <!-- cnlive:auto:end -->. The bot rewrites only that block, and only
    when the set of live streams (or a title) changes. The page's own script turns the list into embedded
    players and re-reads the data from the REST API every minute (REST answers are not page-cached), and the
    home page section reads the same data.

Manual run (inside the container):
    python -m bot.live                     # who is live now (reads the Discord database), writes nothing
    python -m bot.live --from-sqlite       # same, but takes the creators from the bot's SQLite links
    python -m bot.live --apply             # also updates the page
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import discord
import httpx
from discord import app_commands

from . import config

log = logging.getLogger(__name__)

SCHEMA_ID = "clubn-creator/1"
START = "<!-- cnlive:auto:start -->"
END = "<!-- cnlive:auto:end -->"
DATA_ID = "cn-live-data"
API = "https://discord.com/api/v10"
_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)
_NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


# ============================================================================ Discord database (REST)

class CreatorDB:
    """Reads and writes the creator records in the Discord channel through the REST API."""

    def __init__(self, channel_id: int | None = None) -> None:
        self.channel_id = channel_id or config.LIVE_DB_CHANNEL_ID
        self._h = {"Authorization": f"Bot {config.DISCORD_TOKEN}", "User-Agent": "ClubN-Bot/1.0"}
        self._me: str | None = None
        self.records: dict[str, dict] = {}      # discord_id -> record
        self.message_of: dict[str, str] = {}    # discord_id -> message id
        self.loaded_at = 0.0
        self.write_lock = asyncio.Lock()        # held around load-modify-save by writers that need it

    async def _call(self, c: httpx.AsyncClient, method: str, path: str, **kw) -> httpx.Response:
        for _ in range(5):
            r = await c.request(method, f"{API}{path}", headers=self._h, **kw)
            if r.status_code == 429:
                await asyncio.sleep(float(r.json().get("retry_after", 1)) + 0.2)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Discord {method} {path} -> {r.status_code}: {r.text[:200]}")
            return r
        raise RuntimeError(f"Discord {method} {path}: rate limited")

    async def me(self, c: httpx.AsyncClient) -> str:
        if not self._me:
            self._me = (await self._call(c, "GET", "/users/@me")).json()["id"]
        return self._me

    async def load(self) -> dict[str, dict]:
        if not self.channel_id:
            raise RuntimeError("LIVE_DB_CHANNEL_ID is not set")
        recs, msgs = {}, {}
        async with httpx.AsyncClient(timeout=20) as c:
            me = await self.me(c)
            before = None
            while True:
                p = {"limit": 100} | ({"before": before} if before else {})
                page = (await self._call(c, "GET", f"/channels/{self.channel_id}/messages", params=p)).json()
                for m in page:  # newest first
                    if m["author"]["id"] != me:
                        continue
                    rec = parse_record(m.get("content", ""))
                    if rec and rec["discord_id"] not in recs:
                        recs[rec["discord_id"]] = rec
                        msgs[rec["discord_id"]] = m["id"]
                if len(page) < 100:
                    break
                before = page[-1]["id"]
        self.records, self.message_of, self.loaded_at = recs, msgs, time.time()
        return recs

    async def save(self, rec: dict) -> dict:
        """Create or edit the creator's message. Returns the stored record."""
        rec = normalize_record(rec)
        body = {"content": render_record(rec), "allowed_mentions": {"parse": []}}
        async with httpx.AsyncClient(timeout=20) as c:
            mid = self.message_of.get(rec["discord_id"])
            if mid:
                try:
                    await self._call(c, "PATCH", f"/channels/{self.channel_id}/messages/{mid}", json=body)
                except RuntimeError as e:
                    if "-> 404" not in str(e):
                        raise
                    mid = None
            if not mid:
                m = (await self._call(c, "POST", f"/channels/{self.channel_id}/messages", json=body)).json()
                self.message_of[rec["discord_id"]] = m["id"]
        self.records[rec["discord_id"]] = rec
        return rec

    async def delete(self, discord_id: str) -> bool:
        mid = self.message_of.pop(str(discord_id), None)
        self.records.pop(str(discord_id), None)
        if not mid:
            return False
        async with httpx.AsyncClient(timeout=20) as c:
            await self._call(c, "DELETE", f"/channels/{self.channel_id}/messages/{mid}")
        return True


_shared: CreatorDB | None = None


def shared_db() -> CreatorDB:
    """One CreatorDB per process, so every module writing the database edits the same messages."""
    global _shared
    if _shared is None:
        _shared = CreatorDB()
    return _shared


def parse_record(content: str) -> dict | None:
    m = _JSON_BLOCK.search(content or "")
    if not m:
        return None
    try:
        rec = json.loads(m[1])
    except ValueError:
        return None
    if rec.get("schema") != SCHEMA_ID or not rec.get("discord_id"):
        return None
    return normalize_record(rec)


def _handle(v, rx: str) -> str | None:
    v = (v or "").strip().lower().lstrip("@") if isinstance(v, str) else ""
    return v if v and re.fullmatch(rx, v) else None


def normalize_record(rec: dict) -> dict:
    """Optional fields added later (instagram, tiktok) are additive: readers that don't know them ignore them."""
    yt = (rec.get("youtube_channel_id") or "").strip() or None
    if yt and not re.fullmatch(r"UC[\w-]{22}", yt):
        yt = None
    yt_url = rec.get("youtube_url") or None
    if yt_url and not re.fullmatch(r"https?://(?:www\.|m\.)?youtube\.com/[\w@./\-]{1,120}", str(yt_url)):
        yt_url = None
    out = {"schema": SCHEMA_ID, "discord_id": str(rec["discord_id"]), "name": (rec.get("name") or "").strip()[:80],
           "twitch": _handle(rec.get("twitch"), r"[a-z0-9_]{3,25}"),
           "youtube_channel_id": yt, "youtube_url": yt_url if yt else None,
           "active": rec.get("active", True) is not False,
           "updated": rec.get("updated") or dt.date.today().isoformat()}
    ig = _handle(rec.get("instagram"), r"(?!.*\.\.)(?!\.)[a-z0-9._]{1,30}(?<!\.)")
    tt = _handle(rec.get("tiktok"), r"[a-z0-9_.]{2,24}(?<!\.)")
    if ig:
        out["instagram"] = ig
    if tt:
        out["tiktok"] = tt
    return out


def render_record(rec: dict) -> str:
    nets = " · ".join(x for x in (
        f"Twitch `{rec['twitch']}`" if rec["twitch"] else "",
        "YouTube" if rec["youtube_channel_id"] else "",
        f"Instagram `{rec['instagram']}`" if rec.get("instagram") else "",
        f"TikTok `{rec['tiktok']}`" if rec.get("tiktok") else "") if x) or "sin redes"
    head = f"🎥 **{rec['name'] or 'Creador'}** · <@{rec['discord_id']}> · {nets}" + ("" if rec["active"] else " · ⏸️ pausado")
    return f"{head}\n```json\n{json.dumps(rec, ensure_ascii=False)}\n```"


# ============================================================================ YouTube / Twitch helpers

async def resolve_youtube(url: str, client: httpx.AsyncClient | None = None) -> str | None:
    """Channel id (UC…) for a channel URL (/channel/UC…, /@handle, /c/x, /user/x)."""
    if not url:
        return None
    if m := re.search(r"/channel/(UC[\w-]{22})", url):
        return m[1]
    own = client is None
    c = client or httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
    try:
        if (m := re.search(r"/(@[\w.\-]+)", url)) and config.YOUTUBE_API_KEY:
            r = await c.get("https://www.googleapis.com/youtube/v3/channels",
                            params={"part": "id", "forHandle": m[1], "key": config.YOUTUBE_API_KEY})
            if r.status_code == 200 and r.json().get("items"):
                return r.json()["items"][0]["id"]
        if (m := re.search(r"/user/([\w.\-]+)", url)) and config.YOUTUBE_API_KEY:
            r = await c.get("https://www.googleapis.com/youtube/v3/channels",
                            params={"part": "id", "forUsername": m[1], "key": config.YOUTUBE_API_KEY})
            if r.status_code == 200 and r.json().get("items"):
                return r.json()["items"][0]["id"]
        r = await c.get(url, headers={"Accept-Language": "es-ES,es;q=0.9"}, cookies={"CONSENT": "YES+1"})
        m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', r.text) \
            or re.search(r'"externalId":"(UC[\w-]{22})"', r.text)
        return m[1] if m else None
    except httpx.HTTPError as e:
        log.info("live: could not resolve %s: %s", url, e)
        return None
    finally:
        if own:
            await c.aclose()


class Checker:
    """Keeps API tokens and avatar caches between ticks."""

    def __init__(self) -> None:
        self._tw_token: str | None = None
        self._tw_exp = 0.0
        self._avatars: dict[str, tuple[str, float]] = {}
        self.youtube: list[dict] = []       # last YouTube result (checked less often)
        self.youtube_at = 0.0

    async def _twitch_headers(self, c: httpx.AsyncClient) -> dict | None:
        if not (config.TWITCH_CLIENT_ID and config.TWITCH_CLIENT_SECRET):
            return None
        if not self._tw_token or time.time() > self._tw_exp - 300:
            r = await c.post("https://id.twitch.tv/oauth2/token", data={
                "client_id": config.TWITCH_CLIENT_ID, "client_secret": config.TWITCH_CLIENT_SECRET,
                "grant_type": "client_credentials"})
            r.raise_for_status()
            self._tw_token, self._tw_exp = r.json()["access_token"], time.time() + r.json().get("expires_in", 3600)
        return {"Client-Id": config.TWITCH_CLIENT_ID, "Authorization": f"Bearer {self._tw_token}"}

    def _cached_avatar(self, key: str) -> str | None:
        v = self._avatars.get(key)
        return v[0] if v and time.time() - v[1] < 86400 else None

    async def twitch(self, c: httpx.AsyncClient, creators: list[dict]) -> list[dict]:
        by_login = {r["twitch"]: r for r in creators if r["twitch"]}
        h = await self._twitch_headers(c) if by_login else None
        if not h:
            return []
        logins, out = list(by_login), []
        for i in range(0, len(logins), 100):
            r = await c.get("https://api.twitch.tv/helix/streams",
                            params=[("type", "live"), ("first", "100")] + [("user_login", x) for x in logins[i:i + 100]],
                            headers=h)
            if r.status_code == 401:
                self._tw_token = None
            r.raise_for_status()
            for s in r.json().get("data", []):
                rec = by_login.get(s["user_login"].lower())
                if not rec:
                    continue
                out.append({"platform": "twitch", "key": f"tw-{s['user_login'].lower()}",
                            "discord_id": rec["discord_id"], "name": rec["name"] or s["user_name"],
                            "login": s["user_login"].lower(), "url": f"https://www.twitch.tv/{s['user_login'].lower()}",
                            "title": s.get("title", "")[:200], "game": s.get("game_name", ""),
                            "started": s.get("started_at", ""),
                            "thumb": s.get("thumbnail_url", "").replace("{width}", "640").replace("{height}", "360")})
        missing = [s["login"] for s in out if not self._cached_avatar("tw:" + s["login"])]
        if missing:
            r = await c.get("https://api.twitch.tv/helix/users", params=[("login", x) for x in missing], headers=h)
            if r.status_code == 200:
                for u in r.json().get("data", []):
                    self._avatars["tw:" + u["login"].lower()] = (u.get("profile_image_url", ""), time.time())
        for s in out:
            s["avatar"] = self._cached_avatar("tw:" + s["login"]) or ""
        return out

    async def youtube_live(self, c: httpx.AsyncClient, creators: list[dict]) -> list[dict]:
        if not config.YOUTUBE_API_KEY:
            return []
        by_channel = {r["youtube_channel_id"]: r for r in creators if r["youtube_channel_id"]}
        ids: list[str] = [s["video"] for s in self.youtube]   # keep checking streams that were live
        for ch in by_channel:
            try:
                r = await c.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={ch}")
                if r.status_code != 200:
                    continue
                root = ET.fromstring(r.content)
                for e in root.findall("a:entry", _NS)[:config.LIVE_YT_FEED_ITEMS]:
                    vid = e.findtext("yt:videoId", namespaces=_NS)
                    if vid and vid not in ids:
                        ids.append(vid)
            except (httpx.HTTPError, ET.ParseError) as e:
                log.info("live: feed %s failed: %s", ch, e)
        out = []
        for i in range(0, len(ids), 50):
            r = await c.get("https://www.googleapis.com/youtube/v3/videos", params={
                "part": "snippet,liveStreamingDetails", "id": ",".join(ids[i:i + 50]), "key": config.YOUTUBE_API_KEY})
            r.raise_for_status()
            for v in r.json().get("items", []):
                sn, ls = v["snippet"], v.get("liveStreamingDetails", {})
                rec = by_channel.get(sn.get("channelId"))
                if not rec or sn.get("liveBroadcastContent") != "live" or ls.get("actualEndTime"):
                    continue
                th = sn.get("thumbnails", {})
                out.append({"platform": "youtube", "key": f"yt-{v['id']}", "discord_id": rec["discord_id"],
                            "name": rec["name"] or sn.get("channelTitle", ""), "video": v["id"],
                            "channel": sn["channelId"], "url": f"https://www.youtube.com/watch?v={v['id']}",
                            "title": sn.get("title", "")[:200], "game": "",
                            "started": ls.get("actualStartTime", ""),
                            "thumb": (th.get("maxres") or th.get("high") or th.get("medium") or {}).get("url", "")})
        missing = list({s["channel"] for s in out if not self._cached_avatar("yt:" + s["channel"])})
        if missing:
            r = await c.get("https://www.googleapis.com/youtube/v3/channels",
                            params={"part": "snippet", "id": ",".join(missing[:50]), "key": config.YOUTUBE_API_KEY})
            if r.status_code == 200:
                for ch in r.json().get("items", []):
                    t = ch["snippet"].get("thumbnails", {})
                    self._avatars["yt:" + ch["id"]] = ((t.get("medium") or t.get("default") or {}).get("url", ""), time.time())
        for s in out:
            s["avatar"] = self._cached_avatar("yt:" + s["channel"]) or ""
        return out

    async def check(self, creators: list[dict], force_youtube: bool = False) -> list[dict]:
        creators = [r for r in creators if r["active"]]
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "ClubN-Bot/1.0"}) as c:
            try:
                tw = await self.twitch(c, creators)
            except Exception as e:  # keep the last YouTube result, report Twitch as unknown
                log.warning("live: Twitch check failed: %s", e)
                raise
            if force_youtube or time.time() - self.youtube_at >= config.LIVE_YT_INTERVAL_MIN * 60 - 5:
                try:
                    self.youtube = await self.youtube_live(c, creators)
                    self.youtube_at = time.time()
                except Exception as e:
                    log.warning("live: YouTube check failed (keeping the last result): %s", e)
        streams = tw + self.youtube
        streams.sort(key=lambda s: s.get("started") or "")
        return streams


# ============================================================================ page block

def _since(iso: str) -> str:
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo(config.TIMEZONE))
        return t.strftime("%H:%M")
    except ValueError:
        return ""


def _card(s: dict) -> str:
    e = html.escape
    plat = "Twitch" if s["platform"] == "twitch" else "YouTube"
    meta = " · ".join(x for x in (e(s.get("game") or ""), plat, f"desde las {_since(s['started'])}" if s.get("started") else "") if x)
    avatar = f'<img class="cn-live-avatar" src="{e(s["avatar"])}" alt="" width="48" height="48" loading="lazy">' if s.get("avatar") else ""
    return (f'<article class="cn-live-card cn-live-{s["platform"]}" id="{e(s["key"])}">'
            f'<div class="cn-live-player"><a href="{e(s["url"])}" target="_blank" rel="noopener">'
            f'<img src="{e(s.get("thumb") or "")}" alt="Directo de {e(s["name"])}" loading="lazy" width="640" height="360"></a>'
            f'<span class="cn-live-badge">En directo</span></div>'
            f'<div class="cn-live-info">{avatar}<div><h3>{e(s["name"])}</h3>'
            f'<p class="cn-live-title">{e(s.get("title") or "")}</p><p class="cn-live-meta">{meta}</p></div></div>'
            f'<a class="cn-btn cn-live-open" href="{e(s["url"])}" target="_blank" rel="noopener">Abrir en {plat}</a></article>')


EMPTY = ('<div class="cn-live-empty"><p><strong>Ahora mismo no hay nadie del Club en directo.</strong></p>'
         '<p>Vuelve en un rato: esta página se actualiza sola en cuanto alguien empieza a emitir.</p></div>')


def render_block(streams: list[dict], creators: int) -> str:
    data = {"version": 1, "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "creators": creators, "streams": streams}
    js = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    body = f'<div class="cn-live-grid">{"".join(_card(s) for s in streams)}</div>' if streams else EMPTY
    return (f'{START}<div class="cn-live-list" data-count="{len(streams)}">{body}</div>'
            f'<script type="application/json" id="{DATA_ID}">{js}</script>{END}')


def signature(streams: list[dict]) -> str:
    return json.dumps(sorted((s["key"], s.get("title"), s.get("game")) for s in streams), ensure_ascii=False)


def read_signature(content: str) -> str | None:
    a, b = content.find(START), content.find(END)
    if a < 0 or b < a:
        return None
    m = re.search(rf'<script type="application/json" id="{DATA_ID}">(.*?)</script>', content[a:b], re.S)
    try:
        return signature(json.loads(m[1].replace("<\\/", "</"))["streams"]) if m else ""
    except (ValueError, KeyError):
        return ""


async def write_page(wp, streams: list[dict], creators: int, force: bool = False) -> str:
    if not config.LIVE_PAGE_ID:
        return "no LIVE_PAGE_ID"
    page = await wp._req("GET", f"pages/{config.LIVE_PAGE_ID}", params={"context": "edit"})
    slug = page.get("slug") or page.get("generated_slug") or ""
    if not slug.startswith("ahora-en-directo"):
        return f"page {page.get('id')} is not the live page (slug {slug!r})"
    content = page["content"]["raw"]
    old = read_signature(content)
    if old is None:
        return "markers missing in the page: nothing done"
    if old == signature(streams) and not force:
        return "unchanged"
    a, b = content.find(START), content.find(END)
    new = content[:a] + render_block(streams, creators) + content[b + len(END):]
    await wp._req("POST", f"pages/{page['id']}", json={"content": new})
    return "updated"


# ============================================================================ seeding

async def seed_from_members(cdb: CreatorDB, members: list, links_for) -> dict:
    """Add a record for every creator-role member that has none yet. links_for(member) -> {platform: url}."""
    res = {"added": [], "no_links": [], "kept": 0}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
        for m in members:
            if str(m.id) in cdb.records:
                res["kept"] += 1
                continue
            links = await links_for(m)
            tw = re.search(r"twitch\.tv/([A-Za-z0-9_]+)", links.get("twitch", ""))
            yt_url = links.get("youtube")
            yt = await resolve_youtube(yt_url, c) if yt_url else None
            if not tw and not yt:
                res["no_links"].append(m.display_name)
                continue
            await cdb.save({"discord_id": m.id, "name": m.display_name, "twitch": tw[1] if tw else None,
                            "youtube_channel_id": yt, "youtube_url": yt_url})
            res["added"].append(m.display_name)
    return res


# ============================================================================ discord wiring

def setup(client, mod_check, find_links, creator_role) -> None:
    """Called from main.py's setup_hook: slash commands + the polling loop."""
    from discord.ext import tasks

    if not config.LIVE_ENABLED:
        return
    cdb = shared_db()
    checker = Checker()
    state = {"streams": [], "last_error": None}

    async def links_for(member) -> dict:
        from . import db
        row = db.get(member.id)
        return dict(row["links"]) if row and row["links"] else await find_links(member)

    @tasks.loop(minutes=config.LIVE_INTERVAL_MIN)
    async def live_tick() -> None:
        try:
            if time.time() - cdb.loaded_at > config.LIVE_DB_RELOAD_MIN * 60:
                await cdb.load()
            streams = await checker.check(list(cdb.records.values()))
            state["streams"], state["last_error"] = streams, None
            res = await write_page(client.wp, streams, len(cdb.records))
            if res not in ("unchanged", "updated"):
                log.info("live: %s", res)
            elif res == "updated":
                log.info("live: page updated, %d live: %s", len(streams), [s["key"] for s in streams])
        except Exception as e:
            state["last_error"] = str(e)
            log.exception("live: tick failed")

    @live_tick.before_loop
    async def _wait() -> None:
        await client.wait_until_ready()

    def _check(interaction: discord.Interaction) -> bool:
        return mod_check(interaction.user)

    @client.tree.command(name="directo-creador", description="Añade o corrige un creador en la base de directos (moderadores)")
    @app_commands.describe(twitch="Usuario o URL de Twitch; - para quitarlo",
                           youtube="URL del canal o @canal de YouTube; - para quitarlo",
                           activo="Si es No, no se comprueba si está en directo")
    @app_commands.check(_check)
    async def directo_creador(interaction: discord.Interaction, miembro: discord.Member,
                              twitch: str | None = None, youtube: str | None = None,
                              activo: bool | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        if not cdb.loaded_at:
            await cdb.load()
        rec = dict(cdb.records.get(str(miembro.id)) or {"discord_id": miembro.id, "name": miembro.display_name})
        bad = []
        if twitch:
            if twitch.strip() == "-":
                rec["twitch"] = None
            elif m := re.fullmatch(r"(?:https?://)?(?:www\.)?(?:twitch\.tv/)?@?([A-Za-z0-9_]{3,25})/?", twitch.strip()):
                rec["twitch"] = m[1]
            else:
                bad.append("twitch")
        if youtube:
            if youtube.strip() == "-":
                rec["youtube_channel_id"] = rec["youtube_url"] = None
            else:
                url = youtube.strip()
                if not url.startswith("http"):
                    url = "https://www.youtube.com/" + (url if url.startswith(("@", "channel/")) else "@" + url)
                ch = await resolve_youtube(url)
                if ch:
                    rec["youtube_channel_id"], rec["youtube_url"] = ch, url
                else:
                    bad.append("youtube")
        if activo is not None:
            rec["active"] = activo
        rec["updated"] = dt.date.today().isoformat()
        async with cdb.write_lock:
            rec = await cdb.save(rec)
        txt = f"Guardado: {render_record(rec).splitlines()[0]}"
        if bad:
            txt += f"\nNo entendí: {', '.join(bad)}"
        await interaction.followup.send(txt, ephemeral=True)

    @client.tree.command(name="directo-quitar", description="Quita a un creador de la base de directos (moderadores)")
    @app_commands.check(_check)
    async def directo_quitar(interaction: discord.Interaction, miembro: discord.Member) -> None:
        await interaction.response.defer(ephemeral=True)
        await cdb.load()
        ok = await cdb.delete(str(miembro.id))
        await interaction.followup.send("Quitado." if ok else "No estaba en la base.", ephemeral=True)

    @client.tree.command(name="directos-importar",
                         description="Añade a la base de directos a los creadores con rol que aún no están (moderadores)")
    @app_commands.check(_check)
    async def directos_importar(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await cdb.load()
        role = creator_role(interaction.guild)
        if not role:
            await interaction.followup.send("No encuentro el rol de creador.", ephemeral=True)
            return
        res = await seed_from_members(cdb, list(role.members), links_for)
        txt = (f"Añadidos {len(res['added'])}: {', '.join(res['added']) or '—'}\n"
               f"Ya estaban: {res['kept']}\nSin Twitch ni YouTube encontrados ({len(res['no_links'])}): "
               f"{', '.join(res['no_links']) or '—'}\nPuedes añadirlos con `/directo-creador`.")
        await interaction.followup.send(txt[:1900], ephemeral=True)

    @client.tree.command(name="directos", description="Quién del Club está en directo ahora")
    async def directos(interaction: discord.Interaction) -> None:
        s = state["streams"]
        if not s:
            txt = "Ahora mismo no hay nadie del Club en directo."
        else:
            txt = "🔴 **En directo ahora:**\n" + "\n".join(
                f"• **{x['name']}** en {'Twitch' if x['platform'] == 'twitch' else 'YouTube'}: {x['title']} <{x['url']}>"
                for x in s)
        await interaction.response.send_message(txt[:1900], ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    live_tick.start()


# ============================================================================ CLI

async def _cli(apply: bool, from_sqlite: bool) -> dict:
    from . import db
    from .wp import WordPress

    if from_sqlite:
        db.init()
        creators = []
        with db._conn() as c:
            rows = c.execute("SELECT discord_id, display_name, links FROM creators").fetchall()
        for r in rows:
            links = json.loads(r["links"] or "{}")
            tw = re.search(r"twitch\.tv/([A-Za-z0-9_]+)", links.get("twitch", ""))
            yt = await resolve_youtube(links["youtube"]) if links.get("youtube") else None
            if tw or yt:
                creators.append(normalize_record({"discord_id": r["discord_id"], "name": r["display_name"],
                                                  "twitch": tw[1] if tw else None, "youtube_channel_id": yt}))
    else:
        creators = list((await CreatorDB().load()).values())
    streams = await Checker().check(creators, force_youtube=True)
    out = {"creators": len(creators), "live": [(s["name"], s["platform"], s["title"]) for s in streams]}
    if apply:
        out["page"] = await write_page(WordPress(), streams, len(creators))
    return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Who of the Club Ñ creators is live (dry run by default).")
    ap.add_argument("--apply", action="store_true", help="also update the page")
    ap.add_argument("--from-sqlite", action="store_true", help="take creators from the bot's SQLite links")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    print(json.dumps(asyncio.run(_cli(a.apply, a.from_sqlite)), ensure_ascii=False, indent=2))
