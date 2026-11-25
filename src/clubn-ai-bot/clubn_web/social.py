"""Find a creator's social profiles and read what's public about them.

Discovery order:
  1. Links found in the announcements channel (live notifications) for that member.
  2. From the first profile found, follow links on that page (bio links, YouTube "links",
     Twitch panels, Linktree...) to discover the other networks.
  3. Mods can always add / fix links by hand with /redes-creador.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from . import config

log = logging.getLogger(__name__)

PLATFORMS = ("twitch", "youtube", "instagram", "tiktok")

# Handle-level patterns; anything else on those hosts (videos, clips, /p/ posts) is ignored.
_PATTERNS = {
    "twitch": re.compile(r"(?:https?://)?(?:www\.|m\.)?twitch\.tv/(?!videos|directory|p/|settings)([A-Za-z0-9_]{3,25})", re.I),
    "youtube": re.compile(r"(?:https?://)?(?:www\.|m\.)?youtube\.com/(@[\w.\-]{2,}|channel/UC[\w\-]{20,}|c/[\w.\-]+|user/[\w.\-]+)", re.I),
    "instagram": re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/(?!p/|reel/|explore/|stories/)([A-Za-z0-9_.]{1,30})", re.I),
    "tiktok": re.compile(r"(?:https?://)?(?:www\.|m\.)?tiktok\.com/(@[A-Za-z0-9_.]{2,24})", re.I),
}
_LINK_HUBS = re.compile(r"https?://(?:www\.)?(?:linktr\.ee|beacons\.ai|lnk\.bio|campsite\.bio|bio\.link|carrd\.co|[\w\-]+\.carrd\.co)/?[\w\-./]*", re.I)
_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.6",
}


def canonical(platform: str, handle: str) -> str:
    handle = handle.rstrip("/")
    return {
        "twitch": f"https://www.twitch.tv/{handle.lower()}",
        "youtube": f"https://www.youtube.com/{handle}",
        "instagram": f"https://www.instagram.com/{handle.lower()}/",
        "tiktok": f"https://www.tiktok.com/{handle.lower()}",
    }[platform]


def extract_links(text: str) -> dict[str, str]:
    """Return {platform: canonical_url} for every social profile mentioned in text."""
    found: dict[str, str] = {}
    for p, rx in _PATTERNS.items():
        m = rx.search(text or "")
        if m:
            found[p] = canonical(p, m.group(1))
    return found


def normalize_user_input(platform: str, value: str) -> str | None:
    """Accept a full URL or a bare handle from a slash command."""
    value = (value or "").strip()
    if not value:
        return None
    links = extract_links(value)
    if platform in links:
        return links[platform]
    handle = value.lstrip("@")
    if not re.fullmatch(r"[\w.\-]{2,40}", handle):
        return None
    if platform in ("youtube", "tiktok"):
        handle = "@" + handle
    return canonical(platform, handle)


def message_text(msg) -> str:
    """Flatten a discord.Message (content + embeds + buttons) into searchable text."""
    parts = [msg.content or ""]
    for e in msg.embeds:
        d = e.to_dict()
        parts.append(json.dumps(d, ensure_ascii=False))
    for row in getattr(msg, "components", []) or []:
        for c in getattr(row, "children", []) or []:
            if getattr(c, "url", None):
                parts.append(c.url)
    return "\n".join(parts)


def _norm(x: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())


def member_names(member) -> set[str]:
    names = {member.name, getattr(member, "global_name", None) or "", member.display_name}
    return {n for n in map(_norm, names) if len(n) >= 4}


def handle_matches(url: str, names: set[str]) -> bool:
    """True when the profile handle looks like this member's name (pepe_gamer ~ PepeGamer87)."""
    h = _norm(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1].lstrip("@"))
    return len(h) >= 4 and any(n in h or h in n for n in names)


def all_links(text: str) -> list[tuple[str, str]]:
    """Every (platform, canonical_url) in the text, in order, de-duplicated."""
    out: list[tuple[str, str]] = []
    for p, rx in _PATTERNS.items():
        for m in rx.finditer(text or ""):
            item = (p, canonical(p, m.group(1)))
            if item not in out:
                out.append(item)
    return out


