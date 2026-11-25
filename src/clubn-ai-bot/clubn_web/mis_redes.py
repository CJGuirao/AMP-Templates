"""/mis_redes: a creator proposes changes to THEIR OWN entry in the creator database. No AI.

Who
    Only members of the Club Ñ guild who have the creator role. The entry edited is always the caller's
    own (interaction.user.id); the command has no "member" option, so nobody can touch anyone else's entry.

What
    Twitch, YouTube, Instagram and TikTok. Each value is a URL on that platform's own domain or a bare
    handle, checked against strict per-platform rules (exact host allowlist, https only, no user/port,
    one profile path, handle charset and length, reserved paths refused, ASCII only after NFKC).
    Twitch logins are checked against the Twitch API and YouTube channels are resolved to their UC… id
    when the API keys are set. "-" removes a network.

Approval
    Nothing is written straight away. The request is stored in SQLite and a card with ✅ / ❌ is posted
    in the moderation channel (MIS_REDES_REVIEW_CHANNEL_ID, default REVIEW_CHANNEL_ID). Only moderators can
    decide, and not on their own request (unless MIS_REDES_SELF_APPROVE). The decision is claimed with an
    atomic UPDATE … WHERE status='pending', so two moderators can never both act on one card. On ✅ the
    bot re-reads the database from Discord, re-checks that the creator still has the role, re-validates
    the stored values and writes only the fields that were requested.

Limits
    All replies are ephemeral. One open request per creator (a new one replaces the old card), at least
    MIS_REDES_COOLDOWN_MIN minutes between requests, MIS_REDES_MAX_PER_DAY per 24 h, at most
    MIS_REDES_MAX_PENDING open requests in total, and 6 attempts (valid or not) per 10 minutes.

Audit
    Every request stays in the SQLite table social_requests (who, before, after, who decided, when,
    reason, what was written). Rows are never deleted. The card in the moderation channel keeps the final
    state too. List it with:  python -m bot.mis_redes --audit [--user DISCORD_ID] [--limit 50]
"""
from __future__ import annotations

import asyncio
import collections
import datetime as dt
import json
import logging
import re
import secrets
import time
import unicodedata
from typing import Optional
from urllib.parse import urlsplit

import discord
import httpx
from discord import app_commands

from . import config, db, live, social

log = logging.getLogger(__name__)

PLATFORMS = ("twitch", "youtube", "instagram", "tiktok")
LABEL = {"twitch": "Twitch", "youtube": "YouTube", "instagram": "Instagram", "tiktok": "TikTok"}
HOSTS = {
    "twitch": {"twitch.tv", "www.twitch.tv", "m.twitch.tv"},
    "youtube": {"youtube.com", "www.youtube.com", "m.youtube.com"},
    "instagram": {"instagram.com", "www.instagram.com"},
    "tiktok": {"tiktok.com", "www.tiktok.com", "m.tiktok.com"},
}
RESERVED = {
    "twitch": {"videos", "directory", "settings", "p", "search", "downloads", "jobs", "turbo", "prime",
               "subscriptions", "inventory", "wallet", "drops", "friends", "messages", "payments", "broadcast",
               "moderator", "popout", "embed", "login", "signup", "store", "team", "following", "u"},
    "instagram": {"p", "reel", "reels", "explore", "stories", "accounts", "direct", "tv", "about", "developer",
                  "legal", "web", "challenge", "emails", "session"},
    "tiktok": set(),
}
RX = {
    "twitch": re.compile(r"[a-z0-9][a-z0-9_]{2,24}"),
    "instagram": re.compile(r"(?!.*\.\.)(?!\.)[a-z0-9._]{1,30}(?<!\.)"),
    "tiktok": re.compile(r"[a-z0-9_.]{2,24}(?<!\.)"),
    "yt_handle": re.compile(r"@[A-Za-z0-9._\-]{3,30}"),
    "yt_channel": re.compile(r"UC[A-Za-z0-9_\-]{22}"),
    "yt_legacy": re.compile(r"[A-Za-z0-9._\-]{1,100}"),
}
YT_TABS = {"videos", "featured", "streams", "shorts", "about", "live", "playlists", "community", "podcasts"}
MAX_INPUT = 120
SCHEMA = """
CREATE TABLE IF NOT EXISTS social_requests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id        INTEGER NOT NULL,
    user_tag          TEXT,
    changes           TEXT NOT NULL,      -- {platform: new value or null (= remove)}, only what was asked
    before            TEXT NOT NULL,      -- the same fields as they were when the request was made
    status            TEXT NOT NULL,      -- pending, deciding, approved, rejected, superseded, expired, failed
    nonce             TEXT NOT NULL,
    review_message_id INTEGER,
    created_at        REAL NOT NULL,
    decided_by        INTEGER,
    decided_by_tag    TEXT,
    decided_at        REAL,
    reason            TEXT,
    applied           TEXT                -- the record written to the database on approval
);
CREATE INDEX IF NOT EXISTS idx_sr_user ON social_requests(discord_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sr_status ON social_requests(status);
"""


