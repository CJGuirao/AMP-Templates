"""Club Ñ creator-article bot.

Flow: role "creador de contenido" granted -> find socials -> Claude drafts article ->
WordPress DRAFT -> review message in the mod channel with buttons ->
  ✅ Aprobar        publish + sticky/featured + front-page block
  ✏️ Pedir cambios  modal (or just reply to the message) -> regenerate -> new review message
  🗑️ Descartar      draft moved to trash
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import io
import logging
import re
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks
from bs4 import BeautifulSoup

from . import config, db, events, live, mis_redes, momentazos, social, writer
from .wp import WordPress

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("creator-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines include API keys in URLs

intents = discord.Intents.default()
intents.members = True          # privileged: needed to see role changes
intents.message_content = True  # privileged: needed to read mod replies and announcements


class Bot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)
        self.wp = WordPress()
        self._locks: dict[int, asyncio.Lock] = {}

    async def setup_hook(self) -> None:
        db.init()
        self.add_dynamic_items(ApproveButton, ChangesButton, DismissButton)
        events.setup(self, mod_check=is_mod, to_text=html_to_text, status_embed=_with_status)
        momentazos.setup(self)
        live.setup(self, mod_check=is_mod, find_links=find_links_in_announcements, creator_role=creator_role)
        mis_redes.setup(self, mod_check=is_mod, creator_role=creator_role)
        if config.GUILD_ID:
            g = discord.Object(config.GUILD_ID)
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()
        daily_sweep.start()

    def lock(self, uid: int) -> asyncio.Lock:
        return self._locks.setdefault(uid, asyncio.Lock())


bot = Bot()


# ----------------------------------------------------------------------------- helpers

def creator_role(guild: discord.Guild) -> discord.Role | None:
    if config.CREATOR_ROLE_ID:
        return guild.get_role(config.CREATOR_ROLE_ID)
    want = config.CREATOR_ROLE_NAME.lower().lstrip("@")
    return next((r for r in guild.roles if r.name.lower().lstrip("@") == want), None)


def is_mod(member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if config.MOD_ROLE_IDS:
        return any(r.id in config.MOD_ROLE_IDS for r in member.roles)
    return member.guild_permissions.manage_messages


def review_channel() -> discord.TextChannel:
    ch = bot.get_channel(config.REVIEW_CHANNEL_ID)
    if ch is None:
        raise RuntimeError(f"Review channel {config.REVIEW_CHANNEL_ID} not found / not visible to the bot")
    return ch


def html_to_text(h: str) -> str:
    soup = BeautifulSoup(h, "html.parser")
    for a in soup.find_all("a"):
        a.replace_with(f"{a.get_text()} ({a.get('href')})")
    for t in soup.find_all(["h2", "h3"]):
        t.replace_with(f"\n## {t.get_text()}\n")
    return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n")).strip()


async def find_links_in_announcements(member: discord.Member) -> dict[str, str]:
    links: dict[str, str] = {}
    for cid in config.ANNOUNCE_CHANNEL_IDS:
        ch = bot.get_channel(cid)
        if ch is None:
            log.warning("announcement channel %s not visible", cid)
            continue
        try:
            async for msg in ch.history(limit=config.ANNOUNCE_HISTORY_LIMIT):
                for p, url in social.links_for_member(msg, member).items():
                    links.setdefault(p, url)
                if len(links) == len(social.PLATFORMS):
                    break
        except discord.Forbidden:
            log.warning("no permission to read history in %s", cid)
    return links


# ----------------------------------------------------------------------------- core flow

async def handle_new_creator(member: discord.Member) -> None:
    row = db.get(member.id)
    if row and row["status"] not in ("skipped_existing",):
        return  # already handled (or in progress)
    db.upsert(member.id, display_name=member.display_name, status="pending_links")
    links = await find_links_in_announcements(member)
    if links:
        db.upsert(member.id, links=links)
        await generate(member)
    else:
        await review_channel().send(
            f"🆕 **{member.display_name}** ({member.mention}) tiene el rol de creador, pero no encuentro sus redes "
            f"en los anuncios todavía. Lo volveré a intentar cada día; también podéis añadirlas con "
            f"`/redes-creador`.")


async def generate(member: discord.Member, feedback: str | None = None) -> None:
    async with bot.lock(member.id):
        row = db.get(member.id)
        fb = row["feedback"] + ([feedback] if feedback else [])
        db.upsert(member.id, status="drafting", feedback=fb, display_name=member.display_name)
        old_msg_id = row["review_message_id"]
        if old_msg_id and row["status"] == "in_review" and member.id not in _busy:  # e.g. /redes-creador or /generar-articulo on a live review
            try:
                await lock_review_message(await review_channel().fetch_message(old_msg_id), member.id,
                                          "🔄 Regenerando el artículo…")
            except (discord.HTTPException, RuntimeError):
                pass
        try:
            profiles, links = await social.read_profiles(row["links"], discover=not row.get("manual_links"),
                                                          names=social.member_names(member))
            if not profiles:
                raise RuntimeError("No pude leer ningún perfil")
            # The writer sees the titles/views of their best clips, not the image URLs.
            for_llm = {p: {k: ([{kk: vv for kk, vv in b.items() if kk != "thumbnail"} for b in v] if k == "best" else v)
                           for k, v in d.items()} for p, d in profiles.items()}
            art = await writer.write_article(member.display_name, for_llm, row.get("article"), fb)
            art.title = article_title(member.display_name)  # fixed format, set in code (not by the writer)
            slug =re.sub(r"\W+", "-", member.name.lower())
            # Featured image: their Twitch/YouTube logo (profile picture) first, then any other profile image.
            image = next((profiles[p]["image"] for p in ("twitch", "youtube", *profiles)
                          if p in profiles and profiles[p].get("image")), None)
            best = await upload_best(profiles, slug)
            # The gallery is added at save time only, so the stored draft the writer revises stays plain text.
            post = await bot.wp.save_draft(row["wp_post_id"], art.title, with_gallery(art.content_html, best),
                                           art.excerpt, art.tags, image, slug, date_gmt=joined_gmt(member))
            version = row["version"] + 1
            db.upsert(member.id, status="in_review", links=links, wp_post_id=post["id"],
                      article=art.model_dump(), version=version, last_error=None)
            msg = await post_review(member, art, links, row["links"], post["id"], version, fb,
                                    logo=image, best=best)
            db.upsert(member.id, review_message_id=msg.id)
        except Exception as e:
            log.exception("generation failed for %s", member)
            db.upsert(member.id, status="error", last_error=str(e)[:500])
            await review_channel().send(f"⚠️ No pude generar el artículo de **{member.display_name}**: `{str(e)[:300]}`. "
                                        f"Usad `/generar-articulo` para reintentar.")
            return
        if old_msg_id:
            await close_review_message(old_msg_id, "↩️ Sustituido por una nueva versión")


def clean_name(name: str) -> str:
    """Discord display name without decorative emoji/symbols at the edges: '🌸 Flor 🦄' -> 'Flor'."""
    return re.sub(r"^[^\w¿¡]+|[^\w!?.)]+$", "", name).strip() or name.strip()


def article_title(name: str) -> str:
    return f"Conozcamos más sobre {clean_name(name)} y su contenido"


def joined_gmt(member: discord.Member) -> str | None:
    """When they joined the server, as WordPress wants date_gmt (UTC, no offset)."""
    j = member.joined_at
    return j.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") if j else None


async def upload_best(profiles: dict, slug: str) -> list[dict]:
    """Copy the thumbnails of their best clips/videos (up to 4, mixing platforms) into WordPress."""
    pools = [list(d.get("best") or []) for d in profiles.values()]
    picked: list[dict] = []
    while len(picked) < 4 and any(pools):
        for pool in pools:
            if pool and len(picked) < 4:
                picked.append(pool.pop(0))
    out = []
    for i, b in enumerate(picked):
        m = await bot.wp.image(b["thumbnail"], f"{slug}-destacado-{i + 1}")
        if m:
            out.append(b | {"media_id": m[0], "src": m[1]})
    return out


def with_gallery(content_html: str, best: list[dict]) -> str:
    """Insert a 'Lo mejor de su canal' gallery before the final section ('Dónde encontrarle')."""
    if not best:
        return content_html
    figs = "".join(
        f'<figure class="wp-block-image size-large"><a href="{html.escape(b["url"] or "")}" target="_blank" rel="noopener">'
        f'<img src="{html.escape(b["src"])}" alt="{html.escape(b.get("title") or "")}" class="wp-image-{b["media_id"]}"/></a>'
        f'<figcaption class="wp-element-caption">{html.escape(b.get("title") or b["kind"])}</figcaption></figure>'
        for b in best)
    cols = 2 if len(best) != 3 else 3
    gallery = (f'<h2>Lo mejor de su canal</h2>'
               f'<figure class="wp-block-gallery has-nested-images columns-{cols} is-cropped">{figs}</figure>')
    cut = content_html.rfind("<h2")
    return content_html[:cut] + gallery + content_html[cut:] if cut > 0 else content_html + gallery


async def post_review(member, art, links, original_links, post_id, version, feedback,
                      logo: str | None = None, best: list[dict] | None = None) -> discord.Message:
    emb = discord.Embed(title=art.title[:256], description=art.excerpt[:1000], color=0xE8B000)
    emb.set_author(name=f"Nuevo creador: {member.display_name}", icon_url=member.display_avatar.url)
    if logo:
        emb.set_thumbnail(url=logo)
    if best:
        emb.set_image(url=best[0]["src"])
        emb.add_field(name="Imágenes añadidas", value="\n".join(
            f"• [{(b.get('title') or b['kind'])[:80]}]({b['url']})" for b in best)[:1024], inline=False)
    emb.add_field(name="Redes usadas", value="\n".join(
        f"{p}: {u}" + ("" if original_links.get(p) == u else " *(descubierta)*") for p, u in links.items())[:1024],
        inline=False)
    emb.add_field(name="Borrador en WordPress", value=f"[Vista previa]({bot.wp.preview_link(post_id)}) · "
                                                      f"[Editar]({bot.wp.edit_link(post_id)})", inline=False)
    if feedback:
        emb.add_field(name="Cambios aplicados", value="\n".join(f"• {f}" for f in feedback)[-1024:], inline=False)
    emb.set_footer(text=f"Versión {version} · Aprobad, pedid cambios (botón o respondiendo a este mensaje) o descartad.")
    text = f"# {art.title}\n\n{html_to_text(art.content_html)}\n"
    file = discord.File(io.BytesIO(text.encode()), filename=f"articulo-v{version}.md")
    return await review_channel().send(embed=emb, file=file, view=review_view(member.id))


def review_view(uid: int, disabled: bool = False) -> discord.ui.View:
    """The three review buttons; disabled=True greys them out while a moderator's action runs."""
    view = discord.ui.View(timeout=None)
    for item in (ApproveButton(uid), ChangesButton(uid), DismissButton(uid)):
        item.item.disabled = disabled
        view.add_item(item)
    return view


