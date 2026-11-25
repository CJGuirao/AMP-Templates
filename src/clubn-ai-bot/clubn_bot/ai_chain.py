"""AI fallback chain for the Club Ñ web features: Claude -> DeepSeek -> Fulgencio's local model.

clubn_web.writer.structured() (used by the creator articles and the events workflow) tries Claude and
then DeepSeek. In the merged bot it is replaced by Chain.structured, which adds the local model as the
last resort, so drafts still get written when both paid APIs fail or have no credits. Every draft
still waits for a moderator's ✅, whichever model wrote it.

A provider that answers "no credits" or "bad key" is skipped for a while (PAUSE_S) instead of being
retried on every request; other errors (timeouts, 5xx, refusals) only skip it for that one request.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from .llm import LLM, LLMError, extract_json

log = logging.getLogger("clubn_bot.ai_chain")
M = TypeVar("M", bound=BaseModel)

PAUSE_S = 6 * 3600

LOCAL_SYSTEM_SUFFIX = (
    "\n\nResponde SOLO con un objeto JSON válido que cumpla este esquema (sin texto antes ni después):\n"
)


class NoProvider(RuntimeError):
    pass


def _is_credit_or_auth(exc: Exception) -> bool:
    """Errors that won't fix themselves in the next minute: no credits, bad or revoked key."""
    name = type(exc).__name__
    if name in ("AuthenticationError", "PermissionDeniedError"):
        return True
    text = str(exc).lower()
    if name == "BadRequestError" and ("credit" in text or "billing" in text):
        return True
    # writer._write_deepseek raises RuntimeError("DeepSeek rejected the request (401|402): ...")
    return "(401)" in text or "(402)" in text or "insufficient balance" in text


class Chain:
    def __init__(self, writer_module, local_llm: LLM | None = None,
                 cpu_lock: asyncio.Semaphore | None = None, local_max_tokens: int = 3000,
                 clock: Callable[[], float] = time.time):
        self.w = writer_module           # clubn_web.writer: its config, _write_claude and _write_deepseek
        self.local = local_llm
        self.cpu_lock = cpu_lock or asyncio.Semaphore(1)  # shared with Fulgencio's question worker
        self.local_max_tokens = local_max_tokens
        self.clock = clock
        self.paused_until: dict[str, float] = {}
        self.last_provider: str | None = None

    # ----- providers -----
    async def _claude(self, system: str, prompt: str, schema: type[M]) -> M:
        import anthropic

        cfg = self.w.config
        if not cfg.ANTHROPIC_API_KEY:
            raise NoProvider("no ANTHROPIC_API_KEY")
        self.w._claude = self.w._claude or anthropic.AsyncAnthropic(api_key=cfg.ANTHROPIC_API_KEY)
        return await self.w._write_claude(system, prompt, schema)

    async def _deepseek(self, system: str, prompt: str, schema: type[M]) -> M:
        if not self.w.config.DEEPSEEK_API_KEY:
            raise NoProvider("no DEEPSEEK_API_KEY")
        return await self.w._write_deepseek(system, prompt, schema)

    async def _local(self, system: str, prompt: str, schema: type[M]) -> M:
        if self.local is None:
            raise NoProvider("no local model")
        schema_json = schema.model_json_schema()
        messages = [
            {"role": "system", "content": system + LOCAL_SYSTEM_SUFFIX + json.dumps(schema_json, ensure_ascii=False)},
            {"role": "user", "content": prompt},
        ]
        last: Exception | None = None
        for _ in range(2):  # small models sometimes break the JSON once
            async with self.cpu_lock:
                text = await self.local.chat(messages, schema=schema_json, max_tokens=self.local_max_tokens,
                                             temperature=0.3)
            try:
                return schema.model_validate(extract_json(text))
            except (LLMError, ValidationError) as e:
                last = e
                log.warning("local model returned invalid JSON (%s); retrying once", type(e).__name__)
        raise LLMError(f"local model gave no valid answer: {last}")

    # ----- the chain -----
    async def structured(self, system: str, prompt: str, schema: type[M]) -> M:
        errors: list[str] = []
        steps: list[tuple[str, Callable[..., Awaitable[M]]]] = [
            ("claude", self._claude), ("deepseek", self._deepseek), ("local", self._local)]
        for name, fn in steps:
            if self.clock() < self.paused_until.get(name, 0):
                errors.append(f"{name}: paused")
                continue
            try:
                out = await fn(system, prompt, schema)
            except NoProvider as e:
                errors.append(f"{name}: {e}")
                continue
            except Exception as e:  # noqa: BLE001 - any failure moves on to the next provider
                if name != "local" and _is_credit_or_auth(e):
                    self.paused_until[name] = self.clock() + PAUSE_S
                    log.warning("%s unavailable (%s: no credits or bad key); skipping it for %dh",
                                name, type(e).__name__, PAUSE_S // 3600)
                else:
                    log.warning("%s failed (%s); trying the next model", name, type(e).__name__)
                errors.append(f"{name}: {type(e).__name__}: {str(e)[:150]}")
                continue
            if name != "claude":
                log.warning("draft written by the %s model (fallback)", name)
            self.last_provider = name
            return out
        raise RuntimeError("No AI model could write this draft. " + " | ".join(errors))