class Invalid(ValueError):
    """A value that is not accepted; the message is shown to the creator."""


# ============================================================================ validation (pure)

def _clean(raw: str) -> str:
    v = unicodedata.normalize("NFKC", raw or "").strip()
    if v.startswith("<") and v.endswith(">") and v[1:5].lower() == "http":  # Discord's <url> (no embed) form
        v = v[1:-1].strip()
    if not v:
        raise Invalid("está vacío")
    if len(v) > MAX_INPUT:
        raise Invalid(f"es demasiado largo (máximo {MAX_INPUT} caracteres)")
    if not v.isascii() or any(not c.isprintable() or c.isspace() for c in v):
        raise Invalid("tiene espacios o caracteres no permitidos")
    if any(c in v for c in "%\\\"'<>`|{}^[]"):
        raise Invalid("tiene caracteres no permitidos")
    return v


def _segments(platform: str, v: str) -> list[str] | None:
    """Path segments if v is a URL on the platform's domain, None if it is a bare handle."""
    low = v.lower()
    if "://" in low:
        if not low.startswith(("https://", "http://")):
            raise Invalid("solo se aceptan enlaces https")
    elif any(low.startswith(h + "/") or low == h for h in HOSTS[platform]):
        v = "https://" + v
    elif "/" in v or ":" in v:
        raise Invalid(f"no es un enlace de {LABEL[platform]}")
    else:
        return None
    try:
        u = urlsplit(v)
    except ValueError:
        raise Invalid("no es un enlace válido")
    if u.username is not None or u.password is not None or "@" in u.netloc or ":" in u.netloc:
        raise Invalid("no es un enlace válido")
    if (u.hostname or "").lower() not in HOSTS[platform]:
        raise Invalid(f"no es un enlace de {LABEL[platform]} (dominio no permitido)")
    segs = [s for s in u.path.split("/") if s]
    if not segs:
        raise Invalid("le falta el nombre del canal o perfil")
    return segs


def parse(platform: str, raw: str):
    """Validated, normalized value, or None for "-" (remove). Raises Invalid.

    twitch/instagram/tiktok -> lower-case handle without @
    youtube -> ("handle", "@name") | ("channel", "UC…") | ("legacy", "c/name" or "user/name")"""
    if platform not in PLATFORMS:
        raise Invalid("red no soportada")
    if (raw or "").strip() == "-":
        return None
    v = _clean(raw)
    segs = _segments(platform, v)

    if platform == "youtube":
        if segs is None:
            if RX["yt_channel"].fullmatch(v):
                return ("channel", v)
            h = v if v.startswith("@") else "@" + v
            if RX["yt_handle"].fullmatch(h):
                return ("handle", h)
            raise Invalid("no es un @canal de YouTube válido")
        head, rest = segs[0], segs[1:]
        if head.startswith("@"):
            kind, val = "handle", head
        elif head in ("channel", "c", "user") and rest:
            kind, val, rest = ("channel" if head == "channel" else "legacy"), rest[0], rest[1:]
        else:
            raise Invalid("el enlace de YouTube debe ser de un canal (youtube.com/@canal)")
        if len(rest) > 1 or (rest and rest[0].lower() not in YT_TABS):
            raise Invalid("el enlace de YouTube debe ser de un canal, no de un vídeo")
        if kind == "handle" and RX["yt_handle"].fullmatch(val):
            return ("handle", val)
        if kind == "channel" and RX["yt_channel"].fullmatch(val):
            return ("channel", val)
        if kind == "legacy" and RX["yt_legacy"].fullmatch(val):
            return ("legacy", f"{head}/{val}")
        raise Invalid("el canal de YouTube no tiene un formato válido")

    if segs is None:
        handle = v[1:] if v.startswith("@") else v
    else:
        if len(segs) != 1:
            raise Invalid(f"el enlace debe ser el de tu perfil de {LABEL[platform]}, no de una publicación")
        handle = segs[0]
        if platform == "tiktok":
            if not handle.startswith("@"):
                raise Invalid("el enlace de TikTok debe ser tiktok.com/@usuario")
            handle = handle[1:]
        elif handle.startswith("@"):
            raise Invalid(f"no es un perfil de {LABEL[platform]} válido")
    handle = handle.lower()
    if handle in RESERVED[platform] or not RX[platform].fullmatch(handle):
        raise Invalid(f"no es un usuario de {LABEL[platform]} válido")
    return handle


