"""SQLite storage. Snapshots are what make velocity and acceleration possible.

One row per (fingerprint, collected_at) in `snapshots` — that history is the
only reason ReelPulse can tell a reel that is *accelerating* from one that
merely has a big lifetime number.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .models import Candidate

log = logging.getLogger("reelpulse")

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    fingerprint   TEXT PRIMARY KEY,
    platform      TEXT NOT NULL,
    platform_id   TEXT NOT NULL,
    url           TEXT NOT NULL,
    title         TEXT,
    caption       TEXT,
    creator       TEXT,
    creator_id    TEXT,
    published_at  TEXT,
    duration_s    REAL,
    meta          TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    fingerprint   TEXT NOT NULL,
    collected_at  TEXT NOT NULL,
    views         INTEGER,
    likes         INTEGER,
    comments      INTEGER,
    shares        INTEGER,
    saves         INTEGER,
    PRIMARY KEY (fingerprint, collected_at)
);
CREATE INDEX IF NOT EXISTS idx_snap_fp ON snapshots(fingerprint);

CREATE TABLE IF NOT EXISTS clusters (
    cluster_id    TEXT NOT NULL,
    week          TEXT NOT NULL,
    rank          INTEGER,
    vvs           REAL,
    payload       TEXT,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (cluster_id, week)
);

CREATE TABLE IF NOT EXISTS rules (
    week          TEXT NOT NULL,
    antecedent    TEXT NOT NULL,
    consequent    TEXT NOT NULL,
    support       REAL,
    confidence    REAL,
    lift          REAL,
    n             INTEGER,
    PRIMARY KEY (week, antecedent, consequent)
);

-- Rate limiting lives here rather than being created lazily by RateLimiter:
-- these tables belong to the store, and creating them on first limiter use made
-- table existence depend on construction order.
CREATE TABLE IF NOT EXISTS api_spend (
    service   TEXT NOT NULL,
    ts        TEXT NOT NULL,
    cost      REAL NOT NULL,
    endpoint  TEXT,
    key       TEXT          -- what was spent, for distinct-counted limits
);
CREATE INDEX IF NOT EXISTS idx_spend ON api_spend(service, ts);

CREATE TABLE IF NOT EXISTS api_cooldown (
    service   TEXT PRIMARY KEY,
    until     TEXT NOT NULL,
    reason    TEXT,
    failures  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS hashtag_budget (
    hashtag    TEXT NOT NULL,
    queried_at TEXT NOT NULL,
    hashtag_id TEXT,
    PRIMARY KEY (hashtag, queried_at)
);

-- One row per clip per week: the craft attributes and the (within-week
-- z-scored, therefore cross-week comparable) VVS. This is what lets pattern
-- mining pool several weeks, which the evaluation showed is the single biggest
-- lever on both statistical power and week-to-week stability.
CREATE TABLE IF NOT EXISTS mining_rows (
    week       TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    itemset    TEXT NOT NULL,
    vvs        REAL NOT NULL,
    PRIMARY KEY (week, cluster_id)
);
CREATE INDEX IF NOT EXISTS idx_mining_week ON mining_rows(week);

CREATE TABLE IF NOT EXISTS own_media (
    media_id      TEXT PRIMARY KEY,
    permalink     TEXT,
    caption       TEXT,
    timestamp     TEXT,
    metrics       TEXT,
    collected_at  TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path = "data/reelpulse.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    # A column added to SCHEMA reaches a new database and no existing one:
    # CREATE TABLE IF NOT EXISTS does nothing to a table that is already there.
    # On CI the database arrives from a cache written by an older release, so
    # every migration here runs against a real user's data on the next run —
    # which is why they are additive only, and why this lives in Store rather
    # than in whichever component happens to need the column. `ledger import`
    # opens the store and nothing else; a migration parked in RateLimiter would
    # never have run before the first query that needed it.
    MIGRATIONS = [
        ("api_spend", "key", "TEXT"),
    ]

    def _migrate(self) -> None:
        for table, column, decl in self.MIGRATIONS:
            have = {row[1] for row in
                    self.conn.execute(f"PRAGMA table_info({table})")}
            if have and column not in have:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                log.info("[db] migrated %s: added %s", table, column)

    # ---- writes --------------------------------------------------------

    def upsert_candidates(self, candidates: Iterable[Candidate]) -> int:
        count = 0
        for cand in candidates:
            row = cand.to_row()
            self.conn.execute(
                """
                INSERT INTO candidates (fingerprint, platform, platform_id, url, title,
                    caption, creator, creator_id, published_at, duration_s, meta,
                    first_seen, last_seen)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    title=excluded.title, caption=excluded.caption,
                    creator=excluded.creator, duration_s=excluded.duration_s,
                    meta=excluded.meta, last_seen=excluded.last_seen
                """,
                (
                    row["fingerprint"], row["platform"], row["platform_id"], row["url"],
                    row["title"], row["caption"], row["creator"], row["creator_id"],
                    row["published_at"], row["duration_s"], json.dumps(row["meta"]),
                    row["collected_at"], row["collected_at"],
                ),
            )
            self.conn.execute(
                """
                INSERT OR REPLACE INTO snapshots
                    (fingerprint, collected_at, views, likes, comments, shares, saves)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    row["fingerprint"], row["collected_at"], row["views"], row["likes"],
                    row["comments"], row["shares"], row["saves"],
                ),
            )
            count += 1
        self.conn.commit()
        return count

    def save_clusters(self, week: str, payload: list[dict]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        for item in payload:
            self.conn.execute(
                """INSERT OR REPLACE INTO clusters
                   (cluster_id, week, rank, vvs, payload, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (item["cluster_id"], week, item.get("rank"), item.get("vvs"),
                 json.dumps(item), now),
            )
        self.conn.commit()

    def save_rules(self, week: str, rules: list[dict]) -> None:
        for rule in rules:
            self.conn.execute(
                """INSERT OR REPLACE INTO rules
                   (week, antecedent, consequent, support, confidence, lift, n)
                   VALUES (?,?,?,?,?,?,?)""",
                (week, " & ".join(rule["antecedent"]), rule["consequent"],
                 rule["support"], rule["confidence"], rule["lift"], rule["n"]),
            )
        self.conn.commit()

    def save_mining_rows(self, week: str, rows: list[tuple[str, list[str], float]]
                         ) -> None:
        for cluster_id, itemset, vvs in rows:
            self.conn.execute(
                """INSERT OR REPLACE INTO mining_rows
                   (week, cluster_id, itemset, vvs) VALUES (?,?,?,?)""",
                (week, cluster_id, json.dumps(sorted(itemset)), float(vvs)))
        self.conn.commit()

    def mining_rows(self, weeks: int = 4) -> tuple[list[tuple[set[str], float]], int]:
        """Pooled (itemset, vvs) rows from the most recent `weeks` weeks.

        Returns the rows and how many distinct weeks they actually span, so the
        caller can report the real pool rather than the requested one.
        """
        cur = self.conn.execute(
            "SELECT DISTINCT week FROM mining_rows ORDER BY week DESC LIMIT ?",
            (weeks,))
        recent = [row[0] for row in cur.fetchall()]
        if not recent:
            return [], 0

        placeholders = ",".join("?" * len(recent))
        cur = self.conn.execute(
            f"SELECT itemset, vvs FROM mining_rows WHERE week IN ({placeholders})",
            recent)
        return [(set(json.loads(r[0])), float(r[1])) for r in cur.fetchall()], len(recent)

    def save_own_media(self, items: list[dict]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        for item in items:
            self.conn.execute(
                """INSERT OR REPLACE INTO own_media
                   (media_id, permalink, caption, timestamp, metrics, collected_at)
                   VALUES (?,?,?,?,?,?)""",
                (item["id"], item.get("permalink"), item.get("caption"),
                 item.get("timestamp"), json.dumps(item.get("metrics", {})), now),
            )
        self.conn.commit()

    # ---- reads ---------------------------------------------------------

    def prior_snapshot(self, fingerprint: str, before: datetime,
                       min_gap_hours: float = 8.0) -> sqlite3.Row | None:
        """Most recent snapshot at least `min_gap_hours` older than `before`.

        Used to compute acceleration. Returns None on the first-ever sighting,
        in which case the scorer falls back to a neutral acceleration.
        """
        cutoff = (before - timedelta(hours=min_gap_hours)).isoformat()
        cur = self.conn.execute(
            """SELECT * FROM snapshots
               WHERE fingerprint = ? AND collected_at <= ?
               ORDER BY collected_at DESC LIMIT 1""",
            (fingerprint, cutoff),
        )
        return cur.fetchone()

    def own_media(self) -> list[dict]:
        cur = self.conn.execute("SELECT * FROM own_media ORDER BY timestamp DESC")
        out = []
        for row in cur.fetchall():
            item = dict(row)
            item["metrics"] = json.loads(item["metrics"] or "{}")
            out.append(item)
        return out

    def rules(self, week: str | None = None) -> list[dict]:
        if week:
            cur = self.conn.execute("SELECT * FROM rules WHERE week = ? ORDER BY lift DESC", (week,))
        else:
            cur = self.conn.execute("SELECT * FROM rules ORDER BY lift DESC")
        return [dict(row) for row in cur.fetchall()]

    def close(self) -> None:
        self.conn.close()
