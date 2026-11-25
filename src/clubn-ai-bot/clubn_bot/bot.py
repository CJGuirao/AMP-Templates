"""Discord front end. Answers are posted publicly; refusals are shown only to the asker."""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from .config import Config
from .pipeline import Pipeline, Result

log = logging.getLogger("clubn_bot")
NO_PINGS = discord.AllowedMentions.none()


class Busy(Exception):
    pass


class Worker:
    """One question at a time on the CPU, plus a short waiting line.

    Instant checks (rules prechecks, cooldown, daily limit) run before a question joins the line,
    so one member can't fill it with questions that would be rejected anyway.
    """

    def __init__(self, pipeline: Pipeline, queue_size: int):
        self.pipeline = pipeline
        self._sem = asyncio.Semaphore(1)
        self._running = False
        self._waiting = 0  # questions waiting for the CPU, not counting the one running
        self._max = queue_size

    async def run(self, user_id: int, question: str) -> Result:
        rejected = self.pipeline.admit(user_id, question)
        if rejected:
            return rejected
        rejected = self.pipeline.charge(user_id)
        if rejected:
            return rejected
        if self._running and self._waiting >= self._max:
            self.pipeline.refund(user_id)  # "busy" shouldn't cost the member their cooldown
            raise Busy()
        self._waiting += 1
        try:
            await self._sem.acquire()
        finally:
            self._waiting -= 1  # left the line, whether we got the CPU or were cancelled
        self._running = True
        try:
            return await self.pipeline.handle(user_id, question, admitted=True)
        finally:
            self._running = False
            self._sem.release()