def youtube_url(kind: str, val: str) -> str:
    return f"https://www.youtube.com/{val if kind == 'handle' else 'channel/' + val}"


def url_of(platform: str, value) -> str | None:
    if value is None:
        return None
    if platform == "youtube":
        return value["url"]
    return social.canonical(platform, ("@" + value) if platform == "tiktok" else value)


def current_values(rec: dict | None) -> dict:
    rec = rec or {}
    yt = {"channel_id": rec["youtube_channel_id"], "url": rec.get("youtube_url") or
          youtube_url("channel", rec["youtube_channel_id"])} if rec.get("youtube_channel_id") else None
    return {"twitch": rec.get("twitch"), "youtube": yt, "instagram": rec.get("instagram"), "tiktok": rec.get("tiktok")}


def same(platform: str, a, b) -> bool:
    if platform == "youtube":
        return (a or {}).get("channel_id") == (b or {}).get("channel_id")
    return a == b


def apply_changes(rec: dict, changes: dict) -> dict:
    """The record with only the requested fields changed, normalized. Raises Invalid if a value doesn't survive."""
    rec = dict(rec)
    for p, v in changes.items():
        if p not in PLATFORMS:
            raise Invalid(f"campo desconocido {p!r}")
        if p == "youtube":
            if v is not None and not (isinstance(v, dict) and RX["yt_channel"].fullmatch(v.get("channel_id") or "")):
                raise Invalid("canal de YouTube no válido")
            rec["youtube_channel_id"] = v["channel_id"] if v else None
            rec["youtube_url"] = v["url"] if v else None
        elif v is None:
            rec[p] = None
        else:
            rec[p] = v
    rec["updated"] = dt.date.today().isoformat()
    out = live.normalize_record(rec)
    got = current_values(out)
    for p, v in changes.items():  # every requested value must have survived the database's own checks
        if not same(p, got[p], v) or (p == "youtube" and v and got[p]["url"] != v["url"]):
            raise Invalid(f"el valor de {LABEL[p]} no es válido")
    return out


# ============================================================================ checks against the platforms

_checker = None


async def verify(platform: str, parsed, client: httpx.AsyncClient):
    """Turn a parsed value into what is stored. Raises Invalid when the profile doesn't exist."""
    global _checker
    if parsed is None:
        return None
    if platform == "twitch":
        if config.TWITCH_CLIENT_ID and config.TWITCH_CLIENT_SECRET:
            _checker = _checker or live.Checker()
            h = await _checker._twitch_headers(client)
            r = await client.get("https://api.twitch.tv/helix/users", params={"login": parsed}, headers=h)
            r.raise_for_status()
            if not r.json().get("data"):
                raise Invalid(f"no existe el canal de Twitch `{parsed}`")
        return parsed
    if platform == "youtube":
        kind, val = parsed
        if kind == "channel" and config.YOUTUBE_API_KEY:
            r = await client.get("https://www.googleapis.com/youtube/v3/channels",
                                 params={"part": "id", "id": val, "key": config.YOUTUBE_API_KEY})
            r.raise_for_status()
            if not r.json().get("items"):
                raise Invalid("no existe ese canal de YouTube")
            return {"channel_id": val, "url": youtube_url("channel", val)}
        url = youtube_url(kind, val) if kind != "legacy" else f"https://www.youtube.com/{val}"
        ch = await live.resolve_youtube(url, client)
        if not ch or not RX["yt_channel"].fullmatch(ch):
            raise Invalid("no encuentro ese canal de YouTube")
        return {"channel_id": ch, "url": url if kind == "handle" else youtube_url("channel", ch)}
    return parsed  # Instagram / TikTok block automated reads: the moderators check them by eye


