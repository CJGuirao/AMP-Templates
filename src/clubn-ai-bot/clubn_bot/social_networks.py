"""Where Club Ñ social posts go: text limits per network and the clients that publish them.

Each network is published one of three ways ("routes"), picked in this order:
  direct  the network's own official API, when its credentials are set in AMP
          (Bluesky, Facebook Page and Instagram through Meta's Graph API, Threads);
  postiz  a Postiz workspace (cloud or self-hosted) that has that network connected;
  manual  nothing connected: the moderators get the text on the card and post it by hand.
X is posted directly with OAuth 1.0a user keys (X bills every post: pay-per-use). TikTok has no direct
client: its API only lets audited apps post publicly, so it goes through Postiz or by hand.

Nothing here decides to publish: social_posts.py calls publish() only after a moderator's ✅.
In test mode (SOCIAL_DRY_RUN) publish() checks the route and returns without calling any API.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import quote

import httpx

log = logging.getLogger("clubn_bot.social")

NETWORKS = ("x", "bluesky", "threads", "facebook", "instagram", "tiktok")
LABEL = {"x": "X", "bluesky": "Bluesky", "threads": "Threads", "facebook": "Facebook",
         "instagram": "Instagram", "tiktok": "TikTok"}
EMOJI = {"x": "𝕏", "bluesky": "🦋", "threads": "🧵", "facebook": "📘", "instagram": "📸", "tiktok": "🎵"}
LIMIT = {"x": 280, "bluesky": 300, "threads": 500, "facebook": 3000, "instagram": 2200, "tiktok": 2200}
NEEDS_IMAGE = {"instagram", "tiktok"}
X_URL_LEN = 23           # X counts every link as 23 characters
IG_LINK_NOTE = "🔗 Enlace en la bio."
POSTIZ_TYPES = {"x": ("x",), "bluesky": ("bluesky",), "threads": ("threads",), "facebook": ("facebook",),
                "instagram": ("instagram", "instagram-standalone"), "tiktok": ("tiktok",)}


def _truthy(v: str | None, default: bool = False) -> bool:
    v = (v or "").strip().lower()
    return default if not v else v in ("1", "true", "yes", "on", "si", "sí")


def _clean(v: str | None) -> str:
    return (v or "").strip()  # the .env fallback already drops comments (clubn_bot/envfile.py)


def _hashtags(v: str) -> str:
    return " ".join("#" + t for t in (x.strip().lstrip("#").replace(" ", "") for x in re.split(r"[,\s]+", v)) if t)


@dataclass
class Settings:
    enabled: bool = False
    dry_run: bool = True
    review_channel_id: int | None = None
    command_role_ids: frozenset[int] = frozenset()  # besides moderators, who may run /anuncia_en_redes
    default_link: str = ""                         # link added to ads made from Discord
    watch_web: bool = False                        # also draft ads for every new web article
    networks: tuple[str, ...] = NETWORKS
    wp_categories: tuple[str, ...] = ("Creadores_de_contenido", "Eventos")
    poll_min: int = 10
    max_age_hours: int = 48
    hashtags: str = "#ClubÑ"  # from SOCIAL_HASHTAGS "ClubÑ" (written without # so .env files keep it)
    style: str = ""
    postiz_url: str = "https://api.postiz.com/public/v1"
    postiz_key: str = ""
    tiktok_method: str = "UPLOAD"
    bluesky_pds: str = "https://bsky.social"
    bluesky_handle: str = ""
    bluesky_password: str = field(default="", repr=False)
    graph_version: str = "v24.0"
    facebook_page_id: str = ""
    instagram_user_id: str = ""
    meta_token: str = field(default="", repr=False)
    threads_user_id: str = "me"
    threads_token: str = field(default="", repr=False)
    x_api_key: str = ""
    x_api_secret: str = field(default="", repr=False)
    x_access_token: str = field(default="", repr=False)
    x_access_secret: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        e = os.environ if env is None else env
        g = lambda k, d="": _clean(e.get(k)) or d  # noqa: E731

        def num(k: str, d: int) -> int:
            try:
                return max(1, int(g(k, str(d))))
            except ValueError:
                return d

        nets = tuple(n for n in (x.strip().lower() for x in g("SOCIAL_NETWORKS", ",".join(NETWORKS)).split(","))
                     if n in NETWORKS) or NETWORKS
        cats = tuple(c.strip() for c in g("SOCIAL_WP_CATEGORIES", "Creadores_de_contenido,Eventos").split(",")
                     if c.strip() and c.strip() != "*")
        review = g("SOCIAL_REVIEW_CHANNEL_ID")
        roles = frozenset(int(x) for x in re.split(r"[,\s]+", g("SOCIAL_COMMAND_ROLE_IDS")) if x.isdigit())
        link = g("SOCIAL_DEFAULT_LINK", "https://clubn.online")  # "-" = no link
        method = g("SOCIAL_TIKTOK_METHOD", "UPLOAD").upper()
        return cls(
            enabled=_truthy(e.get("SOCIAL_ENABLED")),
            dry_run=_truthy(e.get("SOCIAL_DRY_RUN"), default=True),
            review_channel_id=int(review) if review.isdigit() else None,
            command_role_ids=roles, default_link=link if link.startswith("https://") else "",
            watch_web=_truthy(e.get("SOCIAL_WATCH_WEB")),
            networks=nets, wp_categories=cats,
            poll_min=num("SOCIAL_POLL_MIN", 10), max_age_hours=num("SOCIAL_MAX_AGE_HOURS", 48),
            hashtags=_hashtags(g("SOCIAL_HASHTAGS", "ClubÑ")), style=g("SOCIAL_STYLE")[:1500],
            postiz_url=g("POSTIZ_URL", "https://api.postiz.com/public/v1").rstrip("/"),
            postiz_key=g("POSTIZ_API_KEY"),
            tiktok_method=method if method in ("UPLOAD", "DIRECT_POST") else "UPLOAD",
            bluesky_pds=g("BLUESKY_PDS", "https://bsky.social").rstrip("/"),
            bluesky_handle=g("BLUESKY_HANDLE").lstrip("@"), bluesky_password=g("BLUESKY_APP_PASSWORD"),
            graph_version=g("META_GRAPH_VERSION", "v24.0"),
            facebook_page_id=g("FACEBOOK_PAGE_ID"), instagram_user_id=g("INSTAGRAM_USER_ID"),
            meta_token=g("META_PAGE_TOKEN"),
            threads_user_id=g("THREADS_USER_ID", "me"), threads_token=g("THREADS_TOKEN"),
            x_api_key=g("X_API_KEY"), x_api_secret=g("X_API_SECRET"),
            x_access_token=g("X_ACCESS_TOKEN"), x_access_secret=g("X_ACCESS_SECRET"),
        )

    def direct(self, net: str) -> bool:
        return {
            "bluesky": bool(self.bluesky_handle and self.bluesky_password),
            "facebook": bool(self.facebook_page_id and self.meta_token),
            "instagram": bool(self.instagram_user_id and self.meta_token),
            "threads": bool(self.threads_token),
            "x": bool(self.x_api_key and self.x_api_secret and self.x_access_token and self.x_access_secret),
        }.get(net, False)


# ----------------------------------------------------------------------------- text

def x_length(text: str) -> int:
    """Length as X counts it: links are 23, characters beyond U+10FF (emoji, CJK) count double."""
    n = 0
    for part in re.split(r"(https?://\S+)", text):
        if re.fullmatch(r"https?://\S+", part or ""):
            n += X_URL_LEN
        else:
            n += sum(1 if ord(c) <= 0x10FF else 2 for c in part)
    return n


def length(net: str, text: str) -> int:
    return x_length(text) if net == "x" else len(text)


def suffix(net: str, url: str | None) -> str:
    """What the code adds after the moderators' text. The writer never writes links itself."""
    if not url:
        return ""
    if net in ("x", "threads"):
        return "\n\n" + url
    if net == "instagram":
        return "\n\n" + IG_LINK_NOTE
    return ""  # Bluesky gets a link card, Facebook a link preview, TikTok has no links


