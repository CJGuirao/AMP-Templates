"""Question -> moderation -> retrieval -> grounded answer -> output check. No Discord code here."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .answer import NO_SOURCES, answer_question, guess_lang
from .config import Config
from .llm import LLM
from .moderation import Moderator
from .retrieval import Retriever

REFUSAL = {
    "es": "No puedo responder a esto en el servidor.",
    "en": "I can't answer this on the server.",
}


@dataclass
class Result:
    status: str  # "answered", "refused", "no_sources", "rate_limited"
    public_text: str = ""
    private_text: str = ""
    category: str = ""
    reason: str = ""
    sources: list = field(default_factory=list)


class Pipeline:
    def __init__(self, cfg: Config, llm: LLM | None = None, retriever: Retriever | None = None):
        self.cfg = cfg
        self.llm = llm or LLM(cfg.llm)
        rules_path = cfg.path(cfg.moderation.rules_file)
        rules = rules_path.read_text(encoding="utf-8") if rules_path.exists() else ""
        if cfg.moderation.rules_text:
            rules = (rules + "\n" + cfg.moderation.rules_text.replace("\\n", "\n")).strip()
        self.moderator = Moderator(cfg.moderation, self.llm, rules)
        self.retriever = retriever or Retriever(cfg.search, cfg.path(cfg.search.knowledge_dir))
        self.audit_path = cfg.path(cfg.audit_log)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)

    async def aclose(self):
        await self.llm.aclose()
        await self.retriever.aclose()

    def _audit(self, user_id: int, question: str, res: Result, started: float):
        rec = {
            "ts": int(time.time()),
            "user_id": str(user_id),
            "question": question,
            "status": res.status,
            "category": res.category,
            "reason": res.reason,
            "sources": [s.url for s in res.sources],
            "seconds": round(time.time() - started, 1),
        }
        with self.audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def admit(self, user_id: int, question: str) -> Result | None:
        """Instant checks, run before a question may wait for the CPU. Returns a Result if rejected."""
        lang = guess_lang(question)
        pre = self.moderator.precheck(question)
        if pre:
            res = Result("refused", private_text=f"{REFUSAL[lang]} ({pre.reason})",
                         category=pre.category, reason=pre.reason)
            self._audit(user_id, question, res, time.time())
            return res
        return None

    def charge(self, user_id: int) -> Result | None:
        """Count the question against the user's cooldown and daily limit."""
        limited = self.moderator.limiter.check(user_id)
        return Result("rate_limited", private_text=limited) if limited else None

    def refund(self, user_id: int) -> None:
        self.moderator.limiter.refund(user_id)

    async def handle(self, user_id: int, question: str, admitted: bool = False) -> Result:
        """Full pipeline. Pass admitted=True when admit() and charge() already ran."""
        started = time.time()
        lang = guess_lang(question)
        if not admitted:
            rejected = self.admit(user_id, question) or self.charge(user_id)
            if rejected:
                return rejected

        verdict = await self.moderator.evaluate(question, prechecked=True)
        if not verdict.allowed:
            res = Result(
                "refused",
                private_text=f"{REFUSAL[lang]} ({verdict.reason})" if verdict.reason else REFUSAL[lang],
                category=verdict.category,
                reason=verdict.reason,
            )
            self._audit(user_id, question, res, started)
            return res

        sources = await self.retriever.gather(question, verdict.search_queries)
        ans = await answer_question(self.llm, question, sources)
        if not ans.ok:
            res = Result("no_sources", public_text=NO_SOURCES[lang], category="ok")
        elif not await self.moderator.check_answer(question, ans.text):
            res = Result("refused", private_text=REFUSAL[lang], category="output_blocked",
                         reason="answer failed output check", sources=ans.sources)
        else:
            res = Result("answered", public_text=ans.text, category="ok", sources=ans.sources)
        self._audit(user_id, question, res, started)
        return res