# ============================================================================ storage

def init() -> None:
    with db._conn() as c:
        c.executescript(SCHEMA)


def _req(r) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in ("changes", "before", "applied"):
        d[k] = json.loads(d[k]) if d.get(k) else None
    return d


def get_request(rid: int) -> dict | None:
    with db._conn() as c:
        return _req(c.execute("SELECT * FROM social_requests WHERE id=?", (rid,)).fetchone())


def open_request(uid: int) -> dict | None:
    with db._conn() as c:
        return _req(c.execute("SELECT * FROM social_requests WHERE discord_id=? AND status IN ('pending','deciding') "
                              "ORDER BY id DESC LIMIT 1", (uid,)).fetchone())


def create_request(uid: int, tag: str, changes: dict, before: dict) -> dict:
    with db._conn() as c:
        cur = c.execute("INSERT INTO social_requests (discord_id, user_tag, changes, before, status, nonce, created_at) "
                        "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                        (uid, tag, json.dumps(changes, ensure_ascii=False), json.dumps(before, ensure_ascii=False),
                         secrets.token_hex(4), time.time()))
        rid = cur.lastrowid
    return get_request(rid)


def set_message(rid: int, message_id: int) -> None:
    with db._conn() as c:
        c.execute("UPDATE social_requests SET review_message_id=? WHERE id=?", (message_id, rid))


def transition(rid: int, frm: tuple[str, ...], to: str, **fields) -> bool:
    """Atomic status change: True only for the one caller that moved it from one of `frm`."""
    fields = {k: (json.dumps(v, ensure_ascii=False) if k == "applied" and v is not None else v) for k, v in fields.items()}
    sets = ", ".join(["status=?", *(f"{k}=?" for k in fields)])
    with db._conn() as c:
        cur = c.execute(f"UPDATE social_requests SET {sets} WHERE id=? AND status IN ({','.join('?' * len(frm))})",
                        (to, *fields.values(), rid, *frm))
        return cur.rowcount == 1


def limits_reason(uid: int, now: float | None = None) -> str | None:
    """Why this creator can't send a request now, or None."""
    now = time.time() if now is None else now
    with db._conn() as c:
        mine = [r[0] for r in c.execute("SELECT created_at FROM social_requests WHERE discord_id=? AND status!='failed' "
                                        "AND created_at>? ORDER BY created_at DESC", (uid, now - 86400))]
        pending = c.execute("SELECT COUNT(*) FROM social_requests WHERE status IN ('pending','deciding')").fetchone()[0]
    if mine and now - mine[0] < config.MIS_REDES_COOLDOWN_MIN * 60:
        wait = int(config.MIS_REDES_COOLDOWN_MIN * 60 - (now - mine[0])) // 60 + 1
        return f"Espera {wait} min antes de enviar otra solicitud."
    if len(mine) >= config.MIS_REDES_MAX_PER_DAY:
        return f"Ya has enviado {len(mine)} solicitudes en las últimas 24 horas. Inténtalo mañana."
    if pending >= config.MIS_REDES_MAX_PENDING:
        return "Ahora mismo hay demasiadas solicitudes pendientes de revisar. Inténtalo más tarde."
    return None


def audit(user: int | None = None, limit: int = 50) -> list[dict]:
    q, args = "SELECT * FROM social_requests", []
    if user:
        q, args = q + " WHERE discord_id=?", [user]
    with db._conn() as c:
        return [_req(r) for r in c.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit))]


