"""Make the rate-limit ledger outlive the machine that wrote it.

Every quota this project respects is enforced by the platform across *all* your
calls, not per process — so the record of what has been spent has to be at least
as durable as the window it covers. On a laptop the SQLite file handles that. On
GitHub Actions it does not: each run gets a fresh container, and the only thing
carrying the database between runs is `actions/cache`, which GitHub evicts after
seven days without a hit.

Seven days is exactly the length of Instagram's hashtag window. So the failure
mode is not hypothetical and not gradual: a cache eviction makes the ledger read
zero, the next run cheerfully queries thirty fresh hashtags on top of thirty it
has forgotten, and Meta rejects the lot. Nothing in the logs would say why.

This module writes the live part of the ledger to a small JSON file that gets
committed to the repository, and merges it back on the way in. The database
stays the working store; the file is the part that survives.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .limits import LIMITS

log = logging.getLogger("reelpulse")

# Nothing older than the longest window can affect any decision, so exporting it
# would only grow the file. Two days of slack absorbs clock skew between runners
# and the odd late commit.
MAX_WINDOW_S = max(lim.quota_window_s for lim in LIMITS.values())
KEEP_S = MAX_WINDOW_S + 2 * 86_400

FORMAT = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def export_ledger(store, path: str | Path) -> dict[str, Any]:
    """Write the still-relevant spend and any live cooldowns to `path`."""
    path = Path(path)
    cutoff = (_now() - timedelta(seconds=KEEP_S)).isoformat()

    spend = [
        {"service": s, "ts": ts, "cost": c, "endpoint": e, "key": k}
        for s, ts, c, e, k in store.conn.execute(
            "SELECT service, ts, cost, endpoint, key FROM api_spend "
            "WHERE ts >= ? ORDER BY ts", (cutoff,))
    ]
    cooldowns = [
        {"service": s, "until": u, "reason": r, "failures": f}
        for s, u, r, f in store.conn.execute(
            "SELECT service, until, reason, failures FROM api_cooldown "
            "WHERE until >= ?", (_now().isoformat(),))
    ]

    payload = {"format": FORMAT, "written_at": _now().isoformat(),
               "spend": spend, "cooldowns": cooldowns}
    path.parent.mkdir(parents=True, exist_ok=True)
    # sort_keys so an unchanged ledger produces an unchanged file, and the
    # commit step has nothing to push rather than churning one every run.
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8")
    return {"spend": len(spend), "cooldowns": len(cooldowns), "path": str(path)}


def import_ledger(store, path: str | Path) -> dict[str, Any]:
    """Merge a committed ledger into the database.

    Idempotent: a row already present is skipped, so importing twice — or
    importing on top of a cache hit that already had the data — changes
    nothing. Merging rather than replacing matters because the cached database
    may legitimately hold spend that the committed file does not, if a run
    ended before it could export.
    """
    path = Path(path)
    if not path.exists():
        return {"imported": 0, "skipped": 0, "reason": "no ledger file"}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        log.warning("[ledger] %s is unreadable (%s) — starting from the "
                    "database alone, which may under-count spend", path, exc)
        return {"imported": 0, "skipped": 0, "reason": f"unreadable: {exc}"}

    if payload.get("format") != FORMAT:
        log.warning("[ledger] %s is format %s, expected %s — ignoring",
                    path, payload.get("format"), FORMAT)
        return {"imported": 0, "skipped": 0, "reason": "format mismatch"}

    imported = skipped = 0
    for row in payload.get("spend", []):
        exists = store.conn.execute(
            "SELECT 1 FROM api_spend WHERE service = ? AND ts = ? "
            "AND COALESCE(key, '') = COALESCE(?, '') LIMIT 1",
            (row["service"], row["ts"], row.get("key"))).fetchone()
        if exists:
            skipped += 1
            continue
        store.conn.execute(
            "INSERT INTO api_spend (service, ts, cost, endpoint, key) "
            "VALUES (?,?,?,?,?)",
            (row["service"], row["ts"], float(row["cost"]),
             row.get("endpoint"), row.get("key")))
        imported += 1

    for row in payload.get("cooldowns", []):
        # A cooldown is a floor, never a ceiling: if the database already knows
        # about a longer one, keep it. Shortening a cooldown on import would
        # let a run go back at a platform that is still refusing us.
        current = store.conn.execute(
            "SELECT until FROM api_cooldown WHERE service = ?",
            (row["service"],)).fetchone()
        if current and current[0] >= row["until"]:
            continue
        store.conn.execute(
            "INSERT OR REPLACE INTO api_cooldown (service, until, reason, "
            "failures) VALUES (?,?,?,?)",
            (row["service"], row["until"], row.get("reason"),
             int(row.get("failures") or 0)))

    store.conn.commit()
    return {"imported": imported, "skipped": skipped,
            "written_at": payload.get("written_at")}


def staleness(path: str | Path) -> str | None:
    """How out of date the committed ledger looks, in words, or None if fine.

    An old file is not itself an error — a repo that has not run in a fortnight
    has nothing left inside any window — but it is worth saying out loud when a
    run is about to spend against it.
    """
    path = Path(path)
    if not path.exists():
        return "no committed ledger yet — this run starts from an empty budget"
    try:
        written = datetime.fromisoformat(
            json.loads(path.read_text(encoding="utf-8"))["written_at"])
    except Exception:  # noqa: BLE001
        return "committed ledger has no readable timestamp"
    age = (_now() - written).total_seconds()
    if age > MAX_WINDOW_S:
        return (f"committed ledger is {age / 86_400:.0f} days old — older than "
                f"every window, so all budgets read as full")
    return None