# Members whose review message is being handled right now. Checked and set with no await in
# between, so two clicks (same or different moderators) can never both start an action.
_busy: set[int] = set()


def _claim(uid: int) -> bool:
    if uid in _busy:
        return False
    _busy.add(uid)
    return True


def _with_status(message: discord.Message, note: str) -> discord.Embed:
    emb = message.embeds[0].copy() if message.embeds else discord.Embed()
    for i, f in enumerate(emb.fields):
        if f.name == "Estado":
            emb.remove_field(i)
            break
    emb.add_field(name="Estado", value=note, inline=False)
    return emb


async def lock_review_message(message: discord.Message, uid: int, note: str) -> None:
    """Grey out the buttons on a review message and say who is handling it."""
    try:
        await message.edit(embed=_with_status(message, note), view=review_view(uid, disabled=True))
    except discord.HTTPException as e:
        log.info("could not lock review message %s: %s", message.id, e)


async def unlock_review_message(message: discord.Message, uid: int, note: str) -> None:
    """Re-enable the buttons after an action failed, so the moderators can try again."""
    try:
        await message.edit(embed=_with_status(message, note), view=review_view(uid))
    except discord.HTTPException as e:
        log.info("could not unlock review message %s: %s", message.id, e)


async def close_review_message(message_id: int, note: str) -> None:
    try:
        msg = await review_channel().fetch_message(message_id)
        await msg.edit(embed=_with_status(msg, note), view=None)
    except discord.HTTPException as e:
        log.info("could not close review message %s: %s", message_id, e)