class Attempts:
    """In-memory limiter for attempts (valid or not), so nobody can hammer the platform APIs."""

    def __init__(self, n: int = 6, window: float = 600) -> None:
        self.n, self.window, self.hits = n, window, collections.defaultdict(collections.deque)

    def hit(self, uid: int, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        q = self.hits[uid]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.n:
            return False
        q.append(now)
        return True


# ============================================================================ text

def _show(platform: str, value) -> str:
    if value is None:
        return "—"
    shown = value["url"].split("youtube.com/", 1)[1] if platform == "youtube" else (
        "@" + value if platform == "tiktok" else value)
    return f"`{shown}` <{url_of(platform, value)}>"


def describe(changes: dict, before: dict) -> list[tuple[str, str]]:
    return [(LABEL[p], f"Antes: {_show(p, before.get(p))}\nDespués: {_show(p, v) if v is not None else '— (quitar)'}")
            for p, v in changes.items()]


def summary(rec: dict | None) -> str:
    vals = current_values(rec)
    return "\n".join(f"• {LABEL[p]}: {_show(p, vals[p])}" for p in PLATFORMS)


# ============================================================================ discord wiring

_ctx: dict = {}


def _review_channel(client) -> discord.abc.Messageable:
    ch = client.get_channel(config.MIS_REDES_REVIEW_CHANNEL_ID)
    if ch is None:
        raise RuntimeError(f"Canal de moderación {config.MIS_REDES_REVIEW_CHANNEL_ID} no visible para el bot")
    return ch


def card(req: dict, member: discord.abc.User | None, status: str | None = None, open_: bool = True):
    name = discord.utils.escape_markdown(getattr(member, "display_name", None) or req.get("user_tag") or "?")[:100]
    emb = discord.Embed(title="Solicitud de cambio de redes", color=0x5865F2 if open_ else 0x99AAB5,
                        description=f"<@{req['discord_id']}> (`{req['discord_id']}`) quiere actualizar "
                                    f"**su propia** entrada en la base de creadores.")
    if member is not None:
        emb.set_author(name=name, icon_url=member.display_avatar.url)
    for label, text in describe(req["changes"], req["before"]):
        emb.add_field(name=label, value=text[:1024], inline=False)
    emb.add_field(name="Qué revisar", value="Que cada perfil sea de esta persona (Instagram y TikTok no se pueden "
                                            "comprobar automáticamente).", inline=False)
    if status:
        emb.add_field(name="Estado", value=status[:1024], inline=False)
    emb.set_footer(text=f"Solicitud #{req['id']} · /mis_redes")
    emb.timestamp = dt.datetime.fromtimestamp(req["created_at"], dt.timezone.utc)
    view = None
    if open_:
        view = discord.ui.View(timeout=None)
        view.add_item(ApproveRequest(req["id"], req["nonce"]))
        view.add_item(RejectRequest(req["id"], req["nonce"]))
    return emb, view


async def _notify(client, uid: int, text: str) -> None:
    if not config.MIS_REDES_DM:
        return
    try:
        user = client.get_user(uid) or await client.fetch_user(uid)
        await user.send(text, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        log.info("mis_redes: could not DM %s: %s", uid, e)


async def _close(message: discord.Message | None, req: dict, member, status: str) -> None:
    if message is None:
        return
    emb, _ = card(req, member, status, open_=False)
    try:
        await message.edit(embed=emb, view=None)
    except discord.HTTPException as e:
        log.info("mis_redes: could not close card %s: %s", message.id, e)


async def _decision_check(interaction: discord.Interaction, rid: int, nonce: str) -> dict | None:
    """The request if this moderator may decide on it now; otherwise answers the interaction and returns None."""
    async def no(text: str) -> None:
        await interaction.response.send_message(text, ephemeral=True)

    if interaction.channel_id != config.MIS_REDES_REVIEW_CHANNEL_ID or not _ctx["mod_check"](interaction.user):
        await no("Solo los moderadores pueden revisar estas solicitudes.")
        return None
    req = get_request(rid)
    if not req or req["nonce"] != nonce or req["review_message_id"] != interaction.message.id:
        await no("Esta solicitud no es válida.")
        return None
    if req["discord_id"] == interaction.user.id and not config.MIS_REDES_SELF_APPROVE:
        await no("No puedes revisar tu propia solicitud: tiene que hacerlo otro moderador.")
        return None
    if req["status"] != "pending":
        await no("Esta solicitud ya no está pendiente.")
        return None
    if time.time() - req["created_at"] > config.MIS_REDES_EXPIRE_DAYS * 86400:
        if transition(rid, ("pending",), "expired", decided_at=time.time()):
            await _close(interaction.message, req, None, "⌛ Caducada: el creador puede enviar una nueva.")
        await no("Esta solicitud ha caducado.")
        return None
    return req


class ApproveRequest(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"misredes:ok:(?P<rid>\d+):(?P<nonce>[0-9a-f]{8})"):
    def __init__(self, rid: int, nonce: str) -> None:
        super().__init__(discord.ui.Button(label="Aprobar", style=discord.ButtonStyle.success, emoji="✅",
                                           custom_id=f"misredes:ok:{rid}:{nonce}"))
        self.rid, self.nonce = rid, nonce

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["rid"]), match["nonce"])

    async def callback(self, interaction: discord.Interaction) -> None:
        req = await _decision_check(interaction, self.rid, self.nonce)
        if not req:
            return
        mod = interaction.user
        if not transition(self.rid, ("pending",), "deciding", decided_by=mod.id, decided_by_tag=str(mod)):
            await interaction.response.send_message("Otro moderador ya está con esta solicitud.", ephemeral=True)
            return
        emb, _ = card(req, None, f"⏳ Aplicando (aprobado por {mod.mention})…", open_=False)
        await interaction.response.edit_message(embed=emb, view=None)
        guild, uid = interaction.guild, req["discord_id"]
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.HTTPException:
                member = None
        role = _ctx["creator_role"](guild)
        if member is None or role is None or role not in member.roles:
            transition(self.rid, ("deciding",), "rejected", decided_at=time.time(),
                       reason="ya no tiene el rol de creador o no está en el servidor")
            await _close(interaction.message, req, member, "❌ No aplicada: ya no tiene el rol de creador.")
            return
        cdb = live.shared_db()
        try:
            async with cdb.write_lock:
                await cdb.load()  # fresh copy: apply only the requested fields on top of what is there now
                base = cdb.records.get(str(uid)) or {"discord_id": str(uid), "name": member.display_name}
                rec = await cdb.save(apply_changes(base, req["changes"]))
        except Exception as e:
            log.exception("mis_redes: applying request %s failed", self.rid)
            transition(self.rid, ("deciding",), "pending", decided_by=None, decided_by_tag=None)
            emb, view = card(req, member, f"⚠️ No se pudo guardar; podéis reintentarlo. `{str(e)[:150]}`")
            await interaction.message.edit(embed=emb, view=view)
            return
        transition(self.rid, ("deciding",), "approved", decided_at=time.time(), applied=rec)
        try:
            _sync_article_links(uid, member.display_name, req["changes"])
        except Exception:
            log.exception("mis_redes: could not copy request %s to the article bot's links", self.rid)
        log.info("mis_redes: request %s for %s approved by %s: %s", self.rid, uid, mod, req["changes"])
        await _close(interaction.message, req, member, f"✅ Aprobada por {mod.mention} y guardada en la base.")
        await _notify(interaction.client, uid, "✅ Un moderador ha aprobado tus cambios de redes en Club Ñ. Ya están "
                                               "guardados:\n" + summary(rec))


