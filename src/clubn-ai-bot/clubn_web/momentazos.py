"""Momentazos: keeps the /momentazos/ WordPress page in sync with the Club Ñ YouTube channel.

No AI involved. Every MOMENTAZOS_INTERVAL_HOURS the bot:
  1. reads the channel's public RSS feed (newest 15 uploads) and keeps the videos whose title looks
     like a clip compilation (MOMENTAZOS_TITLE_REGEX). It also reads the Momentazos playlist feed and,
     if YOUTUBE_API_KEY is set, the full playlist through the YouTube Data API;
  2. fetches the page through the WP REST API, reads the video list stored as JSON between
     <!-- momentazos:auto:start --> and <!-- momentazos:auto:end -->;
  3. adds the videos it has never seen before (year / quarter taken from the title, or from the
     publish date), re-renders only that block and saves the page.

Safety: it does nothing if the page is not published (unless MOMENTAZOS_ALLOW_DRAFT), if its slug is
not "momentazos…", or if the markers / JSON are missing. Video ids it has processed are stored in
SQLite, so a video a mod removes from the page by hand is not added again.

Manual run (inside the container, or locally with the .env loaded):
    python -m bot.momentazos              # dry run: prints what it would add, writes nothing
    python -m bot.momentazos --apply      # really updates the page
    python -m bot.momentazos --apply --allow-draft   # also works on a draft page (testing)
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import re
import sqlite3
import time
import unicodedata
import xml.etree.ElementTree as ET

import httpx

from . import config, db

log = logging.getLogger(__name__)

START = "<!-- momentazos:auto:start -->"
END = "<!-- momentazos:auto:end -->"
DATA_ID = "cn-mz-data"

MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre"]
_SHORT = {m[:3]: i + 1 for i, m in enumerate(MONTHS)} | {"set": 9}
_MON_ABBR = ["", "ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS momentazos_seen (
    video_id   TEXT PRIMARY KEY,
    title      TEXT,
    action     TEXT,          -- added | present | skipped
    seen_at    REAL
);
"""


# ----------------------------------------------------------------------------- helpers

def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


def looks_like_momentazos(title: str) -> bool:
    return re.search(config.MOMENTAZOS_TITLE_REGEX, _norm(title)) is not None


def _months_in(title: str) -> list[int]:
    out = []
    for w in re.findall(r"[a-z]+", _norm(title)):
        if w in MONTHS:
            out.append(MONTHS.index(w) + 1)
        elif len(w) == 3 and w in _SHORT:
            out.append(_SHORT[w])
    return out


def classify(video_id: str, title: str, published: str) -> dict:
    """Turn a YouTube title + publish date (YYYY-MM-DD) into a page entry. Deterministic, no AI."""
    pub = dt.date.fromisoformat(published[:10])
    entry = {"id": video_id, "title": title.strip(), "published": pub.isoformat(),
             "year": None, "quarter": None, "months": None, "kind": "trimestral", "num": None}
    if m := re.search(r"#\s*(\d{1,3})\b", title):
        entry["num"] = int(m[1])
    if "anuario" in _norm(title):
        entry.update(kind="especial", year=pub.year - 1 if pub.month <= 3 else pub.year)
        return entry
    months = _months_in(title)
    years = [int(y) for y in re.findall(r"\b(20\d\d)\b", title)]
    if months:
        first, last = months[0], months[-1]
        if years:
            year = years[-1]
        else:  # compilations come out after the period ends: no year in the title -> latest year that fits
            year = pub.year if last <= pub.month else pub.year - 1
        span = (last - first) % 12 + 1
        entry.update(year=year, months=[first, last])
        if span == 3 and first in (1, 4, 7, 10):
            entry["quarter"] = (first - 1) // 3 + 1
        else:
            entry["kind"] = "bimestral" if span == 2 else "otro"
        return entry
    # No months in the title: assume it covers the quarter before the one it was published in.
    q = (pub.month - 1) // 3  # 0..3 = previous quarter index (0 means Q4 of last year)
    year, q = (pub.year - 1, 4) if q == 0 else (pub.year, q)
    entry.update(year=year, quarter=q, months=[3 * q - 2, 3 * q], guessed=True)
    return entry


