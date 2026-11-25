"""Regenerate the AMP settings for the Club Ñ web features from clubn_bot/web_settings.py.

    python tools/gen_amp_settings.py          # rewrite the three files
    python tools/gen_amp_settings.py --check  # exit 1 if any of them is out of date (used by the tests)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]   # src/clubn-ai-bot
ROOT = APP.parents[1]                        # template repository root
sys.path.insert(0, str(APP))

from clubn_bot import web_settings as ws  # noqa: E402

CONFIG = ROOT / "clubn-ai-botconfig.json"
KVP = ROOT / "clubn-ai-bot.kvp"
ENV_EXAMPLE = APP / "defaults" / "env.example"


def render_config(text: str) -> str:
    entries = [e for e in json.loads(text) if e["Category"] != ws.CATEGORY]
    entries += ws.amp_entries()
    return json.dumps(entries, ensure_ascii=False, indent=2) + "\n"


def render_kvp(text: str) -> str:
    m = re.search(r"^App\.EnvironmentVariables=(.*)$", text, re.M)
    env = {k: v for k, v in json.loads(m.group(1)).items() if k not in ws.VARIABLES}
    env.update(ws.kvp_env())
    line = "App.EnvironmentVariables=" + json.dumps(env, ensure_ascii=False, separators=(",", ":"))
    return text[: m.start()] + line + text[m.end():]


def main() -> int:
    check = "--check" in sys.argv
    stale = []
    for path, render in ((CONFIG, render_config), (KVP, render_kvp), (ENV_EXAMPLE, None)):
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        new = ws.env_example() if render is None else render(old)
        if new != old:
            stale.append(path.name)
            if not check:
                path.write_text(new, encoding="utf-8", newline="\n")
    if check and stale:
        print("Out of date (run python src/clubn-ai-bot/tools/gen_amp_settings.py):", ", ".join(stale))
        return 1
    print("Updated:" if stale else "Up to date.", ", ".join(stale))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
