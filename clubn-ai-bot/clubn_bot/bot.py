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
    """One question at a time on the CPU; a short waiting line, then 'busy'."""

    def __init__(self, pipeline: Pipeline, queue_size: int):
        self.pipeline = pipeline
        self._sem = asyncio.Semaphore(1)
        self._waiting = 0
        self._max = queue_size

    async def run(self, user_id: int, question: str) -> Result:
        if self._waiting >= self._max:
            raise Busy()
        self._waiting += 1
        try:
            async with self._sem:
                return await self.pipeline.handle(user_id, question)
        finally:
            self._waiting -= 1


class ClubNBot(discord.Client):
    def __init__(self, cfg: Config):
        intents = discord.Intents.none()
        intents.guilds = True
        if cfg.discord.answer_on_mention:
            intents.guild_messages = True
            intents.message_content = True  # privileged: enable it in the Developer Portal too
        super().__init__(intents=intents, allowed_mentions=NO_PINGS)
        self.cfg = cfg
        self.tree = app_commands.CommandTree(self)
        self.pipeline = Pipeline(cfg)
        self.worker = Worker(self.pipeline, cfg.moderation.queue_size)
        self._register_command()

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
        cid = self.cfg.moderation.mod_log_channel_id
        if not cid or res.status != "refused":
            return
        ch = self.get_channel(cid)
        if ch:
            q = question.replace("`", "'")[:500]
            await ch.send(
                f"🚫 Pregunta rechazada de {user.mention} (`{res.category}`): {res.reason}\n> {q}",
                allowed_mentions=NO_PINGS,
            )

    def _public(self, user, question: str, res: Result) -> str:
        q = question.replace("\n", " ")[:300]
        return f"**{user.display_name} preguntó:** {discord.utils.escape_markdown(q)}\n\n{res.public_text}"[:2000]

    # ----- slash command -----
    def _register_command(self):
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
                msg = await interaction.channel.send(self._public(interaction.user, pregunta, res),
                                                     allowed_mentions=NO_PINGS, suppress_embeds=True)
                await interaction.followup.send(f"Respuesta publicada: {msg.jump_url}", ephemeral=True)
            else:
                await interaction.followup.send(res.private_text, ephemeral=True)
            await self._mod_log(interaction.user, pregunta, res)

        self.tree.add_command(ask)

    async def setup_hook(self):
        # wait for the local model server (started alongside us), then make sure the model is present
        for attempt in range(60):
            try:
                await self.pipeline.llm.ensure_model(log.info)
                break
            except Exception as e:  # server still starting
                if attempt == 59:
                    raise
                log.info("Waiting for the model server (%s)...", e.__class__.__name__)
                await asyncio.sleep(2)
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

    # ----- optional: answer when mentioned -----
    async def on_message(self, message: discord.Message):
        if not self.cfg.discord.answer_on_mention or message.author.bot or not message.guild:
            return
        if self.user not in message.mentions:
            return
        if self._allowed_here(message.channel.id, message.author):
            return
        question = message.content.replace(f"<@{self.user.id}>", "").replace(f"<@!{self.user.id}>", "").strip()
        async with message.channel.typing():
            try:
                res = await self.worker.run(message.author.id, question)
            except Busy:
                await message.reply("Estoy ocupado, prueba en un minuto.", allowed_mentions=NO_PINGS)
                return
        if res.public_text:
            await message.reply(res.public_text[:2000], allowed_mentions=NO_PINGS, suppress_embeds=True)
        else:
            await message.add_reaction("🚫")  # no public detail on why
            try:
                await message.author.send(res.private_text)
            except discord.HTTPException:
                pass
        await self._mod_log(message.author, question, res)

    async def close(self):
        await self.pipeline.aclose()
        await super().close()