def _period(v: dict) -> str:
    if v.get("months"):
        a, b = v["months"]
        return f"{MONTHS[a - 1]}–{MONTHS[b - 1]}" if a != b else MONTHS[a - 1]
    return ""


def _sort_key(v: dict):
    return ((v.get("months") or [0])[0], v["published"])


def _fd(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{int(d)} {_MON_ABBR[int(m)]} {y}"


# ----------------------------------------------------------------------------- rendering

def _card(v: dict) -> str:
    t = html.escape(v["title"])
    if v["kind"] == "especial":
        tags = ["Especial", "Anuario"]
    elif v.get("quarter"):
        tags = [f"T{v['quarter']}", _period(v).capitalize()]
    else:
        tags = [_period(v).capitalize() or "Recopilatorio", "Bimestral" if v["kind"] == "bimestral" else "Especial"]
    if v.get("anuario") and v["kind"] != "especial":
        tags.append("Anuario")
    if v.get("num"):
        tags.append(f"#{v['num']}")
    tg = "".join(f'<span class="cn-mz-tag{" cn-mz-q" if i == 0 else ""}">{html.escape(x)}</span>'
                 for i, x in enumerate(tags))
    vid = html.escape(v["id"])
    return (f'<li><figure class="cn-mz"><div class="cn-mz-media"><button type="button" class="cn-mz-play" '
            f'data-yt="{vid}" aria-label="Reproducir: {t}"><img src="https://i.ytimg.com/vi/{vid}/hqdefault.jpg" '
            f'alt="" loading="lazy" width="480" height="360"></button></div><figcaption>'
            f'<p class="cn-mz-tags">{tg}</p><h3>{t}</h3><p class="cn-mz-meta">Publicado el {_fd(v["published"])} · '
            f'<a href="https://www.youtube.com/watch?v={vid}" target="_blank" rel="noopener">Ver en YouTube</a>'
            f'</p></figcaption></figure></li>')


def render_block(videos: list[dict]) -> str:
    """The auto-generated part of the page: year index, one section per year, specials, and the JSON data."""
    regular = [v for v in videos if v["kind"] != "especial"]
    specials = sorted([v for v in videos if v["kind"] == "especial"], key=lambda v: v["published"], reverse=True)
    years = sorted({v["year"] for v in regular}, reverse=True)
    nav, secs = [], []
    for y in years:
        vs = sorted([v for v in regular if v["year"] == y], key=_sort_key, reverse=True)
        fmt = "trimestral" if all(v.get("quarter") for v in vs) else \
              "bimestral" if not any(v.get("quarter") for v in vs) else "mixto"
        nav.append(f'<li><a href="#momentazos-{y}">{y} <span>{len(vs)}</span></a></li>')
        secs.append(f'<section class="cn-mz-year" id="momentazos-{y}"><h2>Momentazos {y} <small>{len(vs)} '
                    f'{"vídeo" if len(vs) == 1 else "vídeos"} · formato {fmt}</small></h2>'
                    f'<ul class="cn-mz-grid">{"".join(_card(v) for v in vs)}</ul></section>')
    if specials:
        nav.append(f'<li><a href="#especiales">Especiales <span>{len(specials)}</span></a></li>')
        secs.append('<section class="cn-mz-year" id="especiales"><h2>Especiales</h2><p>Además de los '
                    'recopilatorios de cada periodo, el Club publica de vez en cuando vídeos especiales, como el '
                    'anuario: un vídeo más largo que reúne los mejores momentazos de los creadores del Club.</p>'
                    f'<ul class="cn-mz-grid">{"".join(_card(v) for v in specials)}</ul></section>')
    nav.append('<li><a href="#participar">Cómo participar</a></li>')
    videos = sorted(videos, key=lambda v: (v["published"], v["id"]), reverse=True)
    data = json.dumps({"version": 1, "videos": videos}, ensure_ascii=False, separators=(",", ":"))
    data = data.replace("</", "<\\/")
    return (f'{START}<nav aria-label="Momentazos por año"><ul class="cn-mz-nav">{"".join(nav)}</ul></nav>'
            f'{"".join(secs)}<script type="application/json" id="{DATA_ID}">{data}</script>{END}')


def read_block(content: str) -> list[dict] | None:
    """Videos stored in the page, or None if the markers / data are missing."""
    a, b = content.find(START), content.find(END)
    if a < 0 or b < a:
        return None
    m = re.search(rf'<script type="application/json" id="{DATA_ID}">(.*?)</script>', content[a:b], re.S)
    if not m:
        return None
    try:
        return json.loads(m[1].replace("<\\/", "</"))["videos"]
    except (ValueError, KeyError):
        return None


def replace_block(content: str, videos: list[dict]) -> str:
    a, b = content.find(START), content.find(END)
    return content[:a] + render_block(videos) + content[b + len(END):]


# ----------------------------------------------------------------------------- sources

_NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


async def _feed(client: httpx.AsyncClient, query: str) -> list[dict]:
    r = await client.get(f"https://www.youtube.com/feeds/videos.xml?{query}")
    r.raise_for_status()
    root = ET.fromstring(r.content)
    return [{"id": e.findtext("yt:videoId", namespaces=_NS), "title": e.findtext("a:title", namespaces=_NS) or "",
             "published": (e.findtext("a:published", namespaces=_NS) or "")[:10]}
            for e in root.findall("a:entry", _NS)]


async def _api_playlist(client: httpx.AsyncClient, playlist: str) -> list[dict]:
    out, token = [], None
    while True:
        p = {"part": "snippet,contentDetails", "playlistId": playlist, "maxResults": 50, "key": config.YOUTUBE_API_KEY}
        if token:
            p["pageToken"] = token
        r = await client.get("https://www.googleapis.com/youtube/v3/playlistItems", params=p)
        r.raise_for_status()
        j = r.json()
        for it in j.get("items", []):
            pub = it["contentDetails"].get("videoPublishedAt")
            if pub:  # private / deleted videos have none
                out.append({"id": it["contentDetails"]["videoId"], "title": it["snippet"]["title"],
                            "published": pub[:10]})
        token = j.get("nextPageToken")
        if not token:
            return out


async def fetch_candidates() -> list[dict]:
    """Momentazos videos found on YouTube, newest first, without duplicates."""
    found: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "ClubN-Bot/1.0"}) as c:
        if config.MOMENTAZOS_CHANNEL_ID:
            try:
                for v in await _feed(c, f"channel_id={config.MOMENTAZOS_CHANNEL_ID}"):
                    if looks_like_momentazos(v["title"]):
                        found.setdefault(v["id"], v)
            except Exception as e:
                log.warning("momentazos: channel feed failed: %s", e)
        if config.MOMENTAZOS_PLAYLIST_ID:
            # The playlist feed only lists the first 15 items in playlist order, so it is a backup source.
            try:
                for v in await _feed(c, f"playlist_id={config.MOMENTAZOS_PLAYLIST_ID}"):
                    found.setdefault(v["id"], v)
            except Exception as e:
                log.warning("momentazos: playlist feed failed: %s", e)
            if config.YOUTUBE_API_KEY:
                try:
                    for v in await _api_playlist(c, config.MOMENTAZOS_PLAYLIST_ID):
                        found.setdefault(v["id"], v)
                except Exception as e:
                    log.warning("momentazos: YouTube API failed: %s", e)
    return sorted(found.values(), key=lambda v: v["published"], reverse=True)


