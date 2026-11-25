"""Fulgencio + the Club Ñ web features (formerly SuperAntonia) in one process and one Discord connection.

clubn_web is a copy of the SuperAntonia bot package, kept unchanged so it can be refreshed from its
source folder (tools/sync_clubn_web.py). Everything that adapts it to Fulgencio happens here:

- one Discord client: clubn_web.main's client, with privileged intents (members, message content);
- Fulgencio's question command added to it, replacing the /fulgencio placeholder SuperAntonia kept
  for "the other program" (that program is now this one);
- the AI chain Claude -> DeepSeek -> Fulgencio's local model for article and event drafts;
- the local model is checked/downloaded in the background, so the web features start straight away;
- "ClubN bot ready" is still printed on ready, which is what AMP waits for;
- optionally (SOCIAL_ENABLED, off by default) the social posts workflow in social_posts.py.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import os
from pathlib import Path

from .config import Config

log = logging.getLogger("clubn_bot.merged")

REQUIRED_WEB_VARS = ("WP_USER", "WP_APP_PASSWORD")


def prepare_env(cfg: Config, token: str, env: dict | None = None) -> None:
    """Fill the variables clubn_web.config reads at import time."""
    from .web_settings import VARIABLES

    env = os.environ if env is None else env
    # AMP passes "" for blank fields; clubn_web reads some settings with os.getenv(name, default),
    # which would take "" instead of the default, so blank ones are removed.
    for k in VARIABLES:
        if k in env and not (env.get(k) or "").strip():
            del env[k]
    missing = [k for k in REQUIRED_WEB_VARS if not (env.get(k) or "").strip()]
    if missing:
        raise SystemExit(f"Club Ñ web features are on but {', '.join(missing)} is empty: set it in AMP "
                         f"(Configuration > Club Ñ Web > WordPress) or in {cfg.data_dir / '.env'}.")
    env["DISCORD_TOKEN"] = token  # read once by clubn_web.config, removed again in build()
    if not (env.get("DB_PATH") or "").strip():
        env["DB_PATH"] = str(cfg.data_dir / "data" / "clubn_web.db")
    if not (env.get("GUILD_ID") or "").strip() and cfg.discord.guild_id:
        env["GUILD_ID"] = str(cfg.discord.guild_id)
    if not (env.get("TIMEZONE") or "").strip() and (env.get("TZ") or "").strip():
        env["TIMEZONE"] = env["TZ"]


def build(cfg: Config, token: str, *, local_llm=None, pipeline=None):
    """Import the web bot, attach Fulgencio and the AI chain. Returns (client, fulgencio, chain)."""
    from .ai_chain import Chain
    from .bot import Fulgencio
    from .llm import LLM

    prepare_env(cfg, token)
    try:
        web = importlib.import_module("clubn_web.main")
        writer = importlib.import_module("clubn_web.writer")
    finally:
        os.environ.pop("DISCORD_TOKEN", None)  # nothing we start later needs it in its environment
    client = web.bot

    fulgencio = Fulgencio(client, cfg, pipeline=pipeline)
    tree = client.tree
    name = cfg.discord.command_name
    for placeholder in {"fulgencio", name}:
        tree.remove_command(placeholder)
    tree.add_command(fulgencio.make_command())

    chain = Chain(writer, local_llm=local_llm if local_llm is not None else LLM(cfg.llm),
                  cpu_lock=fulgencio.worker._sem)
    writer.structured = chain.structured  # write_article() and events.py look it up at call time

    from .social_networks import Settings as SocialSettings

    social = None
    social_cfg = SocialSettings.from_env()
    if social_cfg.enabled:
        from . import social_posts

        web_config = importlib.import_module("clubn_web.config")
        social = social_posts.setup(client, structured=chain.structured, is_mod=web.is_mod, wp=client.wp,
                                    db_path=web_config.DB_PATH, default_review_channel=web_config.REVIEW_CHANNEL_ID,
                                    settings=social_cfg)
        log.info("Social posts on (%s); networks: %s", "test mode" if social_cfg.dry_run else "LIVE",
                 ", ".join(social_cfg.networks))
    client._clubn_social = social

    web_setup_hook = client.setup_hook
    web_on_message = getattr(client, "on_message", None)
    web_on_ready = getattr(client, "on_ready", None)
    web_close = client.close

    async def setup_hook():
        client._clubn_warm_task = asyncio.get_running_loop().create_task(_warm_model(fulgencio))
        if social is not None:
            from . import social_posts

            await social_posts.start(social)
        await web_setup_hook()  # registers everything and syncs the command list once

    async def on_message(message):
        if social is not None and await social.on_message(message):  # a mod replying to a social card
            return
        if web_on_message is not None:
            await web_on_message(message)
        await fulgencio.on_message(message)

    async def on_ready():
        if web_on_ready is not None:
            await web_on_ready()
        log.info("ClubN bot ready: /%s and the Club Ñ web features are available in %d server(s)",
                 name, len(client.guilds))
        fulgencio._check_mod_log_channel()

    async def close():
        try:
            await fulgencio.aclose()
            if social is not None:
                if social._task is not None:
                    social._task.cancel()
                await social.router.aclose()
            if chain.local is not None and chain.local is not fulgencio.pipeline.llm:
                await chain.local.aclose()
        finally:
            await web_close()

    client.setup_hook = setup_hook
    client.on_message = on_message
    client.on_ready = on_ready
    client.close = close
    return client, fulgencio, chain


async def _warm_model(fulgencio) -> None:
    try:
        await fulgencio.wait_for_model()
        log.info("Local model ready")
    except Exception:
        log.exception("Local model not available; questions will fail until it is, the web features keep running")


def run(cfg: Config, token: str) -> None:
    from .pipeline import Pipeline

    Path(cfg.data_dir / "data").mkdir(parents=True, exist_ok=True)
    pipeline = Pipeline(cfg)  # its model client is shared with the AI chain's local step
    client, _, _ = build(cfg, token, local_llm=pipeline.llm, pipeline=pipeline)
    client.run(token, log_handler=None)
