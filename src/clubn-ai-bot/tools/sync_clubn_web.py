"""Refresh src/clubn-ai-bot/clubn_web from the SuperAntonia source folder (ClubNWebAIAnnouncer/bot).

clubn_web is kept as an unmodified copy; all the Fulgencio-specific wiring is in clubn_bot/merged.py
and clubn_bot/ai_chain.py. After syncing, run the tests (they check the hooks merged.py relies on) and
python src/clubn-ai-bot/tools/gen_amp_settings.py --check (a new setting in clubn_web/config.py needs a line in
clubn_bot/web_settings.py).

    python src/clubn-ai-bot/tools/sync_clubn_web.py path/to/ClubNWebAIAnnouncer/bot
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

DEST = Path(__file__).resolve().parents[1] / "clubn_web"
if len(sys.argv) != 2:
    raise SystemExit(__doc__)
SRC = Path(sys.argv[1])

if not (SRC / "main.py").is_file():
    raise SystemExit(f"{SRC} does not look like the SuperAntonia bot package")
changed = []
for f in sorted(SRC.glob("*.py")):
    d = DEST / f.name
    if not d.exists() or d.read_bytes() != f.read_bytes():
        shutil.copyfile(f, d)
        changed.append(f.name)
gone = sorted(p.name for p in DEST.glob("*.py") if not (SRC / p.name).exists())
print("Copied:", ", ".join(changed) or "nothing (already in sync)")
if gone:
    print("Only in clubn_web (removed upstream? delete by hand if so):", ", ".join(gone))