# ----------------------------------------------------------------------------- buttons

class _ReviewButton:
    """Shared logic; concrete classes also inherit DynamicItem with their own template."""
    action = ""

    def __init__(self, uid: int, label: str, style: discord.ButtonStyle, emoji: str) -> None:
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji,
                                           custom_id=f"creator:{self.action}:{uid}"))
        self.uid = uid

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["uid"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_mod(interaction.user):
            await interaction.response.send_message("Solo los moderadores pueden revisar artículos.", ephemeral=True)
            return False
        row = db.get(self.uid)
        if not row or row["status"] != "in_review" or row["review_message_id"] != interaction.message.id:
            await interaction.response.send_message("Este artículo ya no está pendiente de revisión.", ephemeral=True)
            return False
        if self.uid in _busy:
            await interaction.response.send_message("Otro moderador ya está gestionando este artículo.", ephemeral=True)
            return False
        return True


class ApproveButton(_ReviewButton, discord.ui.DynamicItem[discord.ui.Button], template=r"creator:approve:(?P<uid>\d+)"):
    action = "approve"

    def __init__(self, uid: int) -> None:
        super().__init__(uid, "Aprobar", discord.ButtonStyle.success, "✅")

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _claim(self.uid):
            await interaction.response.send_message("Otro moderador ya está gestionando este artículo.", ephemeral=True)
            return
        try:
            # Grey the buttons out in the same response, before anything slow happens.
            await interaction.response.edit_message(
                embed=_with_status(interaction.message, f"⏳ Publicando (aprobado por {interaction.user.mention})…"),
                view=review_view(self.uid, disabled=True))
            async with bot.lock(self.uid):
                row = db.get(self.uid)
                try:
                    post = await bot.wp.publish(row["wp_post_id"])
                except Exception as e:
                    log.exception("publish failed")
                    await unlock_review_message(interaction.message, self.uid,
                                                f"⚠️ Error al publicar; podéis reintentarlo: `{str(e)[:200]}`")
                    return
                db.upsert(self.uid, status="published")
            await close_review_message(interaction.message.id,
                                       f"✅ Aprobado por {interaction.user.mention} y publicado: {post['link']}")
        finally:
            _busy.discard(self.uid)


class ChangesButton(_ReviewButton, discord.ui.DynamicItem[discord.ui.Button], template=r"creator:changes:(?P<uid>\d+)"):
    action = "changes"

    def __init__(self, uid: int) -> None:
        super().__init__(uid, "Pedir cambios", discord.ButtonStyle.primary, "✏️")

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ChangesModal(self.uid))