class RejectRequest(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"misredes:no:(?P<rid>\d+):(?P<nonce>[0-9a-f]{8})"):
    def __init__(self, rid: int, nonce: str) -> None:
        super().__init__(discord.ui.Button(label="Rechazar", style=discord.ButtonStyle.danger, emoji="✖️",
                                           custom_id=f"misredes:no:{rid}:{nonce}"))
        self.rid, self.nonce = rid, nonce

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["rid"]), match["nonce"])

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _decision_check(interaction, self.rid, self.nonce):
            await interaction.response.send_modal(RejectModal(self.rid, self.nonce))


class RejectModal(discord.ui.Modal, title="Rechazar solicitud de redes"):
    reason = discord.ui.TextInput(label="Motivo (se le envía al creador)", style=discord.TextStyle.paragraph,
                                  required=False, max_length=300)

    def __init__(self, rid: int, nonce: str) -> None:
        super().__init__()
        self.rid, self.nonce = rid, nonce

    async def on_submit(self, interaction: discord.Interaction) -> None:
        req = get_request(self.rid)
        mod = interaction.user
        ok = (req and req["nonce"] == self.nonce and req["review_message_id"] == interaction.message.id
              and _ctx["mod_check"](mod) and (req["discord_id"] != mod.id or config.MIS_REDES_SELF_APPROVE))
        reason = (self.reason.value or "").strip()
        if not ok or not transition(self.rid, ("pending",), "rejected", decided_by=mod.id, decided_by_tag=str(mod),
                                    decided_at=time.time(), reason=reason or None):
            await interaction.response.send_message("Esta solicitud ya no está pendiente.", ephemeral=True)
            return
        status = f"❌ Rechazada por {mod.mention}" + (f": {discord.utils.escape_markdown(reason)}" if reason else "")
        emb, _ = card(req, None, status, open_=False)
        await interaction.response.edit_message(embed=emb, view=None)
        log.info("mis_redes: request %s for %s rejected by %s", self.rid, req["discord_id"], mod)
        await _notify(interaction.client, req["discord_id"],
                      "❌ Un moderador de Club Ñ ha rechazado tu cambio de redes." + (f"\nMotivo: {reason}" if reason else "")
                      + "\nPuedes enviar otra solicitud con /mis_redes.")


