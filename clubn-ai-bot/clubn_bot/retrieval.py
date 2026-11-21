"""Finds evidence for an answer: the server's own knowledge files, Wikipedia, and web search.

Web pages are untrusted: they are fetched with an SSRF guard, size limits, and their text is only ever
given to the model as quoted data.
"""
from __future__ import annotations

import asyncio
import ipaddress
import math
import re
import socket
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from .config import SearchConfig

MAX_PAGE_BYTES = 1_500_000


@dataclass
class Source:
    title: str
    url: str
    text: str
    kind: str  # "local", "wikipedia", "web"


# ---------- text helpers ----------

_STOP = set(
    "el la los las un una unos unas de del al y o u en por para con sin que como cual cuál cuando donde "
    "es son ser fue era se lo le les mi tu su sus nos qué quien quién the a an of and or in on for to is are "
    "was were be what which who how when where why does do did it this that with from".split()
)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def tokens(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9ñ]+", _norm(s)) if len(t) > 2 and t not in _STOP]


def clip(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + " …"


def html_to_text(html: str, url: str = "") -> str:
    try:
        import trafilatura

        out = trafilatura.extract(html, url=url or None, include_comments=False, include_tables=False)
        if out:
            return out
    except Exception:
        pass
    html = re.sub(r"(?is)<(script|style|noscript|nav|footer|header)[^>]*>.*?</\1>", " ", html)
    return re.sub(r"<[^>]+>", " ", html)


# ---------- local knowledge base (server FAQ, rules, guides) ----------


class LocalKB:
    """Tiny BM25 index over Markdown/text files in knowledge/. Each heading starts a new chunk."""

    def __init__(self, folder: Path):
        self.chunks: list[Source] = []
        if folder.is_dir():
            for f in sorted(folder.rglob("*")):
                if f.suffix.lower() in {".md", ".txt"} and f.name.lower() != "readme.md":
                    self._add_file(f)
        self._tf = [Counter(tokens(c.title + " " + c.text)) for c in self.chunks]
        self._df = Counter(t for tf in self._tf for t in tf)
        self._avg = sum(sum(tf.values()) for tf in self._tf) / max(1, len(self._tf))

    def _add_file(self, f: Path):
        title, buf = f.stem, []
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("#"):
                if "".join(buf).strip():
                    self.chunks.append(Source(title, f"local:{f.name}", "\n".join(buf).strip(), "local"))
                title, buf = line.lstrip("# ").strip() or f.stem, []
            else:
                buf.append(line)
        if "".join(buf).strip():
            self.chunks.append(Source(title, f"local:{f.name}", "\n".join(buf).strip(), "local"))

    def search(self, query: str, k: int = 3, min_score: float = 0.5, min_match: float = 0.34) -> list[Source]:
        q = list(dict.fromkeys(tokens(query)))
        if not q or not self.chunks:
            return []
        n = len(self.chunks)
        scored = []
        for i, tf in enumerate(self._tf):
            dl = sum(tf.values())
            s, matched = 0.0, 0
            for t in q:
                if t in tf:
                    matched += 1
                    idf = math.log(1 + (n - self._df[t] + 0.5) / (self._df[t] + 0.5))
                    s += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * dl / self._avg))
            if s >= min_score and matched / len(q) >= min_match:
                scored.append((s, i))
        scored.sort(reverse=True)
        return [self.chunks[i] for _, i in scored[:k]]


# ---------- SSRF-safe fetching ----------


def is_public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            return False
    return bool(infos)


def url_allowed(url: str, blocked_domains: list[str]) -> bool:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    if u.port not in (None, 80, 443):
        return False
    host = u.hostname.lower()
    if any(host == d or host.endswith("." + d) for d in blocked_domains):
        return False
    return True