class DismissButton(_ReviewButton, discord.ui.DynamicItem[discord.ui.Button], template=r"creator:dismiss:(?P<uid>\d+)"):
    action = "dismiss"

    def __init__(self, uid: int) -> None:
        super().__init__(uid, "Descartar", discord.ButtonStyle.danger, "🗑️")

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _claim(self.uid):
            await interaction.response.send_message("Otro moderador ya está gestionando este artículo.", ephemeral=True)
            return
        try:
            await interaction.response.edit_message(
                embed=_with_status(interaction.message, f"⏳ Descartando (por {interaction.user.mention})…"),
                view=review_view(self.uid, disabled=True))
            row = db.get(self.uid)
            try:
                await bot.wp.trash(row["wp_post_id"])
            except Exception as e:
                log.warning("trash failed: %s", e)
            db.upsert(self.uid, status="dismissed")
            await close_review_message(interaction.message.id, f"🗑️ Descartado por {interaction.user.mention}")
        finally:
            _busy.discard(self.uid)


class ChangesModal(discord.ui.Modal, title="¿Qué hay que cambiar?"):
    changes = discord.ui.TextInput(label="Cambios", style=discord.TextStyle.paragraph, max_length=1500)

    def __init__(self, uid: int) -> None:
        super().__init__()
        self.uid = uid

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # The modal may have stayed open while someone else acted: check again before claiming.
        row = db.get(self.uid)
        if (not row or row["status"] != "in_review" or row["review_message_id"] != interaction.message.id
                or not _claim(self.uid)):
            await interaction.response.send_message(
                "Este artículo ya no está pendiente de revisión o lo está gestionando otro moderador.", ephemeral=True)
            return
        try:
            await interaction.response.edit_message(
                embed=_with_status(interaction.message,
                                   f"✏️ Rehaciendo con los cambios de {interaction.user.mention}…"),
                view=review_view(self.uid, disabled=True))
            member = interaction.guild.get_member(self.uid) or await interaction.guild.fetch_member(self.uid)
            await generate(member, f"{interaction.user.display_name}: {self.changes.value}")
        finally:
            _busy.discard(self.uid)