class Fulgencio:
    """The question-answering feature, attachable to any discord.Client.

    ClubNBot below runs it on its own; clubn_bot.merged attaches it to the Club Ñ web bot so both run
    in one process on one Discord connection.
    """

    def __init__(self, client: discord.Client, cfg: Config, pipeline: Pipeline | None = None):
        self.client = client
        self.cfg = cfg
        self.pipeline = pipeline or Pipeline(cfg)
        self.worker = Worker(self.pipeline, cfg.moderation.queue_size)

    # ----- permissions -----
    def _allowed_here(self, channel_id: int, member) -> str | None:
        d = self.cfg.discord
        if d.allowed_channel_ids and channel_id not in d.allowed_channel_ids:
            return "Este bot solo responde en los canales habilitados."
        if d.allowed_role_ids:
            roles = {r.id for r in getattr(member, "roles", [])}
            if not roles & set(d.allowed_role_ids):
                return "No tienes un rol que pueda usar el bot."
        return None

    async def _mod_log(self, user, question: str, res: Result):
        """Best effort: a missing or unreachable log channel never breaks the answer to the member."""
        cid = self.cfg.moderation.mod_log_channel_id
        if not cid or res.status != "refused":
            return
        ch = self.client.get_channel(cid)
        if ch is None:
            log.warning("Moderation log channel %s not found or not visible to the bot; refusal not logged", cid)
            return
        q = question.replace("`", "'")[:500]
        try:
            await ch.send(
                f"🚫 Pregunta rechazada de {user.mention} (`{res.category}`): {res.reason}\n> {q}",
                allowed_mentions=NO_PINGS,
            )
        except discord.HTTPException as e:
            log.warning("Could not write to moderation log channel %s (%s); refusal not logged. "
                        "Give the bot View Channel and Send Messages there.", cid, e)

    def _check_mod_log_channel(self):
        """Warn at startup if the moderation log channel is set but unusable."""
        cid = self.cfg.moderation.mod_log_channel_id
        if not cid:
            return
        ch = self.client.get_channel(cid)
        if ch is None:
            log.warning("Moderation log channel %s not found: check the ID and that the bot can see it", cid)
            return
        me = getattr(ch.guild, "me", None)
        perms = ch.permissions_for(me) if me else None
        if perms is not None and not (perms.view_channel and perms.send_messages):
            log.warning("The bot lacks View Channel or Send Messages in moderation log channel #%s (%s)", ch.name, cid)
        else:
            log.info("Moderation log channel: #%s", ch.name)

    def _public(self, user, question: str, res: Result) -> str:
        """'<name> preguntó: <question>' + answer, shortening only the quoted question to fit 2000."""
        name = discord.utils.escape_markdown(user.display_name[:32])
        q = question.replace("\n", " ")[:300]
        while True:
            text = f"**{name} preguntó:** {discord.utils.escape_markdown(q)}\n\n{res.public_text}"
            if len(text) <= 2000 or not q:
                return text[:2000] if not q else text
            q = q[: max(0, len(q) - (len(text) - 2000) - 1)].rstrip()
            q = (q + "…") if q else ""

    # ----- slash command -----
    def make_command(self) -> app_commands.Command:
        @app_commands.command(name=self.cfg.discord.command_name, description="Haz una pregunta al bot de ClubÑ")
        @app_commands.describe(pregunta="Tu pregunta o petición")
        @app_commands.guild_only()
        async def ask(interaction: discord.Interaction, pregunta: str):
            blocked = self._allowed_here(interaction.channel_id, interaction.user)
            if blocked:
                await interaction.response.send_message(blocked, ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                res = await self.worker.run(interaction.user.id, pregunta)
            except Busy:
                await interaction.followup.send("Estoy ocupado con otras preguntas, prueba en un minuto.", ephemeral=True)
                return
            except Exception:
                log.exception("pipeline failed")
                await interaction.followup.send("Ha ocurrido un error interno.", ephemeral=True)
                return

            if res.public_text:
                try:
                    msg = await interaction.channel.send(self._public(interaction.user, pregunta, res),
                                                         allowed_mentions=NO_PINGS, suppress_embeds=True)
                except discord.HTTPException as e:
                    log.warning("Could not post the answer in channel %s (%s)", interaction.channel_id, e)
                    await interaction.followup.send(
                        "No tengo permiso para publicar en este canal. Avisa a la administración.", ephemeral=True)
                else:
                    await interaction.followup.send(f"Respuesta publicada: {msg.jump_url}", ephemeral=True)
            else:
                await interaction.followup.send(res.private_text, ephemeral=True)
            await self._mod_log(interaction.user, pregunta, res)

        return ask

    async def wait_for_model(self):
        """Wait for the local model server (started alongside us), then make sure the model is present."""
        for attempt in range(60):
            try:
                await self.pipeline.llm.ensure_model(log.info)
                return
            except Exception as e:  # server still starting
                if attempt == 59:
                    raise
                log.info("Waiting for the model server (%s)...", e.__class__.__name__)
                await asyncio.sleep(2)

    # ----- optional: answer when mentioned -----
    async def on_message(self, message: discord.Message):
        me = self.client.user
        if not self.cfg.discord.answer_on_mention or message.author.bot or not message.guild or me is None:
            return
        if me not in message.mentions:
            return
        if self._allowed_here(message.channel.id, message.author):
            return
        question = message.content.replace(f"<@{me.id}>", "").replace(f"<@!{me.id}>", "").strip()
        async with message.channel.typing():
            try:
                res = await self.worker.run(message.author.id, question)
            except Busy:
                await message.reply("Estoy ocupado, prueba en un minuto.", allowed_mentions=NO_PINGS)
                return
        try:
            if res.public_text:
                await message.reply(res.public_text[:2000], allowed_mentions=NO_PINGS, suppress_embeds=True)
            else:
                await message.add_reaction("🚫")  # no public detail on why
                try:
                    await message.author.send(res.private_text)
                except discord.HTTPException:
                    pass
        except discord.HTTPException as e:
            log.warning("Could not reply in channel %s (%s)", message.channel.id, e)
        await self._mod_log(message.author, question, res)

    async def aclose(self):
        await self.pipeline.aclose()


class ClubNBot(discord.Client):
    """Fulgencio on its own (Club Ñ Web Features off): only the question command."""

    def __init__(self, cfg: Config):
        intents = discord.Intents.none()
        intents.guilds = True
        if cfg.discord.answer_on_mention:
            intents.guild_messages = True
            intents.message_content = True  # privileged: enable it in the Developer Portal too
        super().__init__(intents=intents, allowed_mentions=NO_PINGS)
        self.cfg = cfg
        self.tree = app_commands.CommandTree(self)
        self.fulgencio = Fulgencio(self, cfg)
        self.pipeline = self.fulgencio.pipeline
        self.worker = self.fulgencio.worker
        self.tree.add_command(self.fulgencio.make_command())

    async def setup_hook(self):
        await self.fulgencio.wait_for_model()
        gid = self.cfg.discord.guild_id
        if gid:
            guild = discord.Object(id=gid)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        log.info("ClubN bot ready: /%s is available in %d server(s)", self.cfg.discord.command_name, len(self.guilds))
        self.fulgencio._check_mod_log_channel()

    async def on_message(self, message: discord.Message):
        await self.fulgencio.on_message(message)

    async def close(self):
        await self.fulgencio.aclose()
        await super().close()
