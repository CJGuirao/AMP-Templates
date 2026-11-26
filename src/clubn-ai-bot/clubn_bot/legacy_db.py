"""Merge SuperAntonia's old database (bot.db) into the merged bot's database (clubn_web.db).

The README asked for SuperAntonia's bot.db to be copied over clubn_web.db before the first start. If it
was copied next to it instead (data/bot.db), the bot ran on an almost empty database: every startup sweep
treated creators SuperAntonia had already written about as new (three more each time), and events it
was following were unknown. This imports that file once, on startup, before the bot connects:

- creators: missing ones are added; where both have a row, the one further along the review flow wins
  (published > dismissed > in review > anything else), so a creator re-asked by mistake goes back to
  "published". Rows replaced that way are logged with the WordPress draft that was made twice.
- events: matched by key; missing ones are added, and a followed (live/done) event replaces a copy that
  was re-detected from scratch. News collected since the copy was made is kept.
- other tables (images already uploaded, Momentazos seen, /mis_redes history, ...): rows not present yet.

clubn_web.db is backed up first, and bot.db is renamed to bot.db.imported-<time> afterwards, so this runs
once and nothing is deleted.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("clubn_bot.legacy_db")

LEGACY_NAME = "bot.db"

# How far along each status is; the higher one wins when both databases know the same row.
CREATOR_RANK = {"published": 5, "dismissed": 4, "in_review": 3, "drafting": 2, "error": 1, "pending_links": 1,
                "skipped_existing": 0}
EVENT_RANK = {"live": 4, "done": 4, "dismissed": 3, "new": 2, "error": 1}


def _tables(c: sqlite3.Connection, schema: str) -> dict[str, str]:
    return {name: sql for name, sql in c.execute(
        f"SELECT name, sql FROM {schema}.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _cols(c: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [r[1] for r in c.execute(f'PRAGMA {schema}.table_info("{table}")')]


def _pk(c: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [r[1] for r in sorted(c.execute(f'PRAGMA {schema}.table_info("{table}")'), key=lambda r: r[5]) if r[5]]


def _rows(c: sqlite3.Connection, sql: str, args=()) -> list[dict]:
    cur = c.execute(sql, args)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _insert(c: sqlite3.Connection, table: str, row: dict) -> int:
    cols = ", ".join(f'"{k}"' for k in row)
    return c.execute(f'INSERT INTO main."{table}" ({cols}) VALUES ({", ".join("?" * len(row))})',
                     tuple(row.values())).lastrowid


def _update(c: sqlite3.Connection, table: str, key: str, value, row: dict) -> None:
    sets = ", ".join(f'"{k}"=?' for k in row)
    c.execute(f'UPDATE main."{table}" SET {sets} WHERE "{key}"=?', (*row.values(), value))


def _merge_creators(c: sqlite3.Connection, cols: list[str], report: dict) -> None:
    target = {r["discord_id"]: r for r in _rows(c, "SELECT * FROM main.creators")}
    for old in _rows(c, f'SELECT {", ".join(cols)} FROM legacy.creators'):
        new = target.get(old["discord_id"])
        if new is None:
            _insert(c, "creators", old)
            report["creators_added"] += 1
        elif CREATOR_RANK.get(old["status"], 0) > CREATOR_RANK.get(new["status"], 0):
            _update(c, "creators", "discord_id", old["discord_id"],
                    {k: v for k, v in old.items() if k != "discord_id"})
            report["creators_restored"].append({
                "discord_id": old["discord_id"], "name": old.get("display_name"), "status": old["status"],
                "was": new["status"], "duplicate_wp_post_id": new.get("wp_post_id")
                if new.get("wp_post_id") and new.get("wp_post_id") != old.get("wp_post_id") else None,
                "stale_review_message_id": new.get("review_message_id")})


def _facts(v) -> list:
    try:
        return json.loads(v) if v else []
    except ValueError:
        return []


def _merge_events(c: sqlite3.Connection, cols: list[str], report: dict) -> None:
    target = {r["key"]: r for r in _rows(c, "SELECT * FROM main.events")}
    used_ids = {r["id"] for r in target.values()}
    for old in _rows(c, f'SELECT {", ".join(cols)} FROM legacy.events'):
        new = target.get(old["key"])
        if new is None:
            row = dict(old)
            if row.get("id") in used_ids:
                row.pop("id")  # keep the old number when it's free (its Discord buttons carry it)
            used_ids.add(_insert(c, "events", row))
            report["events_added"] += 1
        elif EVENT_RANK.get(old["status"], 0) > EVENT_RANK.get(new["status"], 0):
            row = {k: v for k, v in old.items() if k != "id"}
            if "facts" in row:  # keep what the bot learned since the copy; it becomes news for the next update
                old_facts = _facts(old.get("facts"))
                seen = {f.get("url") for f in old_facts if isinstance(f, dict) and f.get("url")}
                extra = [f for f in _facts(new.get("facts"))
                         if not (isinstance(f, dict) and f.get("url") and f["url"] in seen)]
                row["facts"] = json.dumps(old_facts + extra, ensure_ascii=False)
            _update(c, "events", "id", new["id"], row)
            report["events_restored"].append({"id": new["id"], "name": old.get("name"), "status": old["status"],
                                              "was": new["status"]})


def _merge_other(c: sqlite3.Connection, table: str, cols: list[str], report: dict) -> None:
    pk = _pk(c, "legacy", table)
    if pk == ["id"] and "nonce" in cols:  # /mis_redes requests: numbered locally, unique by nonce
        rest = [k for k in cols if k != "id"]
        n = c.execute(f'INSERT INTO main."{table}" ({", ".join(rest)}) SELECT {", ".join(rest)} FROM legacy."{table}" '
                      f'WHERE nonce NOT IN (SELECT nonce FROM main."{table}")').rowcount
    elif pk == ["id"]:
        log.info("Old database: skipped table %s (no way to tell rows apart)", table)
        return
    else:
        q = ", ".join(f'"{k}"' for k in cols)
        n = c.execute(f'INSERT OR IGNORE INTO main."{table}" ({q}) SELECT {q} FROM legacy."{table}"').rowcount
    if n:
        report["other"][table] = n


def merge(target: Path, legacy: Path) -> dict:
    """Merge legacy into target in one transaction. Returns what changed."""
    report = {"creators_added": 0, "creators_restored": [], "events_added": 0, "events_restored": [], "other": {}}
    c = sqlite3.connect(target, isolation_level=None)
    try:
        c.execute("ATTACH DATABASE ? AS legacy", (str(legacy),))
        c.execute("BEGIN IMMEDIATE")
        have = _tables(c, "main")
        for table, sql in _tables(c, "legacy").items():
            if table not in have:
                c.execute(sql)  # a feature that never ran in the merged bot yet: same table as before
            cols = [k for k in _cols(c, "legacy", table) if k in set(_cols(c, "main", table))]
            if not cols:
                continue
            if table == "creators" and "discord_id" in cols and "status" in cols:
                _merge_creators(c, cols, report)
            elif table == "events" and {"key", "status"} <= set(cols):
                _merge_events(c, cols, report)
            else:
                _merge_other(c, table, cols, report)
        c.execute("COMMIT")
    except BaseException:
        if c.in_transaction:
            c.execute("ROLLBACK")
        raise
    finally:
        c.close()
    return report


def import_if_present(db_path: str | Path) -> dict | None:
    """Run on startup: merge <folder of db_path>/bot.db into db_path once, then rename it."""
    target = Path(db_path)
    legacy = target.parent / LEGACY_NAME
    if not legacy.is_file() or legacy.resolve() == target.resolve():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if target.is_file():  # backup first, with SQLite's own copy (safe even mid-write)
        backup = target.with_name(f"{target.name}.before-import-{stamp}")
        src, dst = sqlite3.connect(target), sqlite3.connect(backup)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        log.info("Backed up %s to %s", target.name, backup.name)
    report = merge(target, legacy)
    legacy.rename(legacy.with_name(f"{LEGACY_NAME}.imported-{stamp}"))
    log.warning("Imported SuperAntonia's %s into %s: %d creators added, %d restored, %d events added, %d restored, "
                "other tables %s", LEGACY_NAME, target.name, report["creators_added"], len(report["creators_restored"]),
                report["events_added"], len(report["events_restored"]), report["other"] or "none")
    for r in report["creators_restored"]:
        log.warning("Creator %s (%s) back to %s (was %s)%s", r["name"], r["discord_id"], r["status"], r["was"],
                    f"; WordPress draft {r['duplicate_wp_post_id']} was made twice and can be trashed"
                    if r["duplicate_wp_post_id"] else "")
    for r in report["events_restored"]:
        log.warning("Event #%s %s back to %s (was %s)", r["id"], r["name"], r["status"], r["was"])
    return report
