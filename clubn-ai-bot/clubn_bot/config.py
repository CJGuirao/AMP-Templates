"""Configuration comes only from environment variables, which the AMP instance settings fill in.

Nothing is hard-coded: every value below has a default that AMP can override, and an empty
variable (AMP passes "" for blank fields) means "use the default".
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class LLMConfig:
    backend: str = "ollama"  # "ollama" (native API, supports keep_alive) or "openai" (llama-server)
    base_url: str = "http://127.0.0.1:11434"
    model: str = "hf.co/LiquidAI/LFM3-1.7B-GGUF:Q4_0"
    keep_alive: str = "5m"  # how long the model stays in RAM after the last question
    num_ctx: int = 8192  # context actually allocated; LFM3 allows 256K but RAM grows with it
    timeout_s: float = 300.0
    num_thread: int = 0  # CPU threads for inference; 0 = let the server decide
    think: bool | None = False  # LFM3 reasons by default; off keeps CPU answers fast
    allow_remote: bool = False  # refuse non-loopback model endpoints unless explicitly allowed


@dataclass
class SearchConfig:
    knowledge_dir: str = "knowledge"
    wikipedia_langs: list[str] = field(default_factory=lambda: ["es", "en"])
    web_search: str = "ddgs"  # "ddgs", "searxng" or "none"
    searxng_url: str = ""
    max_web_results: int = 5
    fetch_pages: int = 2  # top web results downloaded and read in full
    max_chars_per_source: int = 1800
    max_total_chars: int = 9000
    blocked_domains: list[str] = field(default_factory=list)
    user_agent: str = "ClubN-AnswerBot/1.0 (self-hosted Discord bot)"


@dataclass
class ModerationConfig:
    rules_file: str = "rules.md"
    rules_text: str = ""  # extra rules typed into AMP, added to the rules file
    max_question_chars: int = 600
    per_user_cooldown_s: int = 60
    per_user_daily_limit: int = 30
    queue_size: int = 5
    blocked_patterns: list[str] = field(default_factory=list)
    check_output: bool = True  # second model pass over the final answer
    mod_log_channel_id: int | None = None


@dataclass
class DiscordConfig:
    command_name: str = "pregunta"
    allowed_channel_ids: list[int] = field(default_factory=list)  # empty = any channel
    allowed_role_ids: list[int] = field(default_factory=list)  # empty = everyone
    answer_on_mention: bool = False  # needs the privileged Message Content intent
    guild_id: int | None = None  # set to register the slash command instantly on one server


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    moderation: ModerationConfig = field(default_factory=ModerationConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    data_dir: Path = Path(".")
    audit_log: str = "logs/audit.jsonl"

    def path(self, p: str) -> Path:
        pp = Path(p)
        return pp if pp.is_absolute() else self.data_dir / pp


# env var -> (section, attribute)
ENV_MAP: dict[str, tuple[str, str]] = {
    "CLUBN_LLM_BACKEND": ("llm", "backend"),
    "CLUBN_LLM_URL": ("llm", "base_url"),
    "CLUBN_MODEL": ("llm", "model"),
    "CLUBN_KEEP_ALIVE": ("llm", "keep_alive"),
    "CLUBN_NUM_CTX": ("llm", "num_ctx"),
    "CLUBN_LLM_TIMEOUT": ("llm", "timeout_s"),
    "CLUBN_NUM_THREADS": ("llm", "num_thread"),
    "CLUBN_THINK": ("llm", "think"),
    "CLUBN_LLM_ALLOW_REMOTE": ("llm", "allow_remote"),
    "CLUBN_KNOWLEDGE_DIR": ("search", "knowledge_dir"),
    "CLUBN_WIKIPEDIA_LANGS": ("search", "wikipedia_langs"),
    "CLUBN_WEB_SEARCH": ("search", "web_search"),
    "CLUBN_SEARXNG_URL": ("search", "searxng_url"),
    "CLUBN_MAX_WEB_RESULTS": ("search", "max_web_results"),
    "CLUBN_FETCH_PAGES": ("search", "fetch_pages"),
    "CLUBN_BLOCKED_DOMAINS": ("search", "blocked_domains"),
    "CLUBN_RULES_FILE": ("moderation", "rules_file"),
    "CLUBN_RULES_TEXT": ("moderation", "rules_text"),
    "CLUBN_MAX_QUESTION_CHARS": ("moderation", "max_question_chars"),
    "CLUBN_COOLDOWN_S": ("moderation", "per_user_cooldown_s"),
    "CLUBN_DAILY_LIMIT": ("moderation", "per_user_daily_limit"),
    "CLUBN_QUEUE_SIZE": ("moderation", "queue_size"),
    "CLUBN_BLOCKED_PATTERNS": ("moderation", "blocked_patterns"),
    "CLUBN_CHECK_OUTPUT": ("moderation", "check_output"),
    "CLUBN_MOD_LOG_CHANNEL_ID": ("moderation", "mod_log_channel_id"),
    "CLUBN_COMMAND_NAME": ("discord", "command_name"),
    "CLUBN_ALLOWED_CHANNEL_IDS": ("discord", "allowed_channel_ids"),
    "CLUBN_ALLOWED_ROLE_IDS": ("discord", "allowed_role_ids"),
    "CLUBN_ANSWER_ON_MENTION": ("discord", "answer_on_mention"),
    "CLUBN_GUILD_ID": ("discord", "guild_id"),
}

_TRUE = {"1", "true", "yes", "on", "si", "sí"}
_FALSE = {"0", "false", "no", "off"}


def _split(raw: str, sep: str = ",") -> list[str]:
    return [x.strip() for x in raw.split(sep) if x.strip()]


def _coerce(raw: str, default, attr: str):
    if attr in ("think",):  # tri-state: true / false / auto
        low = raw.lower()
        return None if low in ("auto", "default") else low in _TRUE
    if isinstance(default, bool):
        low = raw.lower()
        if low not in _TRUE | _FALSE:
            raise ValueError(f"expected true/false, got {raw!r}")
        return low in _TRUE
    if isinstance(default, int) and not isinstance(default, bool):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, list):
        if attr == "blocked_patterns":  # regexes may contain commas
            return _split(raw, "\n") if "\n" in raw else _split(raw, ";;")
        items = _split(raw)
        return [int(x) for x in items] if attr.endswith("_ids") else items
    if attr.endswith("_id"):
        return int(raw)
    return raw


def load_config(env: dict | None = None) -> Config:
    env = os.environ if env is None else env
    cfg = Config(data_dir=Path(env.get("CLUBN_DATA_DIR") or ".").resolve())
    defaults = {s: {f.name: getattr(getattr(cfg, s), f.name) for f in fields(getattr(cfg, s))}
                for s in ("llm", "search", "moderation", "discord")}
    for var, (section, attr) in ENV_MAP.items():
        raw = (env.get(var) or "").strip()
        if not raw:
            continue
        try:
            value = _coerce(raw, defaults[section][attr], attr)
        except ValueError as e:
            raise SystemExit(f"Invalid value for {var}: {e}") from None
        setattr(getattr(cfg, section), attr, value)
    return cfg


def load_discord_token(env: dict | None = None) -> str:
    """Read the token from AMP's setting, then remove it from the environment so nothing we spawn inherits it."""
    env = os.environ if env is None else env
    tok = (env.get("CLUBN_DISCORD_TOKEN") or env.get("DISCORD_TOKEN") or "").strip()
    for k in ("CLUBN_DISCORD_TOKEN", "DISCORD_TOKEN"):
        env.pop(k, None)
    if not tok:
        raise SystemExit("Discord bot token is empty: set 'Discord Bot Token' in the AMP instance configuration.")
    return tok
