"""Instagram Hashtag Search — native discovery of OTHER people's reels.

This is the endpoint that makes ReelPulse an Instagram tool rather than a
YouTube tool wearing an Instagram hat. `ig_hashtag_search` resolves a hashtag to
an id, and `/{hashtag-id}/top_media` returns *public posts by other accounts* —
with `like_count`, `comments_count`, `caption`, `permalink` and `media_type`.

It is official, free, and needs the same Business/Creator token you already set
for your own insights. What it does not return is `view_count`: Meta exposes
view counts for other people's media only through Business Discovery (see
`instagram_discovery.py`), and only for professional accounts. So clips found
here are ranked on engagement scale rather than view scale, and the scorer
labels them `measurement_basis: engagement` so nothing pretends otherwise.

The binding constraint is unusual and worth respecting carefully:

    **30 unique hashtags per rolling 7-day window, per user.**

Query a 31st and Meta rejects it until one ages out. That is a *hard* budget, so
this collector persists which hashtags it has spent and when, refuses to exceed
the window, and tells you what it has left. Blowing the budget on Monday means
no Instagram discovery until the following Monday.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from ..config import env
from ..models import Candidate
from .base import Collector

log = logging.getLogger("reelpulse")

API = "https://graph.facebook.com/v23.0"

MEDIA_FIELDS = ("id,caption,media_type,media_url,permalink,timestamp,"
                "like_count,comments_count,children{media_type,media_url}")

WINDOW_DAYS = 7
MAX_UNIQUE_HASHTAGS = 30


class HashtagBudget:
    """Tracks the rolling 30-per-7-days hashtag allowance.

    Persisted, because the window is enforced by Meta across *all* your calls,
    not per process. An in-memory counter would reset every run and walk you
    straight into a hard rejection.
    """

    def __init__(self, store) -> None:
        self.store = store
        store.conn.execute("""
            CREATE TABLE IF NOT EXISTS hashtag_budget (
                hashtag    TEXT NOT NULL,
                queried_at TEXT NOT NULL,
                hashtag_id TEXT,
                PRIMARY KEY (hashtag, queried_at)
            )""")
        store.conn.commit()

    def _cutoff(self) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).isoformat()

    def spent(self) -> set[str]:
        cur = self.store.conn.execute(
            "SELECT DISTINCT hashtag FROM hashtag_budget WHERE queried_at >= ?",
            (self._cutoff(),))
        return {row[0] for row in cur.fetchall()}

    def remaining(self) -> int:
        return max(MAX_UNIQUE_HASHTAGS - len(self.spent()), 0)

    def cached_id(self, hashtag: str) -> str | None:
        """Hashtag ids are stable, so a cached one costs nothing to reuse.

        Reusing an id for a hashtag already inside the window is free — the
        window counts *unique hashtags*, not calls.
        """
        cur = self.store.conn.execute(
            "SELECT hashtag_id FROM hashtag_budget WHERE hashtag = ? "
            "AND hashtag_id IS NOT NULL ORDER BY queried_at DESC LIMIT 1",
            (hashtag,))
        row = cur.fetchone()
        return row[0] if row else None

    def record(self, hashtag: str, hashtag_id: str | None) -> None:
        self.store.conn.execute(
            "INSERT OR REPLACE INTO hashtag_budget (hashtag, queried_at, hashtag_id) "
            "VALUES (?,?,?)",
            (hashtag, datetime.now(timezone.utc).isoformat(), hashtag_id))
        self.store.conn.commit()

    def plan(self, wanted: list[str]) -> tuple[list[str], list[str]]:
        """Split a wish list into (affordable, deferred).

        Hashtags already inside the window are free — they do not consume a new
        slot — so they always come first.
        """
        spent = self.spent()
        free = [h for h in wanted if h in spent]
        fresh = [h for h in wanted if h not in spent]
        budget = self.remaining()
        return free + fresh[:budget], fresh[budget:]


class InstagramHashtagCollector(Collector):
    name = "instagram_hashtag"
    service = "instagram_graph"   # shares the app-level hourly budget
    requires = ["IG_ACCESS_TOKEN", "IG_USER_ID"]

    def __init__(self, config: dict, store=None, limiter=None) -> None:
        super().__init__(config, limiter)
        self.budget = HashtagBudget(store) if store is not None else None

    def collect(self) -> list[Candidate]:
        return self.discover(self.config.get("hashtags", []))

    def _resolve(self, hashtag: str, token: str, user_id: str) -> str | None:
        if self.budget:
            cached = self.budget.cached_id(hashtag)
            if cached:
                return cached
        data = self.get_json(f"{API}/ig_hashtag_search",
                             params={"user_id": user_id, "q": hashtag,
                                     "access_token": token}, retries=2)
        items = data.get("data") or []
        return items[0]["id"] if items else None

    def discover(self, hashtags: list[str], *, edge: str | None = None,
                 limit: int | None = None) -> list[Candidate]:
        token, user_id = env("IG_ACCESS_TOKEN"), env("IG_USER_ID")
        if not token or not user_id or token.startswith("your_"):
            log.warning("[instagram_hashtag] IG_ACCESS_TOKEN + IG_USER_ID not set "
                        "— Instagram-native discovery is OFF, so the leaderboard "
                        "will only contain clips that were cross-posted to YouTube.")
            return []

        hashtags = [h.lstrip("#").lower() for h in hashtags if h.strip()]
        if not hashtags:
            return []

        edge = edge or self.config.get("edge", "top_media")
        limit = limit or int(self.config.get("limit", 50))

        if self.budget:
            affordable, deferred = self.budget.plan(hashtags)
            if deferred:
                # Never silently truncate a budget-limited sweep.
                log.warning("[instagram_hashtag] budget: %d/%d unique hashtags left "
                            "in the 7-day window. Deferring: %s",
                            self.budget.remaining(), MAX_UNIQUE_HASHTAGS,
                            ", ".join(f"#{h}" for h in deferred))
            hashtags = affordable

        out: list[Candidate] = []
        for hashtag in hashtags:
            try:
                hashtag_id = self._resolve(hashtag, token, user_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("[instagram_hashtag] could not resolve #%s (%s)", hashtag, exc)
                continue
            if not hashtag_id:
                log.info("[instagram_hashtag] #%s returned no id", hashtag)
                continue
            if self.budget:
                self.budget.record(hashtag, hashtag_id)

            try:
                data = self.get_json(
                    f"{API}/{hashtag_id}/{edge}",
                    params={"user_id": user_id, "fields": MEDIA_FIELDS,
                            "limit": limit, "access_token": token}, retries=2)
            except Exception as exc:  # noqa: BLE001
                log.warning("[instagram_hashtag] #%s %s failed (%s)", hashtag, edge, exc)
                continue

            out.extend(self._parse(data.get("data", []), hashtag, edge))

        log.info("[instagram_hashtag] %d reels from %d hashtags (%d budget left)",
                 len(out), len(hashtags),
                 self.budget.remaining() if self.budget else -1)
        return out

    def _parse(self, items: list[dict], hashtag: str, edge: str) -> list[Candidate]:
        out: list[Candidate] = []
        for item in items:
            # VIDEO covers reels here; hashtag search does not expose
            # media_product_type, so short videos are the best available filter.
            if item.get("media_type") not in {"VIDEO", "CAROUSEL_ALBUM"}:
                continue

            caption = item.get("caption") or ""
            shortcode = ""
            permalink = item.get("permalink") or ""
            if "/reel/" in permalink or "/p/" in permalink:
                shortcode = permalink.rstrip("/").split("/")[-1]

            out.append(Candidate(
                platform="instagram",
                platform_id=shortcode or item["id"],
                url=permalink or f"https://www.instagram.com/p/{shortcode}/",
                title=caption[:180],
                caption=caption,
                creator="",          # hashtag search cannot return username
                published_at=item.get("timestamp"),
                # No view count exists for other people's media on this edge.
                # Leaving it None is deliberate — the scorer switches such clips
                # to an engagement basis rather than guessing a number.
                views=None,
                likes=item.get("like_count"),
                comments=item.get("comments_count"),
                meta={
                    "discovered_via": f"hashtag:{hashtag}:{edge}",
                    "hashtags": [hashtag],
                    "media_type": item.get("media_type"),
                    "ig_media_id": item.get("id"),
                    "instagram_native": True,
                },
                source="instagram_hashtag_search",
            ))
        return out
