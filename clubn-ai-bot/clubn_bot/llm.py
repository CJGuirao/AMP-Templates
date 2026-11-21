"""Thin client for the local model. Talks only to loopback unless explicitly allowed."""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from urllib.parse import urlparse

import httpx

from .config import LLMConfig

_THINK_RE = re.compile(r"<think>.*?(</think>|$)", re.S)


class LLMError(RuntimeError):
    pass


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


class LLM:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        if not cfg.allow_remote:
            assert_loopback(cfg.base_url)
        # trust_env=False: never route model traffic through a proxy from the environment
        self._client = httpx.AsyncClient(timeout=cfg.timeout_s, trust_env=False)

    async def aclose(self):
        await self._client.aclose()

    async def ensure_model(self, log=print) -> None:
        """Ollama only: download the configured model on first start (progress goes to the AMP console)."""
        if self.cfg.backend != "ollama":
            return
        base = self.cfg.base_url.rstrip("/")
        r = await self._client.post(base + "/api/show", json={"model": self.cfg.model})
        if r.status_code == 200:
            return
        log(f"Model {self.cfg.model} not found locally, downloading it (first start only)...")
        last = ""
        async with self._client.stream("POST", base + "/api/pull", json={"model": self.cfg.model},
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
    ) -> str:
        c = self.cfg
        if c.backend == "ollama":
            body = {
                "model": c.model,
                "messages": messages,
                "stream": False,
                "keep_alive": c.keep_alive,
                "options": {
                    "temperature": temperature,
                    "top_k": 50,
                    "repeat_penalty": 1.1,
                    "num_ctx": c.num_ctx,
                    "num_predict": max_tokens,
                },
            }
            if c.num_thread > 0:
                body["options"]["num_thread"] = c.num_thread
            if schema:
                body["format"] = schema
            if c.think is not None:
                body["think"] = c.think
            url = c.base_url.rstrip("/") + "/api/chat"
        elif c.backend == "openai":
            body = {
                "model": c.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "top_k": 50,
                "repeat_penalty": 1.1,
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
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise LLMError(f"LLM request failed: {e}") from e

        if c.backend == "ollama":
            return strip_think(data.get("message", {}).get("content", ""))
        return strip_think(data["choices"][0]["message"].get("content") or "")

    async def chat_json(self, messages: list[dict], schema: dict, **kw) -> dict:
        return extract_json(await self.chat(messages, schema=schema, **kw))