# ----------------------------------------------------------------------------- events

@bot.event
async def on_scheduled_event_create(se: discord.ScheduledEvent) -> None:
    await events.on_scheduled_event(se)


@bot.event
async def on_scheduled_event_update(before: discord.ScheduledEvent, after: discord.ScheduledEvent) -> None:
    await events.on_scheduled_event(after)


@bot.event
async def on_scheduled_event_delete(se: discord.ScheduledEvent) -> None:
    await events.on_scheduled_event(se, deleted=True)


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s", bot.user)
    await bot.wp.resolve_base()


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member) -> None:
    role = creator_role(after.guild)
    if role and role not in before.roles and role in after.roles:
        log.info("%s got the creator role", after)
        await handle_new_creator(after)


@bot.event
async def on_message(msg: discord.Message) -> None:
    if await events.on_message(msg):  # a mod replying to an event card
        return
    if msg.author.bot and msg.channel.id not in config.ANNOUNCE_CHANNEL_IDS:
        return
    # 1) Mod replies to a review message = change request.
    if msg.channel.id == config.REVIEW_CHANNEL_ID and msg.reference and msg.reference.message_id:
        row = db.by_review_message(msg.reference.message_id)
        if row and row["status"] == "in_review" and is_mod(msg.author) and msg.content.strip():
            uid = row["discord_id"]
            if not _claim(uid):
                await msg.reply("Otro moderador ya está gestionando este artículo; espera a la nueva versión.",
                                mention_author=False)
                return
            try:
                await msg.add_reaction("✏️")
                review = msg.reference.resolved if isinstance(msg.reference.resolved, discord.Message) \
                    else await msg.channel.fetch_message(msg.reference.message_id)
                await lock_review_message(review, uid, f"✏️ Rehaciendo con los cambios de {msg.author.mention}…")
                member = msg.guild.get_member(uid) or await msg.guild.fetch_member(uid)
                await generate(member, f"{msg.author.display_name}: {msg.content.strip()}")
            finally:
                _busy.discard(uid)
        return
    # 2) New live announcement for someone still waiting for links.
    if msg.channel.id in config.ANNOUNCE_CHANNEL_IDS and msg.guild:
        role = creator_role(msg.guild)
        if not role:
            return
        for m in role.members:
            row = db.get(m.id)
            if row and row["status"] == "pending_links":
                links = social.links_for_member(msg, m)
                if links:
                    db.upsert(m.id, links=row["links"] | links)
                    await generate(m)


@tasks.loop(time=dt.time(hour=config.DAILY_SWEEP_HOUR, tzinfo=ZoneInfo(config.TIMEZONE)))
async def daily_sweep() -> None:
    await run_sweep()


async def run_sweep() -> None:
    """Catch anyone the live event missed, and retry people still waiting for links."""
    for guild in bot.guilds:
        if config.GUILD_ID and guild.id != config.GUILD_ID:
            continue
        role = creator_role(guild)
        if not role:
            log.error("Role %r not found in %s", config.CREATOR_ROLE_NAME, guild)
            continue
        first_run = not db.known_ids()
        budget = config.BACKFILL_PER_SWEEP
        for m in sorted(role.members, key=lambda x: x.joined_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc)):
            row = db.get(m.id)
            if row is None and first_run and not config.BACKFILL_EXISTING:
                db.upsert(m.id, display_name=m.display_name, status="skipped_existing")
            elif row is None and budget > 0:
                budget -= 1
                await handle_new_creator(m)
            elif row is None:
                continue  # over today's budget; picked up on a later sweep
            elif row["status"] == "error" and row["links"] and budget > 0:
                budget -= 1  # retry yesterday's failures (e.g. API outage, no credits)
                await generate(m)
            elif row["status"] == "pending_links":
                links = await find_links_in_announcements(m)
                if links:
                    db.upsert(m.id, links=links)
                    await generate(m)


