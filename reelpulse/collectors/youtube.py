"""YouTube Data API v3 — the workhorse.

Why YouTube carries a leaderboard about *Instagram*: Meta publishes no public
view counts for anyone else's Reels, and never has. But the overwhelming
majority of clips that go globally viral on Reels are cross-posted to Shorts
within days, usually by the original creator and always by aggregators. Shorts
*does* expose exact view counts, for free, under an official API.

So YouTube is not a substitute subject — it is a measuring instrument pointed
at the same clip. Read the leaderboard as "the short-form clips that went
biggest this week, ranked using the numbers Shorts will tell us", and the
Instagram column as "here is that same clip on Reels".

Quota maths: free tier is 10,000 units/day. search.list costs 100 units;
videos.list costs 1 unit for up to 50 ids. The default config runs 24 searches
(2,400 units) plus ~24 detail calls (24 units) = well under a quarter of the
daily allowance, so a daily snapshot cron never runs dry.
"""
from __future__ import annotations

import itertools
import logging
import re
from datetime import datetime, timedelta, timezone

from ..config import env
from ..models import Candidate
from .base import Collector

log = logging.getLogger("reelpulse")

API = "https://www.googleapis.com/youtube/v3"
_ISO_DUR = re.compile(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?")


def parse_duration(iso: str) -> float | None:
    """ISO-8601 duration -> seconds."""
    match = _ISO_DUR.fullmatch(iso or "")
    if not match:
        return None
    days, hours, minutes, seconds = (float(g or 0) for g in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


class YouTubeCollector(Collector):
    name = "youtube"
    service = "youtube"
    requires = ["YOUTUBE_API_KEY"]

    # Quota is denominated in UNITS, not calls. Charging every call as 1 would
    # under-count spend on search.list by 100x — precisely the endpoint that
    # exhausts the daily budget.
    SEARCH_COST = 100
    VIDEOS_COST = 1

    def collect(self) -> list[Candidate]:
        key = env("YOUTUBE_API_KEY")
        if not key or key.startswith("your_"):
            log.warning("[youtube] YOUTUBE_API_KEY not set — skipping the primary "
                        "signal. The leaderboard will be much weaker.")
            return []

        cfg = self.config
        window = int(self.config.get("_window_days", 7))
        published_after = (datetime.now(timezone.utc) - timedelta(days=window)) \
            .replace(microsecond=0).isoformat().replace("+00:00", "Z")

        # Rotate region x query pairs so global coverage stays even across runs.
        pairs = list(itertools.product(cfg.get("regions", ["US"]),
                                       cfg.get("queries", ["#shorts"])))
        pairs = pairs[: int(cfg.get("max_searches_per_run", 24))]

        video_ids: dict[str, str] = {}   # id -> region it surfaced in
        for region, query in pairs:
            data = self.get_json(
                f"{API}/search",
                cost=self.SEARCH_COST,
                params={
                    "key": key,
                    "part": "id",
                    "q": query,
                    "type": "video",
                    "videoDuration": "short",       # < 4 min, reel-shaped
                    "order": "viewCount",
                    "publishedAfter": published_after,
                    "regionCode": region,
                    "relevanceLanguage": "en",
                    "maxResults": min(int(cfg.get("results_per_search", 50)), 50),
                },
            )
            for item in data.get("items", []):
                vid = (item.get("id") or {}).get("videoId")
                if vid:
                    video_ids.setdefault(vid, region)

        log.info("[youtube] %d unique videos from %d searches", len(video_ids), len(pairs))
        if not video_ids:
            return []

        return self._hydrate(key, video_ids, float(cfg.get("max_duration_s", 180)))

    def search_keyword(self, queries: list[str], *, regions: list[str],
                       days: int = 7, order: str = "viewCount",
                       max_searches: int = 12) -> list[Candidate]:
        """Ad-hoc keyword search across regions.

        Quota note worth understanding before you widen this: every
        `search.list` call costs 100 units against a 10,000/day budget. Four
        query variants across six regions is 24 calls = 2,400 units, so roughly
        four keyword searches a day sit comfortably alongside the daily
        snapshot. The CLI caps `variants x regions` rather than letting a broad
        search silently burn the day's quota and leave the cron job dry.
        """
        key = env("YOUTUBE_API_KEY")
        if not key or key.startswith("your_"):
            log.warning("[youtube] YOUTUBE_API_KEY not set — keyword search "
                        "cannot run without it")
            return []

        published_after = (datetime.now(timezone.utc) - timedelta(days=days)) \
            .replace(microsecond=0).isoformat().replace("+00:00", "Z")

        pairs = [(region, query) for query in queries for region in regions]
        pairs = pairs[:max_searches]

        video_ids: dict[str, str] = {}
        for region, query in pairs:
            data = self.get_json(
                f"{API}/search",
                cost=self.SEARCH_COST,
                # Ad-hoc searches spend from the unreserved portion only, so a
                # burst of them cannot starve the scheduled daily snapshot.
                respect_reserve=True,
                params={
                    "key": key, "part": "id", "q": query, "type": "video",
                    "videoDuration": "short", "order": order,
                    "publishedAfter": published_after, "regionCode": region,
                    "maxResults": 50,
                },
            )
            for item in data.get("items", []):
                vid = (item.get("id") or {}).get("videoId")
                if vid:
                    video_ids.setdefault(vid, region)

        log.info("[youtube] keyword search: %d unique videos from %d calls "
                 "(%d quota units)", len(video_ids), len(pairs), len(pairs) * 100)
        if not video_ids:
            return []

        max_duration = float(self.config.get("max_duration_s", 180))
        return self._hydrate(key, video_ids, max_duration)

    def _hydrate(self, key: str, video_ids: dict[str, str],
                 max_duration: float) -> list[Candidate]:
        """videos.list in batches of 50 — 1 quota unit per batch."""
        out: list[Candidate] = []
        ids = list(video_ids)
        for start in range(0, len(ids), 50):
            batch = ids[start:start + 50]
            data = self.get_json(
                f"{API}/videos",
                cost=self.VIDEOS_COST,
                params={
                    "key": key,
                    "part": "snippet,statistics,contentDetails",
                    "id": ",".join(batch),
                },
            )
            for item in data.get("items", []):
                snippet = item.get("snippet", {})
                stats = item.get("statistics", {})
                details = item.get("contentDetails", {})
                duration = parse_duration(details.get("duration", ""))
                if duration is None or duration > max_duration:
                    continue

                tags = snippet.get("tags") or []
                description = snippet.get("description", "") or ""
                hashtags = re.findall(r"#(\w+)", f"{snippet.get('title','')} {description}")

                out.append(Candidate(
                    platform="youtube",
                    platform_id=item["id"],
                    url=f"https://www.youtube.com/shorts/{item['id']}",
                    title=snippet.get("title", ""),
                    caption=description[:2000],
                    creator=snippet.get("channelTitle", ""),
                    creator_id=snippet.get("channelId", ""),
                    published_at=snippet.get("publishedAt"),
                    duration_s=duration,
                    views=int(stats["viewCount"]) if "viewCount" in stats else None,
                    likes=int(stats["likeCount"]) if "likeCount" in stats else None,
                    comments=int(stats["commentCount"]) if "commentCount" in stats else None,
                    meta={
                        "region": video_ids.get(item["id"], ""),
                        "tags": tags[:20],
                        "hashtags": hashtags[:20],
                        "thumbnail": (snippet.get("thumbnails", {})
                                      .get("high", {}).get("url", "")),
                        "lang": snippet.get("defaultAudioLanguage", ""),
                    },
                    source="youtube_data_api_v3",
                ))
        return out
