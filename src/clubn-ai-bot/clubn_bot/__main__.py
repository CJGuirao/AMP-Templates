"""Usage (all settings come from CLUBN_* environment variables, set by the AMP instance):
  python -m clubn_bot run              start the Discord bot
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


def main():
    ap = argparse.ArgumentParser(prog="clubn_bot")
    ap.add_argument("cmd", choices=["run", "ask", "check", "pull"])
    ap.add_argument("question", nargs="?")
    a = ap.parse_args()
    level = os.environ.get("CLUBN_LOG_LEVEL", "INFO").strip().upper()
    logging.basicConfig(level=logging.DEBUG if level == "DEBUG" else logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "discord", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING if noisy != "discord" else logging.INFO)
    cfg = load_config()

    if a.cmd == "ask":
        asyncio.run(_ask(cfg, a.question or ""))
    elif a.cmd == "check":
        asyncio.run(_check(cfg))
    elif a.cmd == "pull":
        asyncio.run(_pull(cfg))
    else:
        from .bot import ClubNBot

        token = load_discord_token()
        ClubNBot(cfg).run(token, log_handler=None)


if __name__ == "__main__":
    main()