def body_limit(net: str, url: str | None) -> int:
    """Characters left for the text once the suffix is added."""
    return LIMIT[net] - length(net, suffix(net, url))


def fit(net: str, text: str, url: str | None) -> str:
    """Trim the text (at a word boundary, with …) so text + suffix fits the network's limit."""
    text = re.sub(r"https?://\S+", "", text or "").strip()  # links come only from the code
    text = re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", text))
    room = body_limit(net, url)
    if length(net, text) <= room:
        return text
    while text and length(net, text + "…") > room:
        cut = text.rstrip().rfind(" ")
        text = text[:cut] if cut > room // 2 else text[:-1]
    return text.rstrip(" ,;:.-") + "…"


def final_text(net: str, text: str, url: str | None) -> str:
    return text + suffix(net, url)


def hashtag_facets(text: str) -> list[dict]:
    """Bluesky only links hashtags that come with a facet (byte offsets in UTF-8)."""
    facets, raw = [], text.encode("utf-8")
    for m in re.finditer(r"(?<![\w#])#([^\s#.,;:!?¡¿()\"']{1,64})", text):
        start = len(text[: m.start()].encode("utf-8"))
        end = start + len(m.group(0).encode("utf-8"))
        if raw[start:end].decode("utf-8") == m.group(0):
            facets.append({"index": {"byteStart": start, "byteEnd": end},
                           "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": m.group(1)}]})
    return facets


