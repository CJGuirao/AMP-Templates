"""Thin client for the local model. Talks only to loopback unless explicitly allowed."""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from urllib.parse import urlparse

import logging

import httpx

from .config import LLMConfig

log = logging.getLogger("clubn_bot.llm")

_THINK_RE = re.compile(r"<think>.*?(</think>|$)", re.S)


class LLMError(RuntimeError):
    pass


class LLMTruncated(LLMError):
    """The reply hit the output token limit, so any JSON in it is cut off."""


def assert_loopback(url: str) -> None:
    host = urlparse(url).hostname or ""
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except socket.gaierror as e:
        raise LLMError(f"Cannot resolve LLM host {host!r}") from e
    for a in addrs:
        if not ipaddress.ip_address(a.split("%")[0]).is_loopback:
            raise LLMError(
                f"LLM endpoint {url} is not loopback ({a}). Set llm.allow_remote: true only if you really mean it."
            )


def strip_think(text: str) -> str:
    return _THINK_RE.sub("", text or "").strip()


def extract_json(text: str) -> dict:
    """Parse the first JSON object in a model reply (after removing reasoning)."""
    text = strip_think(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = esc = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    raise LLMError("Model reply contained no valid JSON object")


_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
}


def validate(value, schema: dict, path: str = "$") -> None:
    """Minimal JSON-schema check for the schemas this bot uses (type, required, properties, items, enum,
    maxItems). maxLength only steers generation; callers cut long strings themselves. Raises LLMError
    so a malformed verdict takes the same refusal path as no verdict."""
    t = schema.get("type")
    if t:
        py = _TYPES[t]
        ok = isinstance(value, py) and not (t in ("integer", "number") and isinstance(value, bool))
        if not ok:
            raise LLMError(f"{path}: expected {t}")
    if "enum" in schema and value not in schema["enum"]:
        raise LLMError(f"{path}: value not allowed")
    if t == "object":
        for key in schema.get("required", []):
            if key not in value:
                raise LLMError(f"{path}: missing {key}")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                validate(value[key], sub, f"{path}.{key}")
    if t == "array":
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise LLMError(f"{path}: too many items")
        for i, item in enumerate(value):
            validate(item, schema.get("items", {}), f"{path}[{i}]")


class LLM:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        if not cfg.allow_remote:
            assert_loopback(cfg.base_url)
        # trust_env=False: never route model traffic through a proxy from the environment
        self._client = httpx.AsyncClient(timeout=cfg.timeout_s, trust_env=False)
        self._no_think: set[str] = set()  # models that reject the think switch

    @property
    def models(self) -> list[str]:
        """Every model the bot uses: the answer model, then the rules-check model if it differs."""
        return list(dict.fromkeys(m for m in (self.cfg.model, self.cfg.check_model) if m))

    async def aclose(self):
        await self._client.aclose()

    async def ensure_model(self, log=print) -> None:
        """Ollama only: download the configured models on first start (progress goes to the AMP console)."""
        if self.cfg.backend != "ollama":
            return
        for model in self.models:
            await self._ensure_one(model, log)

    async def _ensure_one(self, model: str, log) -> None:
        base = self.cfg.base_url.rstrip("/")
        r = await self._client.post(base + "/api/show", json={"model": model})
        if r.status_code == 200:
            return
        log(f"Model {model} not found locally, downloading it (first start only)...")
        last = ""
        async with self._client.stream("POST", base + "/api/pull", json={"model": model},
                                       timeout=None) as resp:
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("error"):
                    raise LLMError(f"Model download failed: {ev['error']}")
                status = ev.get("status", "")
                if ev.get("total"):
                    pct = int(100 * ev.get("completed", 0) / ev["total"])
                    status = f"{status} {pct // 10 * 10}%"
                if status != last:
                    log(f"  {status}")
                    last = status
        log("Model download complete.")

    async def chat(
        self,
        messages: list[dict],
        *,
        schema: dict | None = None,
        max_tokens: int = 512,
        temperature: float = 0.1,
        model: str | None = None,
    ) -> str:
        text, truncated = await self._chat(messages, schema, max_tokens, temperature, model or self.cfg.model)
        if truncated and schema:
            raise LLMTruncated(f"reply hit the {max_tokens}-token limit")
        return text

    async def _chat(self, messages, schema, max_tokens, temperature, model) -> tuple[str, bool]:
        c = self.cfg
        # A repetition penalty punishes the quotes, commas and braces that JSON repeats on every field, which
        # pushes a small model to keep writing inside a string instead of closing it. Off for JSON replies.
        repeat_penalty = 1.0 if schema else 1.05
        if c.backend == "ollama":
            body = {
                "model": model,
                "messages": messages,
                "stream": False,
                "keep_alive": c.keep_alive,
                "options": {
                    "temperature": temperature,
                    "top_k": 50,
                    "repeat_penalty": repeat_penalty,
                    "num_ctx": c.num_ctx,
                    "num_predict": max_tokens,
                },
            }
            if c.num_thread > 0:
                body["options"]["num_thread"] = c.num_thread
            if schema:
                body["format"] = schema
            if c.think is not None and model not in self._no_think:
                body["think"] = c.think
            url = c.base_url.rstrip("/") + "/api/chat"
        elif c.backend == "openai":
            body = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "top_k": 50,
                "repeat_penalty": repeat_penalty,
            }
            if schema:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "reply", "schema": schema, "strict": True},
                }
            if c.think is not None:
                body["chat_template_kwargs"] = {"enable_thinking": c.think}
            url = c.base_url.rstrip("/") + "/v1/chat/completions"
        else:
            raise LLMError(f"Unknown backend {c.backend}")

        try:
            r = await self._client.post(url, json=body)
            if r.status_code == 400 and "think" in body and "think" in r.text.lower():
                # Ollama rejects the switch for models without a reasoning mode; they don't reason anyway.
                log.info("Model %s has no reasoning mode; not sending the Reasoning Mode switch", model)
                self._no_think.add(model)
                del body["think"]
                r = await self._client.post(url, json=body)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise LLMError(f"LLM request failed: {e}") from e

        if c.backend == "ollama":
            msg = data.get("message", {})
            text = msg.get("content", "")
            thinking = msg.get("thinking") or ""
            truncated = data.get("done_reason") == "length"
        else:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
            thinking = choice["message"].get("reasoning_content") or ""
            truncated = choice.get("finish_reason") == "length"
        if "<think>" in text and "</think>" not in text:
            thinking, text = text, ""
        if thinking:
            if c.think is False:
                log.warning("The model reasoned before replying (%d characters) although Reasoning Mode is Off; "
                            "this model or Ollama version ignores the switch.", len(thinking))
            log.debug("Model reasoning: %r", thinking[:2000])
        if truncated:
            log.warning("Model reply hit the %d-token limit (%d characters of reply)", max_tokens, len(text))
        return strip_think(text), truncated

    async def chat_json(self, messages: list[dict], schema: dict, *, max_tokens: int = 512, **kw) -> dict:
        """Ask for JSON matching `schema`. A reply cut off by the token limit is retried once with twice
        the budget; anything still unparseable or off-schema raises LLMError, which callers treat as a refusal."""
        for attempt in (1, 2):
            text = ""
            try:
                text = await self.chat(messages, schema=schema, max_tokens=max_tokens, **kw)
                data = extract_json(text)
                validate(data, schema)
                return data
            except LLMTruncated:
                if attempt == 2:
                    raise
                max_tokens *= 2
            except LLMError as e:
                if text:
                    log.debug("Unusable model reply (%s): %r", e, text[:2000])
                raise
        raise LLMError("unreachable")
