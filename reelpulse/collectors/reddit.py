"""Reddit — the off-platform share signal.

A clip that only circulates inside one app is popular. A clip that people
bother to *re-upload somewhere else* is viral. Reddit is the cheapest legal
window onto that second thing, and it is also where Instagram Reel permalinks
surface in the open, which is how ReelPulse gets real instagram.com/reel/ URLs
without ever touching Instagram's HTML.

Free tier: ~100 queries/minute with OAuth (client credentials, no user login).
A run uses one token request plus one listing per subreddit.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests

from ..config import env
from ..models import Candidate
from .base import Collector, USER_AGENT

log = logging.getLogger("reelpulse")

REEL_RE = re.compile(r"instagram\.com/(?:reel|reels|p)/([A-Za-z0-9_-]{5,})")
SHORTS_RE = re.compile(r"youtube\.com/shorts/([A-Za-z0-9_-]{5,})")


class RedditCollector(Collector):
    name = "reddit"
    service = "reddit"
    requires = ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"]

    def _token(self) -> str | None:
        cid, secret = env("REDDIT_CLIENT_ID"), env("REDDIT_CLIENT_SECRET")
        if not cid or not secret or cid.startswith("your_"):
            return None
        # The token exchange is a real Reddit request and counts against the
        # same per-minute budget as everything else. It used to bypass the
        # limiter, which meant a tight retry loop could hammer the auth endpoint
        # — the request most likely to get an app blocked.
        self.limiter.acquire(self.service, 1.0)
        resp = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(cid, secret),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        body = {}
        try:
            body = resp.json()
        except ValueError:
            pass
        self.limiter.observe(self.service, resp.status_code, resp.headers,
                             body, 1.0, "access_token")
        resp.raise_for_status()
        return body.get("access_token")

    def search(self, query: str, *, time_filter: str = "week",
               limit: int = 100) -> list[Candidate]:
        """Site-wide Reddit search for Instagram/Shorts links about a keyword.

        This is the one place ReelPulse can find Instagram permalinks for a
        specific topic: Meta exposes no search over other people's Reels, but
        people post reel links to Reddit with topical titles, and that is public
        and officially queryable.
        """
        token = self._token()
        if not token:
            return []

        headers = {"Authorization": f"bearer {token}", "User-Agent": USER_AGENT}
        out: list[Candidate] = []

        # Two passes: general search, then link-domain search. The domain query
        # catches posts whose title never mentions the keyword but whose linked
        # reel is on-topic; relevance filtering downstream removes the misses.
        searches = [
            {"q": query, "sort": "top", "t": time_filter, "limit": limit},
            {"q": f"{query} site:instagram.com", "sort": "top",
             "t": time_filter, "limit": limit},
        ]

        for params in searches:
            try:
                data = self.get_json("https://oauth.reddit.com/search",
                                     params=params, headers=headers, retries=2)
            except Exception as exc:  # noqa: BLE001
                log.info("[reddit] search pass failed (%s)", exc)
                continue
            out.extend(self._parse_listing(data, subreddit="search"))

        log.info("[reddit] keyword search found %d linked clips", len(out))
        return out

    def collect(self) -> list[Candidate]:
        token = self._token()
        if not token:
            log.info("[reddit] no credentials — skipping share signal")
            return []

        headers = {"Authorization": f"bearer {token}", "User-Agent": USER_AGENT}
        cfg = self.config
        out: list[Candidate] = []

        for sub in cfg.get("subreddits", []):
            data = self.get_json(
                f"https://oauth.reddit.com/r/{sub}/{cfg.get('listing', 'top')}",
                params={"t": cfg.get("time_filter", "week"),
                        "limit": int(cfg.get("limit", 100))},
                headers=headers,
            )
            out.extend(self._parse_listing(data, subreddit=sub))
        return out

    def _parse_listing(self, data: dict, *, subreddit: str) -> list[Candidate]:
        """Extract Instagram reel and YouTube Shorts links from a Reddit listing.

        Shared by the subreddit sweep and keyword search so both produce
        identically shaped Candidates — a divergence here would mean search
        results score differently from leaderboard results for no good reason.
        """
        out: list[Candidate] = []
        for child in (data.get("data", {}).get("children", []) or []):
            post = child.get("data", {})
            blob = " ".join(filter(None, [
                post.get("url", ""), post.get("url_overridden_by_dest", ""),
                post.get("selftext", ""), post.get("title", ""),
            ]))

            reel = REEL_RE.search(blob)
            short = SHORTS_RE.search(blob)
            if not reel and not short:
                continue

            created = datetime.fromtimestamp(
                post.get("created_utc", 0), tz=timezone.utc)
            shared = {
                "reddit_score": int(post.get("score", 0)),
                "reddit_comments": int(post.get("num_comments", 0)),
                "subreddit": post.get("subreddit", subreddit),
                "reddit_permalink": "https://reddit.com" + post.get("permalink", ""),
                "reddit_title": post.get("title", ""),
            }

            if reel:
                shortcode = reel.group(1)
                out.append(Candidate(
                    platform="instagram",
                    platform_id=shortcode,
                    url=f"https://www.instagram.com/reel/{shortcode}/",
                    title=post.get("title", ""),
                    creator="",
                    published_at=created,   # proxy: when it hit Reddit
                    # Reddit score is a *share* signal, deliberately not
                    # mapped to `views` — we never invent a view count.
                    shares=shared["reddit_score"],
                    comments=shared["reddit_comments"],
                    meta={**shared, "published_at_is_proxy": True},
                    source="reddit_oauth",
                ))
            if short:
                out.append(Candidate(
                    platform="reddit",
                    platform_id=f"{post.get('id')}",
                    url="https://reddit.com" + post.get("permalink", ""),
                    title=post.get("title", ""),
                    published_at=created,
                    shares=shared["reddit_score"],
                    comments=shared["reddit_comments"],
                    meta={**shared, "youtube_id": short.group(1)},
                    source="reddit_oauth",
                ))
        return out