# ----------------------------------------------------------------------------- publishing

@dataclass
class Post:
    title: str
    summary: str
    url: str | None
    image_url: str | None


@dataclass
class Result:
    ok: bool
    route: str
    detail: str = ""
    url: str | None = None
    remote_id: str | None = None

    def as_dict(self) -> dict:
        return {"ok": self.ok, "route": self.route, "detail": self.detail[:300], "url": self.url,
                "id": self.remote_id}


class PublishError(RuntimeError):
    pass


def _err(r: httpx.Response, what: str) -> PublishError:
    try:
        data = r.json()
        msg = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else None
        msg = msg or data.get("message") or data.get("error") or r.text
    except ValueError:
        msg = r.text
    return PublishError(f"{what}: HTTP {r.status_code}: {str(msg)[:200]}")


class Router:
    """Chooses a route per network and publishes. One instance for the whole bot."""

    def __init__(self, s: Settings, http: httpx.AsyncClient | None = None,
                 token_store: tuple[Callable[[str], str | None], Callable[[str, str], None]] | None = None,
                 sleep: Callable[[float], object] = asyncio.sleep):
        self.s = s
        self.http = http or httpx.AsyncClient(timeout=60, headers={"User-Agent": "ClubN-SocialBot/1.0"})
        self._get, self._set = token_store or (lambda _k: None, lambda _k, _v: None)
        self.sleep = sleep
        self._postiz: dict[str, str] | None = None  # network -> integration id
        self._postiz_error = ""

    async def aclose(self) -> None:
        await self.http.aclose()

    # ----- routes -----
    async def postiz_integrations(self, refresh: bool = False) -> dict[str, str]:
        if not self.s.postiz_key:
            return {}
        if self._postiz is not None and not refresh:
            return self._postiz
        try:
            r = await self.http.get(f"{self.s.postiz_url}/integrations",
                                    headers={"Authorization": self.s.postiz_key})
            if r.status_code != 200:
                raise _err(r, "Postiz integrations")
            found: dict[str, str] = {}
            for i in r.json():
                if i.get("disabled"):
                    continue
                for net, types in POSTIZ_TYPES.items():
                    if i.get("identifier") in types:
                        found.setdefault(net, i["id"])
            self._postiz, self._postiz_error = found, ""
        except (httpx.HTTPError, PublishError, ValueError, KeyError, TypeError) as e:
            self._postiz_error = str(e)[:200]
            log.warning("Postiz: could not list the connected channels (%s)", type(e).__name__)
            return self._postiz or {}
        return found

    async def route(self, net: str) -> str:
        if self.s.direct(net):
            return "direct"
        if net in await self.postiz_integrations():
            return "postiz"
        return "manual"

    async def routes(self) -> dict[str, str]:
        return {n: await self.route(n) for n in self.s.networks}

    # ----- publish -----
    async def publish(self, net: str, text: str, post: Post) -> Result:
        route = await self.route(net)
        if route == "manual":
            return Result(False, "manual", "sin conexión: publicad este texto a mano")
        if net in NEEDS_IMAGE and not post.image_url:
            return Result(False, route, f"{LABEL[net]} necesita una imagen y esta publicación no tiene")
        full = final_text(net, text, post.url)
        if length(net, full) > LIMIT[net]:
            return Result(False, route, f"el texto supera el límite de {LIMIT[net]} caracteres")
        if self.s.dry_run:
            return Result(True, route, "modo prueba: no se ha publicado")
        try:
            if route == "postiz":
                return await self._postiz_publish(net, full, post)
            return await getattr(self, f"_{net}")(full, post)
        except (PublishError, httpx.HTTPError, ValueError, KeyError) as e:
            log.warning("%s via %s failed: %s", net, route, type(e).__name__)
            return Result(False, route, str(e)[:300] or type(e).__name__)

    # ----- Postiz -----
    async def _postiz_publish(self, net: str, full: str, post: Post) -> Result:
        ids = await self.postiz_integrations()
        hdr = {"Authorization": self.s.postiz_key}
        images = []
        if post.image_url:
            r = await self.http.post(f"{self.s.postiz_url}/upload-from-url", headers=hdr,
                                     json={"url": post.image_url})
            if r.status_code >= 400:
                raise _err(r, "Postiz upload")
            m = r.json()
            images = [{"id": m["id"], "path": m["path"], "alt": post.title[:300]}]
        settings: dict = {"__type": POSTIZ_TYPES[net][0]}
        if net == "x":
            settings["who_can_reply_post"] = "everyone"
        elif net == "instagram":
            settings.update(post_type="post", is_trial_reel=False, collaborators=[])
        elif net == "facebook" and post.url:
            settings["url"] = post.url
        elif net == "tiktok":
            settings.update(privacy_level="PUBLIC_TO_EVERYONE", duet=False, stitch=False, comment=True,
                            autoAddMusic="no", brand_content_toggle=False, brand_organic_toggle=False,
                            content_posting_method=self.s.tiktok_method, title=post.title[:90])
        body = {
            "type": "now", "date": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "shortLink": False, "tags": [],
            "posts": [{"integration": {"id": ids[net]}, "value": [{"content": full, "image": images}],
                       "settings": settings}],
        }
        r = await self.http.post(f"{self.s.postiz_url}/posts", headers=hdr, json=body)
        if r.status_code >= 400:
            raise _err(r, "Postiz")
        out = r.json()
        pid = (out[0] or {}).get("postId") if isinstance(out, list) and out else None
        note = "enviado a Postiz"
        if net == "tiktok" and self.s.tiktok_method == "UPLOAD":
            note += "; queda en la bandeja de TikTok: hay que terminarlo en la app en 24 h"
        return Result(True, "postiz", note, remote_id=pid)

    # ----- Bluesky (AT Protocol, app password) -----
    async def _bluesky(self, full: str, post: Post) -> Result:
        pds = self.s.bluesky_pds
        r = await self.http.post(f"{pds}/xrpc/com.atproto.server.createSession",
                                 json={"identifier": self.s.bluesky_handle, "password": self.s.bluesky_password})
        if r.status_code != 200:
            raise _err(r, "Bluesky login")
        sess = r.json()
        auth = {"Authorization": f"Bearer {sess['accessJwt']}"}
        record: dict = {"$type": "app.bsky.feed.post", "text": full, "langs": ["es"],
                        "createdAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
        facets = hashtag_facets(full)
        if facets:
            record["facets"] = facets
        if post.url:
            ext = {"uri": post.url, "title": post.title[:300], "description": post.summary[:300]}
            thumb = await self._small_image(post.image_url, 950_000)
            if thumb:
                b = await self.http.post(f"{pds}/xrpc/com.atproto.repo.uploadBlob", content=thumb[0],
                                         headers=auth | {"Content-Type": thumb[1]})
                if b.status_code == 200:
                    ext["thumb"] = b.json()["blob"]
            record["embed"] = {"$type": "app.bsky.embed.external", "external": ext}
        elif post.image_url:  # no link: the picture itself
            img = await self._small_image(post.image_url, 950_000)
            if img:
                b = await self.http.post(f"{pds}/xrpc/com.atproto.repo.uploadBlob", content=img[0],
                                         headers=auth | {"Content-Type": img[1]})
                if b.status_code != 200:
                    raise _err(b, "Bluesky (imagen)")
                record["embed"] = {"$type": "app.bsky.embed.images",
                                   "images": [{"image": b.json()["blob"], "alt": post.title[:300]}]}
        r = await self.http.post(f"{pds}/xrpc/com.atproto.repo.createRecord", headers=auth,
                                 json={"repo": sess["did"], "collection": "app.bsky.feed.post", "record": record})
        if r.status_code != 200:
            raise _err(r, "Bluesky")
        uri = r.json()["uri"]
        return Result(True, "direct", "publicado", remote_id=uri,
                      url=f"https://bsky.app/profile/{sess.get('handle') or self.s.bluesky_handle}/post/{uri.rsplit('/', 1)[-1]}")

    async def _small_image(self, url: str | None, cap: int) -> tuple[bytes, str] | None:
        if not url or not url.startswith("https://"):
            return None
        try:
            r = await self.http.get(url, follow_redirects=True)
        except httpx.HTTPError:
            return None
        ctype = r.headers.get("content-type", "").split(";")[0]
        if r.status_code != 200 or not ctype.startswith("image/") or len(r.content) > cap:
            return None
        return r.content, ctype

    # ----- Meta Graph API: Facebook Page and Instagram professional account -----
    def _graph(self, path: str) -> str:
        return f"https://graph.facebook.com/{self.s.graph_version}/{path}"

    async def _facebook(self, full: str, post: Post) -> Result:
        tok, page = self.s.meta_token, self.s.facebook_page_id
        if post.url or not post.image_url:  # a link preview
            data = {"message": full, "access_token": tok}
            if post.url:
                data["link"] = post.url
            r = await self.http.post(self._graph(f"{page}/feed"), data=data)
        else:  # no link: a photo post with the text as caption
            r = await self.http.post(self._graph(f"{page}/photos"),
                                     data={"url": post.image_url, "caption": full, "access_token": tok})
        if r.status_code != 200:
            raise _err(r, "Facebook")
        out = r.json()
        pid = out.get("post_id") or out["id"]
        return Result(True, "direct", "publicado", url=f"https://www.facebook.com/{pid}", remote_id=pid)

    async def _instagram(self, full: str, post: Post) -> Result:
        uid, tok = self.s.instagram_user_id, self.s.meta_token
        r = await self.http.post(self._graph(f"{uid}/media"),
                                 data={"image_url": post.image_url, "caption": full, "access_token": tok})
        if r.status_code != 200:
            raise _err(r, "Instagram (subida)")
        cid = r.json()["id"]
        await self._wait_container(self._graph(cid), tok, "Instagram")
        r = await self.http.post(self._graph(f"{uid}/media_publish"), data={"creation_id": cid, "access_token": tok})
        if r.status_code != 200:
            raise _err(r, "Instagram")
        mid = r.json()["id"]
        link = None
        p = await self.http.get(self._graph(mid), params={"fields": "permalink", "access_token": tok})
        if p.status_code == 200:
            link = p.json().get("permalink")
        return Result(True, "direct", "publicado", url=link, remote_id=mid)

    async def _wait_container(self, url: str, token: str, what: str, tries: int = 10) -> None:
        for _ in range(tries):
            r = await self.http.get(url, params={"fields": "status_code", "access_token": token})
            if r.status_code == 200:
                status = r.json().get("status_code")
                if status in ("FINISHED", "PUBLISHED"):
                    return
                if status in ("ERROR", "EXPIRED"):
                    raise PublishError(f"{what}: el servidor rechazó la imagen ({status})")
            await self.sleep(3)
        raise PublishError(f"{what}: la imagen tarda demasiado en procesarse")

    # ----- X API v2 (OAuth 1.0a user context: the account that owns the access token) -----
    def x_auth(self, method: str, url: str, nonce: str | None = None, timestamp: str | None = None) -> str:
        """OAuth 1.0a header. A JSON body is not part of the signature (only form/query parameters are)."""
        s = self.s
        oauth = {"oauth_consumer_key": s.x_api_key, "oauth_nonce": nonce or secrets.token_hex(16),
                 "oauth_signature_method": "HMAC-SHA1", "oauth_timestamp": timestamp or str(int(time.time())),
                 "oauth_token": s.x_access_token, "oauth_version": "1.0"}
        enc = lambda v: quote(str(v), safe="~-._")  # noqa: E731
        params = "&".join(f"{enc(k)}={enc(v)}" for k, v in sorted(oauth.items()))
        base = "&".join((method.upper(), enc(url), enc(params)))
        key = f"{enc(s.x_api_secret)}&{enc(s.x_access_secret)}"
        oauth["oauth_signature"] = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
        return "OAuth " + ", ".join(f'{enc(k)}="{enc(v)}"' for k, v in sorted(oauth.items()))

    async def _x(self, full: str, post: Post) -> Result:
        url = "https://api.x.com/2/tweets"
        r = await self.http.post(url, json={"text": full}, headers={"Authorization": self.x_auth("POST", url)})
        if r.status_code not in (200, 201):
            raise _err(r, "X")
        tid = r.json()["data"]["id"]
        return Result(True, "direct", "publicado", url=f"https://x.com/i/web/status/{tid}", remote_id=tid)

    # ----- Threads API (long-lived token, refreshed by the bot) -----
    def threads_token(self) -> str:
        """The AMP token, or the refreshed copy the bot stored if it was made from that same token."""
        src = hashlib.sha256(self.s.threads_token.encode()).hexdigest()[:16]
        if self._get("threads_src") == src:
            return self._get("threads_token") or self.s.threads_token
        return self.s.threads_token

    async def refresh_threads_token(self) -> bool:
        """Threads tokens last 60 days; refreshing gives another 60. Run about weekly."""
        if not self.s.threads_token or self.s.dry_run:
            return False
        r = await self.http.get("https://graph.threads.net/refresh_access_token",
                                params={"grant_type": "th_refresh_token", "access_token": self.threads_token()})
        if r.status_code != 200:
            log.warning("Threads token refresh failed (HTTP %s); renew THREADS_TOKEN in AMP if posts fail",
                        r.status_code)
            return False
        self._set("threads_src", hashlib.sha256(self.s.threads_token.encode()).hexdigest()[:16])
        self._set("threads_token", r.json()["access_token"])
        return True

    async def _threads(self, full: str, post: Post) -> Result:
        tok, uid = self.threads_token(), self.s.threads_user_id or "me"
        base = "https://graph.threads.net/v1.0"
        data = {"media_type": "TEXT", "text": full, "access_token": tok}
        if post.url:
            data["link_attachment"] = post.url
        elif post.image_url:
            data |= {"media_type": "IMAGE", "image_url": post.image_url}
        r = await self.http.post(f"{base}/{uid}/threads", data=data)
        if r.status_code != 200:
            raise _err(r, "Threads (borrador)")
        cid = r.json()["id"]
        await self.sleep(2)  # Meta recommends a short wait before publishing a container
        r = await self.http.post(f"{base}/{uid}/threads_publish", data={"creation_id": cid, "access_token": tok})
        if r.status_code != 200:
            raise _err(r, "Threads")
        tid = r.json()["id"]
        link = None
        p = await self.http.get(f"{base}/{tid}", params={"fields": "permalink", "access_token": tok})
        if p.status_code == 200:
            link = p.json().get("permalink")
        return Result(True, "direct", "publicado", url=link, remote_id=tid)