class Retriever:
    def __init__(self, cfg: SearchConfig, knowledge_dir: Path):
        self.cfg = cfg
        self.kb = LocalKB(knowledge_dir)
        self._client = httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=False,
            headers={"User-Agent": cfg.user_agent},
        )

    async def aclose(self):
        await self._client.aclose()

    async def fetch_text(self, url: str) -> str | None:
        for _ in range(4):  # follow at most 3 redirects, re-checking each hop
            if not url_allowed(url, self.cfg.blocked_domains):
                return None
            host = urlparse(url).hostname
            if not await asyncio.to_thread(is_public_host, host):
                return None
            try:
                async with self._client.stream("GET", url) as r:
                    if r.is_redirect:
                        url = urljoin(url, r.headers.get("location", ""))
                        continue
                    if r.status_code != 200:
                        return None
                    ctype = r.headers.get("content-type", "")
                    if "html" not in ctype and "text/plain" not in ctype:
                        return None
                    body = b""
                    async for chunk in r.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_PAGE_BYTES:
                            break
                    html = body.decode(r.encoding or "utf-8", errors="replace")
            except httpx.HTTPError:
                return None
            return await asyncio.to_thread(html_to_text, html, url)
        return None

    async def wikipedia(self, query: str, lang: str) -> list[Source]:
        api = f"https://{lang}.wikipedia.org/w/api.php"
        try:
            r = await self._client.get(
                api,
                params={"action": "query", "list": "search", "srsearch": query, "srlimit": 2, "format": "json"},
            )
            hits = r.json().get("query", {}).get("search", [])
            if not hits:
                return []
            titles = "|".join(h["title"] for h in hits)
            r = await self._client.get(
                api,
                params={
                    "action": "query",
                    "prop": "extracts|info",
                    "inprop": "url",
                    "explaintext": 1,
                    "exchars": self.cfg.max_chars_per_source,
                    "titles": titles,
                    "redirects": 1,
                    "format": "json",
                },
            )
            pages = r.json().get("query", {}).get("pages", {}).values()
        except (httpx.HTTPError, ValueError):
            return []
        return [
            Source(f"Wikipedia ({lang}): {p['title']}", p.get("fullurl", ""), p.get("extract", ""), "wikipedia")
            for p in pages
            if p.get("extract")
        ]

    async def web_search(self, query: str) -> list[dict]:
        n = self.cfg.max_web_results
        if self.cfg.web_search == "searxng":
            try:
                r = await self._client.get(
                    self.cfg.searxng_url.rstrip("/") + "/search", params={"q": query, "format": "json"}
                )
                return [
                    {"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
                    for x in r.json().get("results", [])[:n]
                ]
            except (httpx.HTTPError, ValueError):
                return []
        if self.cfg.web_search == "ddgs":
            def _run():
                from ddgs import DDGS

                return DDGS().text(query, max_results=n)

            try:
                res = await asyncio.to_thread(_run)
            except Exception:
                return []
            return [{"title": x.get("title", ""), "url": x.get("href", ""), "snippet": x.get("body", "")} for x in res]
        return []

    async def gather(self, question: str, queries: list[str]) -> list[Source]:
        cfg = self.cfg
        sources: list[Source] = list(self.kb.search(question + " " + " ".join(queries)))
        q0 = queries[0] if queries else question

        tasks = [self.wikipedia(q0, lang) for lang in cfg.wikipedia_langs]
        tasks += [self.web_search(q) for q in queries[:2]] if cfg.web_search != "none" else []
        results = await asyncio.gather(*tasks)
        wiki = [s for group in results[: len(cfg.wikipedia_langs)] for s in group]
        web_hits = [h for group in results[len(cfg.wikipedia_langs) :] for h in group]

        seen = {s.url for s in sources}
        web_hits = [
            h for h in web_hits
            if h["url"] and url_allowed(h["url"], cfg.blocked_domains)
            and "wikipedia.org" not in h["url"] and not (h["url"] in seen or seen.add(h["url"]))
        ]

        # read the top pages in full, keep the rest as search snippets
        full = await asyncio.gather(*(self.fetch_text(h["url"]) for h in web_hits[: cfg.fetch_pages]))
        web: list[Source] = []
        for i, h in enumerate(web_hits):
            text = (full[i] if i < len(full) else None) or h["snippet"]
            if text and len(text) > 40:
                web.append(Source(h["title"] or h["url"], h["url"], text, "web"))

        for s in wiki:
            if s.url not in seen:
                seen.add(s.url)
                sources.append(s)
        sources += web

        # rank non-local sources by overlap with the question, then enforce the size budget
        qt = set(tokens(question + " " + " ".join(queries)))
        local = [s for s in sources if s.kind == "local"]
        other = sorted(
            (s for s in sources if s.kind != "local"),
            key=lambda s: -len(qt & set(tokens(s.title + " " + s.text[:3000]))),
        )
        out, total = [], 0
        for s in local + other:
            s.text = clip(s.text, cfg.max_chars_per_source)
            if total + len(s.text) > cfg.max_total_chars or len(out) >= 6:
                break
            out.append(s)
            total += len(s.text)
        return out
