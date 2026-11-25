"""Fulgencio + Club Ñ web features: settings, AI fallback chain and the one-client wiring."""
import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from clubn_bot import envfile, web_settings
from clubn_bot.ai_chain import PAUSE_S, Chain
from clubn_bot.config import load_config

APP = Path(__file__).resolve().parents[1]   # src/clubn-ai-bot
ROOT = APP.parents[1]                        # template repository root


# ---------- .env fallback ----------

def test_envfile_parse_and_precedence(tmp_path):
    (tmp_path / ".env").write_text(
        "# comment\nA=1\nB=two words  # trailing\nC=   # only a comment\nD='q # kept'\n"
        "export E=5\nbad key=x\nF=from-file\n", encoding="utf-8")
    env = {"CLUBN_DATA_DIR": str(tmp_path), "F": "from-amp", "A": ""}
    loaded = envfile.load(env=env)
    assert env["A"] == "1" and env["B"] == "two words" and env["C"] == "" and env["D"] == "q # kept"
    assert env["E"] == "5" and env["F"] == "from-amp"  # a filled AMP setting wins
    assert "F" not in loaded and "bad key" not in env


def test_env_example_parses_to_the_defaults():
    parsed = envfile.parse((APP / "defaults" / "env.example").read_text(encoding="utf-8"))
    for var, _f, _d, kind, default, *_ in web_settings.SETTINGS:
        assert parsed[var] == ("" if kind == "password" else default), var


# ---------- AMP panel covers every setting ----------