def _sync_article_links(uid: int, name: str, changes: dict) -> None:
    """Keep the article bot's links in step (only if it already knows this creator). Never regenerates anything."""
    row = db.get(uid)
    if not row:
        return
    links = dict(row["links"])
    for p, v in changes.items():
        if v is None:
            links.pop(p, None)
        else:
            links[p] = url_of(p, v)
    if links != row["links"]:
        db.upsert(uid, links=links, manual_links=True, display_name=name)


def setup(client, mod_check, creator_role) -> None:
    """Called from main.py's setup_hook."""
    if not config.MIS_REDES_ENABLED:
        return
    init()
    _ctx.update(mod_check=mod_check, creator_role=creator_role)
    client.add_dynamic_items(ApproveRequest, RejectRequest)
    attempts = Attempts()
    locks: dict[int, asyncio.Lock] = collections.defaultdict(asyncio.Lock)

    @client.tree.command(name="mis_redes", description="Actualiza tus redes en la base de creadores del Club "
                                                       "(un moderador revisa el cambio)")
    @app_commands.guild_only()
    @app_commands.describe(twitch="Tu usuario o enlace de Twitch; escribe - para quitarlo",
                           youtube="Tu @canal o enlace de canal de YouTube; - para quitarlo",
                           instagram="Tu usuario o enlace de Instagram; - para quitarlo",
                           tiktok="Tu @usuario o enlace de TikTok; - para quitarlo")
    async def mis_redes(interaction: discord.Interaction,
                        twitch: Optional[app_commands.Range[str, 1, MAX_INPUT]] = None,
                        youtube: Optional[app_commands.Range[str, 1, MAX_INPUT]] = None,
                        instagram: Optional[app_commands.Range[str, 1, MAX_INPUT]] = None,
                        tiktok: Optional[app_commands.Range[str, 1, MAX_INPUT]] = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        async def say(text: str) -> None:
            await interaction.followup.send(text[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

        member = interaction.user
        guild = interaction.guild
        if (guild is None or (config.GUILD_ID and guild.id != config.GUILD_ID)
                or not isinstance(member, discord.Member)):
            await say("Este comando solo funciona en el servidor de Club Ñ.")
            return
        role = creator_role(guild)
        if role is None or role not in member.roles:
            await say("Este comando es solo para miembros con el rol de creador de contenido.")
            return
        if not attempts.hit(member.id):
            await say("Demasiados intentos seguidos. Espera unos minutos.")
            return
        uid = member.id
        given = {"twitch": twitch, "youtube": youtube, "instagram": instagram, "tiktok": tiktok}
        given = {p: v for p, v in given.items() if v is not None}
        cdb = live.shared_db()

        async with locks[uid]:
            try:
                await cdb.load()
            except Exception:
                log.exception("mis_redes: could not read the creator database")
                await say("No puedo leer la base de creadores ahora mismo. Inténtalo más tarde.")
                return
            rec = cdb.records.get(str(uid))
            now_vals = current_values(rec)
            pend = open_request(uid)
            if not given:
                txt = "**Tus redes en la base de creadores:**\n" + summary(rec)
                if pend:
                    txt += f"\n\nTienes una solicitud pendiente de revisión (#{pend['id']})."
                txt += "\n\nPara cambiarlas: `/mis_redes twitch:… youtube:… instagram:… tiktok:…` (usa `-` para quitar una)."
                await say(txt)
                return

            errors, parsed = [], {}
            for p, raw in given.items():
                try:
                    parsed[p] = parse(p, raw)
                except Invalid as e:
                    errors.append(f"• {LABEL[p]}: {e}")
            if not errors:
                async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                             headers={"User-Agent": "Mozilla/5.0"}) as c:
                    for p, v in list(parsed.items()):
                        try:
                            parsed[p] = await verify(p, v, c)
                        except Invalid as e:
                            errors.append(f"• {LABEL[p]}: {e}")
                        except (httpx.HTTPError, RuntimeError) as e:
                            log.warning("mis_redes: verifying %s failed: %s", p, e)
                            errors.append(f"• {LABEL[p]}: no he podido comprobarlo ahora; inténtalo más tarde")
            if errors:
                await say("No he enviado nada, revisa esto:\n" + "\n".join(errors))
                return
            changes = {p: v for p, v in parsed.items() if not same(p, now_vals[p], v)}
            if not changes:
                await say("Eso ya es lo que hay guardado; no hay nada que cambiar.\n" + summary(rec))
                return
            try:
                apply_changes(rec or {"discord_id": str(uid), "name": member.display_name}, changes)
            except Invalid as e:
                await say(f"No he enviado nada: {e}")
                return
            if pend and pend["status"] == "deciding":
                await say("Un moderador está revisando tu solicitud anterior ahora mismo. Espera a que termine.")
                return
            if why := limits_reason(uid):
                await say(why)
                return
            if pend and transition(pend["id"], ("pending",), "superseded", decided_at=time.time()):
                try:
                    old = await _review_channel(interaction.client).fetch_message(pend["review_message_id"])
                    await _close(old, pend, member, "↩️ Sustituida por una solicitud nueva del creador.")
                except (discord.HTTPException, RuntimeError, TypeError):
                    pass
            before = {p: now_vals[p] for p in changes}
            req = create_request(uid, str(member), changes, before)
            try:
                emb, view = card(req, member)
                msg = await _review_channel(interaction.client).send(embed=emb, view=view)
            except Exception:
                log.exception("mis_redes: could not post request %s", req["id"])
                transition(req["id"], ("pending",), "failed", decided_at=time.time())
                await say("No he podido enviar tu solicitud a moderación. Inténtalo más tarde.")
                return
            set_message(req["id"], msg.id)
        log.info("mis_redes: request %s from %s: %s", req["id"], member, changes)
        await say("📨 Enviado a moderación. Cuando un moderador lo apruebe se guardará en la base de creadores "
                  "(te aviso por mensaje privado si los tienes abiertos).\n\n**Cambios pedidos:**\n" +
                  "\n".join(f"• {LABEL[p]}: {_show(p, v) if v is not None else 'quitar'}" for p, v in changes.items()))


# ============================================================================ CLI

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Audit trail of /mis_redes requests.")
    ap.add_argument("--audit", action="store_true", help="list requests, newest first")
    ap.add_argument("--user", type=int, help="only this Discord id")
    ap.add_argument("--limit", type=int, default=50)
    a = ap.parse_args()
    init()
    for r in audit(a.user, a.limit):
        when = dt.datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M")
        dec = dt.datetime.fromtimestamp(r["decided_at"]).strftime("%Y-%m-%d %H:%M") if r["decided_at"] else ""
        print(f"#{r['id']} {when} {r['user_tag']} ({r['discord_id']}) {r['status']}"
              f"{' by ' + (r['decided_by_tag'] or '') if r['decided_by'] else ''} {dec}"
              f"{' reason: ' + r['reason'] if r['reason'] else ''}\n    {json.dumps(r['changes'], ensure_ascii=False)}")