def links_for_member(msg, member) -> dict[str, str]:
    """Links in an announcement that belong to this member.

    Announcements are often posted by a bot or by another person and can contain several
    channels, so a link is only accepted when its handle resembles the member's Discord
    name. The one exception: a message the member wrote themselves with a single link
    per platform."""
    names = member_names(member)
    cands = all_links(message_text(msg))
    found: dict[str, str] = {}
    for p, url in cands:
        if p not in found and handle_matches(url, names):
            found[p] = url
    if not found and msg.author.id == member.id:
        per_platform: dict[str, list[str]] = {}
        for p, url in cands:
            per_platform.setdefault(p, []).append(url)
        found = {p: urls[0] for p, urls in per_platform.items() if len(urls) == 1}
    return found


# --------------------------------------------------------------------------- fetching

async def _get(client: httpx.AsyncClient, url: str) -> str:
    try:
        r = await client.get(url, follow_redirects=True, timeout=20)
        if r.status_code == 200:
            return r.text
        log.info("GET %s -> %s", url, r.status_code)
    except httpx.HTTPError as e:
        log.info("GET %s failed: %s", url, e)
    return ""


def _meta(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    def m(*names):
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return ""
    return {
        "title": m("og:title", "twitter:title") or (soup.title.string.strip() if soup.title and soup.title.string else ""),
        "description": m("og:description", "description", "twitter:description"),
        "image": m("og:image", "twitter:image"),
    }


async def _twitch_api(client: httpx.AsyncClient, login: str) -> dict:
    if not (config.TWITCH_CLIENT_ID and config.TWITCH_CLIENT_SECRET):
        return {}
    tok = await client.post("https://id.twitch.tv/oauth2/token", data={
        "client_id": config.TWITCH_CLIENT_ID, "client_secret": config.TWITCH_CLIENT_SECRET,
        "grant_type": "client_credentials"})
    if tok.status_code != 200:
        return {}
    h = {"Client-Id": config.TWITCH_CLIENT_ID, "Authorization": f"Bearer {tok.json()['access_token']}"}
    u = (await client.get("https://api.twitch.tv/helix/users", params={"login": login}, headers=h)).json().get("data", [])
    if not u:
        return {}
    u = u[0]
    ch = (await client.get("https://api.twitch.tv/helix/channels", params={"broadcaster_id": u["id"]}, headers=h)).json().get("data", [{}])[0]
    fol = (await client.get("https://api.twitch.tv/helix/channels/followers", params={"broadcaster_id": u["id"]}, headers=h)).json()
    vids = (await client.get("https://api.twitch.tv/helix/videos", params={"user_id": u["id"], "first": 5}, headers=h)).json().get("data", [])
    # Their best moments: most-viewed clips (Helix returns them by views), else most-viewed VODs.
    best = []
    clips = (await client.get("https://api.twitch.tv/helix/clips", params={"broadcaster_id": u["id"], "first": 8},
                              headers=h)).json().get("data", [])
    for c in sorted(clips, key=lambda c: c.get("view_count", 0), reverse=True):
        if c.get("thumbnail_url"):
            best.append({"title": c.get("title"), "url": c.get("url"), "views": c.get("view_count"),
                         "thumbnail": c["thumbnail_url"], "kind": "clip de Twitch"})
    if not best:
        top = (await client.get("https://api.twitch.tv/helix/videos", params={
            "user_id": u["id"], "first": 8, "sort": "views"}, headers=h)).json().get("data", [])
        for v in top:
            thumb = (v.get("thumbnail_url") or "").replace("%{width}", "640").replace("%{height}", "360")
            if thumb and "_404/" not in thumb:  # VODs still processing have a placeholder
                best.append({"title": v.get("title"), "url": v.get("url"), "views": v.get("view_count"),
                             "thumbnail": thumb, "kind": "vídeo de Twitch"})
    return {
        "title": u.get("display_name"), "description": u.get("description"), "image": u.get("profile_image_url"),
        "created_at": u.get("created_at"), "followers": fol.get("total"),
        "game": ch.get("game_name"), "stream_title": ch.get("title"), "tags": ch.get("tags"),
        "recent_videos": [v.get("title") for v in vids],
        "best": best[:4],
    }


async def _youtube_api(client: httpx.AsyncClient, url: str) -> dict:
    if not config.YOUTUBE_API_KEY:
        return {}
    path = urlparse(url).path.strip("/")
    params = {"part": "snippet,statistics", "key": config.YOUTUBE_API_KEY}
    if path.startswith("@"):
        params["forHandle"] = path
    elif path.startswith("channel/"):
        params["id"] = path.split("/", 1)[1]
    else:
        params["forUsername"] = path.split("/", 1)[-1]
    items = (await client.get("https://www.googleapis.com/youtube/v3/channels", params=params)).json().get("items", [])
    if not items:
        return {}
    it = items[0]
    sn, st = it["snippet"], it.get("statistics", {})
    vids = (await client.get("https://www.googleapis.com/youtube/v3/search", params={
        "part": "snippet", "channelId": it["id"], "order": "date", "maxResults": 5, "type": "video",
        "key": config.YOUTUBE_API_KEY})).json().get("items", [])
    top = (await client.get("https://www.googleapis.com/youtube/v3/search", params={
        "part": "snippet", "channelId": it["id"], "order": "viewCount", "maxResults": 4, "type": "video",
        "key": config.YOUTUBE_API_KEY})).json().get("items", [])
    best = []
    for v in top:
        th = v["snippet"].get("thumbnails", {})
        thumb = (th.get("high") or th.get("medium") or th.get("default") or {}).get("url")
        if thumb and v.get("id", {}).get("videoId"):
            best.append({"title": v["snippet"].get("title"), "url": f"https://www.youtube.com/watch?v={v['id']['videoId']}",
                         "thumbnail": thumb, "kind": "vídeo de YouTube"})
    return {
        "title": sn.get("title"), "description": sn.get("description"),
        "image": sn.get("thumbnails", {}).get("high", {}).get("url"), "created_at": sn.get("publishedAt"),
        "subscribers": st.get("subscriberCount"), "videos": st.get("videoCount"),
        "recent_videos": [v["snippet"]["title"] for v in vids],
        "best": best,
    }


async def read_profiles(links: dict[str, str], discover: bool = True,
                        names: set[str] | None = None) -> tuple[dict, dict[str, str]]:
    """Read every known profile. Returns (profiles, links) where links may have grown
    with profiles discovered on the pages we read."""
    links = dict(links)
    profiles: dict[str, dict] = {}
    async with httpx.AsyncClient(headers=_HEADERS) as client:
        queue = [p for p in PLATFORMS if p in links]
        seen_hubs: set[str] = set()
        while queue:
            p = queue.pop(0)
            url = links[p]
            html = await _get(client, url)
            data = _meta(html) if html else {}
            try:
                if p == "twitch":
                    data |= {k: v for k, v in (await _twitch_api(client, url.rsplit("/", 1)[-1])).items() if v}
                elif p == "youtube":
                    data |= {k: v for k, v in (await _youtube_api(client, url)).items() if v}
            except Exception as e:  # optional enrichment must never break the flow
                log.warning("API enrichment for %s failed: %s", url, e)
            profiles[p] = {"url": url, **data}

            if not discover:
                continue
            # Discover more networks from this page (and from any link hub it points to).
            # Only accept handles that resemble the member's name or one of their known handles,
            # so a "friends"/raid link on the page isn't mistaken for theirs.
            known = set(names or set()) | {_norm(u.rstrip("/").rsplit("/", 1)[-1].lstrip("@")) for u in links.values()}
            known = {k for k in known if len(k) >= 4}
            blobs = [html]
            for hub in set(_LINK_HUBS.findall(html or "")) - seen_hubs:
                seen_hubs.add(hub)
                blobs.append(await _get(client, hub))
            for found_p, found_url in all_links(unquote("\n".join(blobs)).replace("\\/", "/")):
                if found_p not in links and handle_matches(found_url, known):
                    links[found_p] = found_url
                    queue.append(found_p)
    return profiles, links
