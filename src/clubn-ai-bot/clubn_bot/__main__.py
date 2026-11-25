"""Usage (all settings come from the AMP instance; blank ones may be filled from <CLUBN_DATA_DIR>/.env):
  python -m clubn_bot run              start the Discord bot (with the Club Ñ web features if
                                       CLUBN_WEB_ENABLED is on)
  python -m clubn_bot web TOOL ...     run a Club Ñ web tool: "web momentazos [--apply]",
                                       "web live [--apply]", "web mis_redes --audit"
  python -m clubn_bot ask "question"   run the full pipeline from the shell, without Discord
  python -m clubn_bot check            check the model endpoint and settings
  python -m clubn_bot pull             download the configured model (used by AMP Update)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from . import envfile
from .config import load_config, load_discord_token


def _say(msg: str):
    print(msg, flush=True)


async def _ask(cfg, question: str):
    from .pipeline import Pipeline

    p = Pipeline(cfg)
    try:
        res = await p.handle(0, question)
    finally:
        await p.aclose()
    _say(f"status: {res.status}  category: {res.category}  reason: {res.reason}")
    _say("-" * 60)
    _say(res.public_text or res.private_text)


async def _pull(cfg):
    import httpx

    from .llm import LLM, LLMError

    llm = LLM(cfg.llm)
    try:
        await llm.ensure_model(_say)
    except (LLMError, httpx.HTTPError) as e:
        raise SystemExit(f"Model download failed for {cfg.llm.model} at {cfg.llm.base_url}: {e or type(e).__name__}")
    finally:
        await llm.aclose()


async def _check(cfg):
    from .llm import LLM

    llm = LLM(cfg.llm)
    try:
        await llm.ensure_model(_say)
        out = await llm.chat([{"role": "user", "content": "Reply with the single word: OK"}], max_tokens=20)
        _say(f"Model {cfg.llm.model} at {cfg.llm.base_url} answered: {out!r}")
    finally:
        await llm.aclose()
    rules = cfg.path(cfg.moderation.rules_file)
    _say(f"Rules file: {rules} ({'found' if rules.exists() else 'missing'}); extra rules from AMP: "
         f"{'yes' if cfg.moderation.rules_text else 'no'}")
    _say(f"Knowledge folder: {cfg.path(cfg.search.knowledge_dir)}")


WEB_TOOLS = ("momentazos", "live", "mis_redes", "preview")


def _web_enabled() -> bool:
    return (os.environ.get("CLUBN_WEB_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on", "si", "sí")


def _web_tool(cfg, argv: list[str]):
    import runpy

    from .merged import prepare_env

    if not argv or argv[0] not in WEB_TOOLS:
        raise SystemExit(f"Usage: python -m clubn_bot web {{{'|'.join(WEB_TOOLS)}}} [options]")
    token = (os.environ.get("CLUBN_DISCORD_TOKEN") or os.environ.get("DISCORD_TOKEN") or "").strip()
    prepare_env(cfg, token)
    sys.argv = [f"clubn_web.{argv[0]}", *argv[1:]]
    runpy.run_module(f"clubn_web.{argv[0]}", run_name="__main__", alter_sys=True)


def main():
    ap = argparse.ArgumentParser(prog="clubn_bot")
    ap.add_argument("cmd", choices=["run", "ask", "check", "pull", "web"])
    ap.add_argument("question", nargs="?")
    a, rest = ap.parse_known_args()
    if rest and a.cmd != "web":
        ap.error(f"unrecognized arguments: {' '.join(rest)}")
    level = os.environ.get("CLUBN_LOG_LEVEL", "INFO").strip().upper()
    logging.basicConfig(level=logging.DEBUG if level == "DEBUG" else logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "discord", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING if noisy != "discord" else logging.INFO)
    loaded = envfile.load()
    if loaded:
        logging.getLogger("clubn_bot").info("Read %d blank setting(s) from %s", len(loaded), envfile.default_path())
    cfg = load_config()

    if a.cmd == "web":
        _web_tool(cfg, ([a.question] if a.question else []) + rest)
    elif a.cmd == "ask":
        asyncio.run(_ask(cfg, a.question or ""))
    elif a.cmd == "check":
        asyncio.run(_check(cfg))
    elif a.cmd == "pull":
        asyncio.run(_pull(cfg))
    elif _web_enabled():
        from . import merged

        merged.run(cfg, load_discord_token())
    else:
        from .bot import ClubNBot

        token = load_discord_token()
        ClubNBot(cfg).run(token, log_handler=None)


if __name__ == "__main__":
    main()
