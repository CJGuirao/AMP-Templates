"""WordPress REST client (application-password auth).

Nothing here publishes on its own: drafts are created by generate(), and only
publish() (called from the mods' ✅ button) makes a post public.
"""
from __future__ import annotations

import html
import logging

import httpx

from . import config, db

log = logging.getLogger(__name__)


class WordPress:
    def __init__(self) -> None:
        self.base: str | None = None
        self._client = httpx.AsyncClient(
            auth=(config.WP_USER, config.WP_APP_PASSWORD), timeout=30,
            headers={"User-Agent": "ClubN-CreatorBot/1.0"})
        self._category_id: int | None = None
        self._tag_cache: dict[str, int] = {}

    # ------------------------------------------------------------------ plumbing
    async def resolve_base(self) -> str:
        """Use staging (wp.clubn.online); if it's gone, fall back to production (clubn.online)."""
        if self.base:
            return self.base
        for url in (config.WP_URL, config.WP_FALLBACK_URL):
            try:
                r = await self._client.get(f"{url}/wp-json/wp/v2/users/me", params={"context": "edit"})
                if r.status_code == 200:
                    self.base = url
                    log.info("WordPress: using %s as %s", url, r.json().get("name"))
                    return url
                log.warning("WordPress %s answered %s", url, r.status_code)
            except httpx.HTTPError as e:
                log.warning("WordPress %s unreachable: %s", url, e)
        raise RuntimeError("Neither WordPress site is reachable with the configured credentials")

    async def _req(self, method: str, path: str, **kw) -> dict | list:
        base = await self.resolve_base()
        r = await self._client.request(method, f"{base}/wp-json/wp/v2/{path}", **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"WordPress {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    # ------------------------------------------------------------------ taxonomy
    async def category_id(self) -> int:
        if self._category_id:
            return self._category_id
        cats = await self._req("GET", "categories", params={"search": config.WP_CATEGORY_NAME, "per_page": 100})
        for c in cats:
            if html.unescape(c["name"]).lower() == config.WP_CATEGORY_NAME.lower():
                self._category_id = c["id"]
                return c["id"]
        c = await self._req("POST", "categories", json={"name": config.WP_CATEGORY_NAME,
                                                         "slug": "creadores-de-contenido"})
        self._category_id = c["id"]
        return c["id"]

    async def tag_ids(self, names: list[str]) -> list[int]:
        ids = []
        for n in {n.strip().lower() for n in names if n.strip()}:
            if n not in self._tag_cache:
                found = await self._req("GET", "tags", params={"search": n})
                match = next((t for t in found if html.unescape(t["name"]).lower() == n), None)
                if not match:
                    try:
                        match = await self._req("POST", "tags", json={"name": n})
                    except RuntimeError as e:
                        log.warning("tag %s: %s", n, e)
                        continue
                self._tag_cache[n] = match["id"]
            ids.append(self._tag_cache[n])
        return ids

    # ------------------------------------------------------------------ media
    async def upload_image(self, url: str, name: str) -> int | None:
        m = await self.image(url, name)
        return m[0] if m else None

    async def image(self, url: str, name: str) -> tuple[int, str] | None:
        """(media_id, wordpress_url) for an external image, uploading it once and reusing it after.
        Images are copied into WordPress because Twitch/YouTube thumbnail URLs can change or expire."""
        if not url:
            return None
        if hit := db.cached_media(url):
            return hit
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
                img = await c.get(url)
            if img.status_code != 200 or not img.headers.get("content-type", "").startswith("image/"):
                return None
            ext = img.headers["content-type"].split("/")[1].split(";")[0].replace("jpeg", "jpg")
            media = await self._req("POST", "media", content=img.content, headers={
                "Content-Type": img.headers["content-type"],
                "Content-Disposition": f'attachment; filename="creador-{name}.{ext}"'})
            db.cache_media(url, media["id"], media["source_url"])
            return media["id"], media["source_url"]
        except Exception as e:
            log.warning("image upload failed: %s", e)
            return None

    # ------------------------------------------------------------------ posts
    async def save_draft(self, post_id: int | None, title: str, content: str, excerpt: str,
                         tags: list[str], image_url: str | None, slug_hint: str,
                         date_gmt: str | None = None) -> dict:
        data = {
            "title": title, "content": content, "excerpt": excerpt, "status": "draft",
            "categories": [await self.category_id()], "tags": await self.tag_ids(tags),
        }
        if date_gmt:  # the day they joined the Discord server; publish() keeps it
            data["date_gmt"] = date_gmt
        media_id = await self.upload_image(image_url, slug_hint)  # cached: free on regenerations
        if media_id:
            data["featured_media"] = media_id
        if not post_id:
            return await self._req("POST", "posts", json=data)
        return await self._req("POST", f"posts/{post_id}", json=data)

    async def trash(self, post_id: int) -> None:
        await self._req("DELETE", f"posts/{post_id}")  # moves to trash (recoverable)

    async def publish(self, post_id: int) -> dict:
        """Publish, mark featured (sticky + featured tag), and point the front-page block at it."""
        extra_tags = await self.tag_ids([config.WP_FEATURED_TAG]) if config.WP_FEATURED_TAG else []
        current = await self._req("GET", f"posts/{post_id}", params={"context": "edit"})
        post = await self._req("POST", f"posts/{post_id}", json={
            "status": "publish", "sticky": True,
            "tags": sorted(set(current.get("tags", [])) | set(extra_tags))})
        await self.update_front_block(post)
        return post

    async def update_front_block(self, post: dict) -> None:
        """Keep a synced pattern ('Nuevo creador en Club Ñ') pointing at the newest creator.
        It's inserted once on the front page; after that the bot only rewrites its content."""
        title = html.unescape(post["title"]["rendered"])
        excerpt = post.get("excerpt", {}).get("rendered", "")
        img = ""
        if post.get("featured_media"):
            try:
                m = await self._req("GET", f"media/{post['featured_media']}")
                img = (f'<!-- wp:image {{"sizeSlug":"medium"}} --><figure class="wp-block-image size-medium">'
                       f'<a href="{post["link"]}"><img src="{m["source_url"]}" alt="{html.escape(title)}"/></a></figure>'
                       f'<!-- /wp:image -->')
            except RuntimeError:
                pass
        content = (
            '<!-- wp:group {"className":"nuevo-creador-clubn"} --><div class="wp-block-group nuevo-creador-clubn">'
            f'<!-- wp:heading --><h2 class="wp-block-heading">{html.escape(config.WP_FRONT_BLOCK_TITLE)}</h2><!-- /wp:heading -->'
            f'{img}'
            f'<!-- wp:heading {{"level":3}} --><h3 class="wp-block-heading"><a href="{post["link"]}">{html.escape(title)}</a></h3><!-- /wp:heading -->'
            f'<!-- wp:html -->{excerpt}<!-- /wp:html -->'
            f'<!-- wp:paragraph --><p><a href="{post["link"]}">Conócele →</a></p><!-- /wp:paragraph -->'
            '</div><!-- /wp:group -->'
        )
        blocks = await self._req("GET", "blocks", params={"search": config.WP_FRONT_BLOCK_TITLE, "context": "edit"})
        existing = next((b for b in blocks if b["title"]["raw"] == config.WP_FRONT_BLOCK_TITLE), None)
        if existing:
            await self._req("POST", f"blocks/{existing['id']}", json={"content": content})
        else:
            await self._req("POST", "blocks", json={"title": config.WP_FRONT_BLOCK_TITLE,
                                                    "content": content, "status": "publish"})

    # ------------------------------------------------------------------ events (bot/events.py)
    async def _api(self, method: str, path: str, **kw) -> dict | list:
        """Any REST namespace, e.g. 'tribe/events/v1/events'."""
        base = await self.resolve_base()
        r = await self._client.request(method, f"{base}/wp-json/{path}", **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"WordPress {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    async def category_by_name(self, name: str, slug: str) -> int:
        if name in self._tag_cache:  # shared cache, keys can't clash with lower-cased tag names
            return self._tag_cache[name]
        cats = await self._req("GET", "categories", params={"search": name, "per_page": 100})
        match = next((c for c in cats if html.unescape(c["name"]).lower() == name.lower()), None)
        if not match:
            match = await self._req("POST", "categories", json={"name": name, "slug": slug})
        self._tag_cache[name] = match["id"]
        return match["id"]

    async def save_event_post(self, post_id: int | None, title: str, content: str, excerpt: str,
                              tags: list[str], status: str = "draft") -> dict:
        """Create or update the event's article (a normal post in the Eventos category)."""
        data = {"title": title, "content": content, "excerpt": excerpt, "status": status,
                "categories": [await self.category_by_name(config.WP_EVENTS_CATEGORY, "eventos")],
                "tags": await self.tag_ids(tags)}
        return await self._req("POST", f"posts/{post_id}" if post_id else "posts", json=data)

    async def save_calendar_entry(self, tec_id: int | None, fields: dict) -> dict:
        """Create or update an entry of The Events Calendar (start_date/end_date in local time)."""
        path = "tribe/events/v1/events" + (f"/{tec_id}" if tec_id else "")
        return await self._api("POST", path, json=fields)

    async def trash_calendar_entry(self, tec_id: int) -> None:
        await self._api("DELETE", f"tribe/events/v1/events/{tec_id}")

    def edit_link(self, post_id: int) -> str:
        return f"{self.base}/wp-admin/post.php?post={post_id}&action=edit"

    def preview_link(self, post_id: int) -> str:
        return f"{self.base}/?p={post_id}&preview=true"