def test_amp_files_in_sync():
    r = subprocess.run([sys.executable, str(APP / "tools" / "gen_amp_settings.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_every_clubn_web_setting_is_in_amp():
    src = (APP / "clubn_web" / "config.py").read_text(encoding="utf-8")
    used = set(re.findall(r'(?:_env|_int|_ids|_bool|os\.getenv|os\.environ\[)\(?"([A-Z_]+)"', src))
    handled_elsewhere = {"DISCORD_TOKEN", "GUILD_ID", "TIMEZONE", "DB_PATH"}  # Fulgencio's own AMP settings
    assert used - handled_elsewhere - set(web_settings.VARIABLES) == set()


def test_secrets_are_password_fields_and_kept_from_ollama():
    assert set(web_settings.SECRETS) == {"WP_APP_PASSWORD", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
                                         "YOUTUBE_API_KEY", "TWITCH_CLIENT_SECRET", "POSTIZ_API_KEY",
                                         "BLUESKY_APP_PASSWORD", "META_PAGE_TOKEN", "THREADS_TOKEN",
                                         "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET",
                                         "TIKTOK_CLIENT_SECRET", "TIKTOK_REFRESH_TOKEN"}
    for script in ("clubn-amp-run", "clubn-amp-update"):
        text = (ROOT / "docker" / "clubn-ai-bot" / script).read_text(encoding="utf-8")
        line = next(l for l in text.splitlines() if l.startswith("NO_SECRETS="))
        for var in [*web_settings.SECRETS, "CLUBN_DISCORD_TOKEN", "DISCORD_TOKEN"]:
            assert var in re.findall(r"-u ([A-Z_]+)", line), (script, var)
        assert "ollama serve" in text and "env -u CLUBN_DISCORD_TOKEN /usr/bin/ollama" not in text


# ---------- AI chain: Claude -> DeepSeek -> local ----------

class Out(BaseModel):
    title: str


class FakeWriter:
    def __init__(self, claude=None, deepseek=None, anthropic_key="k", deepseek_key="k"):
        self.config = SimpleNamespace(ANTHROPIC_API_KEY=anthropic_key, DEEPSEEK_API_KEY=deepseek_key)
        self._claude = object()
        self.claude, self.deepseek = claude, deepseek
        self.calls = []

    async def _write_claude(self, system, prompt, schema):
        self.calls.append("claude")
        if isinstance(self.claude, Exception):
            raise self.claude
        return schema(title=self.claude)

    async def _write_deepseek(self, system, prompt, schema):
        self.calls.append("deepseek")
        if isinstance(self.deepseek, Exception):
            raise self.deepseek
        return schema(title=self.deepseek)


class FakeLocal:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    async def chat(self, messages, schema=None, **kw):
        self.calls += 1
        assert schema and "SOLO con un objeto JSON" in messages[0]["content"]
        return self.replies.pop(0)


class BadRequestError(Exception):  # same name as anthropic's
    pass


def run(coro):
    return asyncio.run(coro)


def test_chain_prefers_claude():
    w = FakeWriter(claude="c", deepseek="d")
    assert run(Chain(w, FakeLocal()).structured("s", "p", Out)).title == "c"
    assert w.calls == ["claude"]


def test_chain_claude_out_of_credits_goes_to_deepseek_and_pauses_claude():
    now = [1000.0]
    w = FakeWriter(claude=BadRequestError("Your credit balance is too low"), deepseek="d")
    ch = Chain(w, FakeLocal(), clock=lambda: now[0])
    assert run(ch.structured("s", "p", Out)).title == "d"
    assert run(ch.structured("s", "p", Out)).title == "d"
    assert w.calls == ["claude", "deepseek", "deepseek"]  # Claude skipped while paused
    now[0] += PAUSE_S + 1
    run(ch.structured("s", "p", Out))
    assert w.calls[-2:] == ["claude", "deepseek"]


def test_chain_falls_back_to_local_when_both_apis_fail():
    w = FakeWriter(claude=TimeoutError("slow"), deepseek=RuntimeError("DeepSeek rejected the request (402): no balance"))
    local = FakeLocal('{"title": "local"}')
    ch = Chain(w, local)
    out = run(ch.structured("s", "p", Out))
    assert out.title == "local" and ch.last_provider == "local"
    assert "deepseek" in ch.paused_until and "claude" not in ch.paused_until  # timeout isn't a pause


def test_chain_without_keys_uses_local_and_retries_bad_json_once():
    w = FakeWriter(anthropic_key="", deepseek_key="")
    local = FakeLocal("not json", '<think>x</think>{"title": "ok"}')
    assert run(Chain(w, local).structured("s", "p", Out)).title == "ok"
    assert w.calls == [] and local.calls == 2


def test_chain_reports_every_failure():
    w = FakeWriter(claude=RuntimeError("down"), deepseek=RuntimeError("down too"))
    with pytest.raises(RuntimeError, match="claude.*deepseek.*local"):
        run(Chain(w, None).structured("s", "p", Out))


# ---------- one client for everything ----------

@pytest.fixture(scope="module")
def merged_bot(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("inst")
    saved = dict(os.environ)
    os.environ.update({"WP_USER": "bot", "WP_APP_PASSWORD": "x", "CREATOR_ROLE_NAME": "", "MIS_REDES_ENABLED": "true",
                       "LIVE_ENABLED": "true", "EVENTS_ENABLED": "true"})
    os.environ.pop("DB_PATH", None)
    cfg = load_config({"CLUBN_DATA_DIR": str(tmp), "CLUBN_COMMAND_NAME": "fulgencio", "CLUBN_GUILD_ID": "123"})
    from clubn_bot import merged

    pipeline = SimpleNamespace(admit=lambda *a: None, charge=lambda *a: None, refund=lambda *a: None,
                               llm=SimpleNamespace(), aclose=lambda: asyncio.sleep(0))
    client, fulgencio, chain = merged.build(cfg, "token-for-tests", local_llm=FakeLocal(), pipeline=pipeline)
    env_after = dict(os.environ)
    yield SimpleNamespace(client=client, fulgencio=fulgencio, chain=chain, env=env_after, tmp=tmp)
    os.environ.clear()
    os.environ.update(saved)


def test_merged_env(merged_bot):
    from clubn_web import config

    assert "DISCORD_TOKEN" not in merged_bot.env  # removed after clubn_web read it
    assert config.DISCORD_TOKEN == "token-for-tests"
    assert config.GUILD_ID == 123
    assert config.CREATOR_ROLE_NAME == "creador de contenido"  # blank AMP field -> default, not ""
    assert Path(config.DB_PATH) == merged_bot.tmp / "data" / "clubn_web.db"


def test_merged_uses_the_chain(merged_bot):
    from clubn_web import writer

    assert writer.structured == merged_bot.chain.structured
    assert merged_bot.chain.cpu_lock is merged_bot.fulgencio.worker._sem  # local drafts queue with questions


def test_merged_command_tree(merged_bot):
    client = merged_bot.client
    intents = client.intents
    assert intents.members and intents.message_content

    async def go():
        synced = []

        async def fake_sync(*, guild=None):
            synced.append(guild.id if guild else None)
            return []

        client.tree.sync = fake_sync
        from clubn_web import main as web
        web.daily_sweep.start = lambda *a, **k: None  # no background sweep in tests
        await client.setup_hook()
        return synced

    synced = asyncio.run(go())
    assert synced == [123]
    names = [c.name for c in client.tree.get_commands(guild=SimpleNamespace(id=123))]
    assert len(names) == len(set(names)), names
    for n in ("fulgencio", "redes-creador", "generar-articulo", "mis_redes", "directos"):
        assert n in names, (n, names)
    fulg = client.tree.get_command("fulgencio")
    assert [p.name for p in fulg.parameters] == ["pregunta"]  # the real one, not the old placeholder


def test_social_posts_stay_off_by_default(merged_bot):
    assert merged_bot.client._clubn_social is None
    names = [c.name for c in merged_bot.client.tree.get_commands()]
    assert "anuncia_en_redes" not in names


def test_every_social_setting_is_in_amp_and_secrets_are_password_fields():
    src = (APP / "clubn_bot" / "social_networks.py").read_text(encoding="utf-8")
    used = set(re.findall(r'g\("([A-Z_]+)"', src)) | set(re.findall(r'e\.get\("([A-Z_]+)"\)', src))
    kinds = {s[0]: s[3] for s in web_settings.SETTINGS}
    assert used - set(kinds) == set()
    for var in used:
        if re.search(r"SECRET|PASSWORD|TOKEN|API_KEY", var) and var not in ("X_API_KEY",):
            assert kinds[var] == "password", var
