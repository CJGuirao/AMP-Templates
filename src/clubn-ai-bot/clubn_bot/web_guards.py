"""Safety nets around two clubn_web flows (installed by merged.build; clubn_web itself stays unmodified).

1. Creators: before a creator with no database row is sent through the article flow (startup sweep,
   daily sweep or the role being granted), WordPress is asked whether their article already exists.
   A published one is recorded as published and nobody is asked again; an existing draft is recorded
   so a later /generar-articulo rewrites it instead of making a second one. This keeps a lost or
   replaced database from re-asking the mods for creators that were already done.
2. Events: a Discord scheduled event created after the bot already follows the same event from a
   message (same day, similar name) is attached to that event instead of starting a second one.
   (The other order, message after scheduled event, is already handled by clubn_web.events.)
"""
from __future__ import annotations

import datetime as dt
import difflib
import html
import logging

log = logging.getLogger("clubn_bot.web_guards")

LIVE_STATUSES = {"publish", "future", "private"}


def same_event(name_a: str, start_a: dt.datetime | None, name_b: str, start_b: dt.datetime | None) -> bool:
    """The test clubn_web.events.handle_candidate uses to spot one event announced twice."""
    same_day = start_a and start_b and abs((start_a - start_b).total_seconds()) < 36 * 3600
    similar = difflib.SequenceMatcher(None, name_a.lower(), name_b.lower()).ratio() > 0.55
    return bool(same_day and similar)


async def find_article(wp, titles: set[str]) -> dict | None:
    """The creator article whose title is one of `titles`, published first, else a draft/pending one."""
    cat = await wp.category_id()
    found = []
    for title in titles:
        name = title.removeprefix("Conozcamos más sobre ").removesuffix(" y su contenido")
        posts = await wp._req("GET", "posts", params={
            "search": name, "categories": cat, "per_page": 20, "context": "edit",
            "status": "publish,future,draft,pending,private"})
        found += [p for p in posts if html.unescape((p.get("title") or {}).get("raw")
                                                    or (p.get("title") or {}).get("rendered") or "").strip() in titles]
    found.sort(key=lambda p: (p.get("status") not in LIVE_STATUSES, p.get("id", 0)))
    return found[0] if found else None


def install(web, events, db) -> None:
    """Wrap web.handle_new_creator and events.on_scheduled_event (looked up by name at call time)."""
    original_new_creator = web.handle_new_creator
    original_scheduled = events.on_scheduled_event

    async def handle_new_creator(member) -> None:
        if db.get(member.id) is None:
            names = {getattr(member, a, None) for a in ("display_name", "global_name", "name")}
            titles = {web.article_title(n) for n in names if n}
            try:
                post = await find_article(web.bot.wp, titles)
            except Exception:
                log.exception("Could not check WordPress for %s's article; trying again on the next sweep", member)
                return
            if post and post.get("status") in LIVE_STATUSES:
                db.upsert(member.id, display_name=member.display_name, status="published", wp_post_id=post["id"])
                log.info("%s already has article %s on WordPress: marked as published", member, post["id"])
                return
            if post:
                db.upsert(member.id, display_name=member.display_name, status="error", wp_post_id=post["id"],
                          last_error="Ya hay un borrador en WordPress; usad /generar-articulo para revisarlo.")
                log.info("%s already has draft %s on WordPress: kept, not drafted again", member, post["id"])
                return
        await original_new_creator(member)

    async def on_scheduled_event(se, deleted: bool = False) -> None:
        cancelled = deleted or se.status in (events.discord.EventStatus.cancelled,
                                             events.discord.EventStatus.completed)
        cfg = events.config
        if (cfg.EVENTS_ENABLED and not cancelled and se.start_time
                and (not cfg.GUILD_ID or se.guild_id == cfg.GUILD_ID)
                and db.event_by("scheduled_id", se.id) is None):
            for ev in events.active_events():
                if ev.get("scheduled_id"):
                    continue
                if same_event(ev["name"], events._parse(ev["start_utc"]), se.name, se.start_time):
                    db.event_save(ev["id"], scheduled_id=se.id)
                    log.info("scheduled event %s attached to event #%s %s", se.name, ev["id"], ev["name"])
                    break  # the original now sees it as a change to that event
        await original_scheduled(se, deleted)

    web.handle_new_creator = handle_new_creator
    events.on_scheduled_event = on_scheduled_event
