"""Load secrets and Club Ñ web settings from a .env file in the instance folder.

The file lives next to the instance data (CLUBN_DATA_DIR, /AMP/clubn-ai-bot on AMP), never in git.
An AMP setting that is filled in wins over the file; AMP passes "" for blank settings, and those are
filled from the file. Lines look like KEY=value; '#' starts a comment line, and " #" after a value
starts a trailing comment (the Club Ñ web config strips those too).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not _KEY.match(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif value.startswith("#"):  # "KEY=   # comment" means empty
            value = ""
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        out[key] = value
    return out


def default_path(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    explicit = (env.get("CLUBN_ENV_FILE") or "").strip()
    if explicit:
        return Path(explicit)
    return Path(env.get("CLUBN_DATA_DIR") or ".") / ".env"


def load(path: Path | None = None, env: dict | None = None) -> list[str]:
    """Fill unset or empty variables from the file. Returns the names it set (never the values)."""
    env = os.environ if env is None else env
    path = path or default_path(env)
    if not path.is_file():
        return []
    loaded = []
    for key, value in parse(path.read_text(encoding="utf-8-sig")).items():
        if not (env.get(key) or "").strip():
            env[key] = value
            loaded.append(key)
    return loaded