# ----------------------------------------------------------------------------- state

def _conn() -> sqlite3.Connection:
    c = db._conn()
    c.executescript(SCHEMA)
    return c


def seen_ids() -> set[str]:
    with _conn() as c:
        return {r[0] for r in c.execute("SELECT video_id FROM momentazos_seen")}


def mark_seen(video_id: str, title: str, action: str) -> None:
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO momentazos_seen (video_id, title, action, seen_at) VALUES (?,?,?,?)",
                  (video_id, title, action, time.time()))


# ----------------------------------------------------------------------------- the job

async def sync(wp, apply: bool = True, allow_draft: bool | None = None) -> dict:
    """Check YouTube and update the page. Returns a summary dict; never raises for 'nothing to do'."""
    allow_draft = config.MOMENTAZOS_ALLOW_DRAFT if allow_draft is None else allow_draft
    res = {"added": [], "skipped": [], "status": "ok", "page": config.MOMENTAZOS_PAGE_ID}
    if not config.MOMENTAZOS_PAGE_ID:
        res["status"] = "no MOMENTAZOS_PAGE_ID"
        return res
    page = await wp._req("GET", f"pages/{config.MOMENTAZOS_PAGE_ID}", params={"context": "edit"})
    if not str(page.get("slug", "")).startswith("momentazos") and not page.get("generated_slug", "").startswith("momentazos"):
        res["status"] = f"page {page.get('id')} is not the momentazos page (slug {page.get('slug')!r})"
        return res
    if page["status"] != "publish" and not allow_draft:
        res["status"] = f"page is {page['status']}, not published: nothing done"
        return res
    content = page["content"]["raw"]
    videos = read_block(content)
    if videos is None:
        res["status"] = "markers or JSON data missing in the page: nothing done"
        return res
    on_page = {v["id"] for v in videos}
    seen = seen_ids()
    new = []
    for cand in await fetch_candidates():
        if cand["id"] in on_page:
            if apply and cand["id"] not in seen:
                mark_seen(cand["id"], cand["title"], "present")
            continue
        if cand["id"] in seen:
            res["skipped"].append(cand["title"])  # removed from the page on purpose: leave it out
            continue
        new.append(classify(cand["id"], cand["title"], cand["published"]))
    res["added"] = [f"{v['title']} -> {v['year']} {('T' + str(v['quarter'])) if v.get('quarter') else v['kind']}"
                    + (" (trimestre deducido de la fecha)" if v.get("guessed") else "") for v in new]
    if new and apply:
        await wp._req("POST", f"pages/{page['id']}", json={"content": replace_block(content, videos + new)})
        for v in new:
            mark_seen(v["id"], v["title"], "added")
        log.info("momentazos: added %d video(s) to page %s: %s", len(new), page["id"], res["added"])
    elif not new:
        log.info("momentazos: no new videos")
    return res