@daily_sweep.before_loop
async def _wait() -> None:
    await bot.wait_until_ready()
    await run_sweep()  # also sweep once at startup


# ----------------------------------------------------------------------------- slash commands

def _mods_only(interaction: discord.Interaction) -> bool:
    return is_mod(interaction.user)


@bot.tree.command(name="redes-creador", description="Añade o corrige las redes de un creador (moderadores)")
@app_commands.describe(
    twitch="URL o usuario; escribe - para quitar la que hay",
    youtube="URL o @canal; escribe - para quitar la que hay",
    instagram="URL o usuario; escribe - para quitar la que hay",
    tiktok="URL o @usuario; escribe - para quitar la que hay",
    reemplazar_todo="Borra las redes que no indiques (útil si las detectadas son de otra persona)")
@app_commands.check(_mods_only)
async def redes_creador(interaction: discord.Interaction, miembro: discord.Member,
                        twitch: str | None = None, youtube: str | None = None,
                        instagram: str | None = None, tiktok: str | None = None,
                        reemplazar_todo: bool = False) -> None:
    given = {"twitch": twitch, "youtube": youtube, "instagram": instagram, "tiktok": tiktok}
    row = db.get(miembro.id) or db.upsert(miembro.id, display_name=miembro.display_name, status="pending_links")
    links = {} if reemplazar_todo else dict(row["links"])
    bad = []
    for p, v in given.items():
        if not v:
            continue
        if v.strip() == "-":
            links.pop(p, None)
            continue
        url = social.normalize_user_input(p, v)
        if url:
            links[p] = url
        else:
            bad.append(p)
    changed = links != row["links"]
    # Corrected links are authoritative: the next draft starts from scratch, not from the old text.
    db.upsert(miembro.id, links=links, **({"article": None, "manual_links": True} if changed else {}))
    txt = "Redes guardadas:\n" + ("\n".join(f"• {p}: {u}" for p, u in links.items()) or "(ninguna)")
    if bad:
        txt += f"\nNo entendí: {', '.join(bad)}"
    regenerate = links and (row["status"] in ("pending_links", "error", "skipped_existing")
                            or (changed and row["status"] == "in_review"))
    if regenerate:
        txt += "\nRehaciendo el artículo con estas redes…"
    await interaction.response.send_message(txt, ephemeral=True)
    if regenerate:
        await generate(miembro, "Redes corregidas por un moderador; usa solo estas." if row["status"] == "in_review" else None)


@bot.tree.command(name="generar-articulo", description="(Re)genera el artículo de un creador (moderadores)")
@app_commands.check(_mods_only)
async def generar_articulo(interaction: discord.Interaction, miembro: discord.Member) -> None:
    row = db.get(miembro.id)
    if row and row["status"] == "published":
        await interaction.response.send_message("Ese artículo ya está publicado.", ephemeral=True)
        return
    await interaction.response.send_message(f"Generando el artículo de {miembro.display_name}…", ephemeral=True)
    if not row or not row["links"]:
        db.upsert(miembro.id, display_name=miembro.display_name,
                  links=await find_links_in_announcements(miembro), status="pending_links")
        if not db.get(miembro.id)["links"]:
            await interaction.followup.send("No encuentro sus redes. Añádelas con `/redes-creador`.", ephemeral=True)
            return
    await generate(miembro)


@bot.tree.command(name="fulgencio", description="Habla con Fulgencio")
@app_commands.describe(texto="Lo que quieras decirle a Fulgencio")
async def fulgencio(interaction: discord.Interaction, texto: str | None = None) -> None:
    """Only registers the command with Discord: another program listening on this bot answers it.
    It must NOT respond here, or it would steal the interaction from that program."""
    log.info("/fulgencio used by %s (handled elsewhere)", interaction.user)


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    msg = "Solo los moderadores pueden usar este comando." if isinstance(error, app_commands.CheckFailure) \
        else f"Error: {error}"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


def run() -> None:
    bot.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    run()