# ----------------------------------------------------------------------------- discord wiring

def setup(client) -> None:
    """Called from main.py's setup_hook. Starts the polling loop."""
    from discord.ext import tasks

    if not config.MOMENTAZOS_ENABLED:
        return

    @tasks.loop(hours=config.MOMENTAZOS_INTERVAL_HOURS)
    async def momentazos_tick() -> None:
        try:
            res = await sync(client.wp)
        except Exception:
            log.exception("momentazos: sync failed")
            return
        if res["status"] != "ok":
            log.info("momentazos: %s", res["status"])
        if res["added"] and config.MOMENTAZOS_NOTIFY_CHANNEL_ID:
            ch = client.get_channel(config.MOMENTAZOS_NOTIFY_CHANNEL_ID)
            if ch:
                await ch.send("📼 Añadido a la página de Momentazos:\n" + "\n".join(f"• {a}" for a in res["added"]))

    @momentazos_tick.before_loop
    async def _wait() -> None:
        await client.wait_until_ready()

    momentazos_tick.start()


# ----------------------------------------------------------------------------- CLI

if __name__ == "__main__":
    import argparse
    import asyncio

    from .wp import WordPress

    ap = argparse.ArgumentParser(description="Sync the Momentazos page with YouTube (dry run by default).")
    ap.add_argument("--apply", action="store_true", help="really update the page")
    ap.add_argument("--allow-draft", action="store_true", help="also work on a draft page (testing)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init()
    out = asyncio.run(sync(WordPress(), apply=a.apply, allow_draft=a.allow_draft or None))
    print(json.dumps(out, ensure_ascii=False, indent=2))
