"""Instagram Business Discovery — real view counts for other people's reels.

This is the only official, free route to `view_count` on media you do not own.
`business_discovery.username({handle})` returns another **professional**
account's public media with `like_count`, `comments_count` and `view_count`.

That makes it the single most valuable Instagram endpoint in this project, and
also the most constrained:

  * the target must be a Business or Creator account — personal accounts return
    an error, and most personal accounts are exactly the ones that go viral by
    accident;
  * you must name the account. There is no "find me popular accounts" call, so
    this works from a **watchlist you curate**, not from open discovery;
  * `view_count` is null for photos and can be null on older media.

So the honest division of labour across this codebase is:

    hashtag search      → open Instagram discovery, engagement-scale only
    business discovery  → real Instagram view counts, watchlist-scoped
    YouTube Shorts      → view counts at global scale, for cross-posted clips

None of the three is sufficient alone. Together they cover most of what is
actually knowable for free.
"""
from __future__ import annotations

import logging

from ..config import env
from ..models import Candidate
from .base import Collector

log = logging.getLogger("reelpulse")

API = "https://graph.facebook.com/v23.0"

MEDIA_FIELDS = ("id,caption,media_type,media_product_type,permalink,timestamp,"
                "like_count,comments_count,view_count")
ACCOUNT_FIELDS = "username,name,followers_count,media_count"


class InstagramDiscoveryCollector(Collector):
    name = "instagram_discovery"
    service = "instagram_graph"   # shares the app-level hourly budget
    requires = ["IG_ACCESS_TOKEN", "IG_USER_ID"]

    def collect(self) -> list[Candidate]:
        return self.discover(self.config.get("watchlist", []))

    def discover(self, handles: list[str], limit: int | None = None
                 ) -> list[Candidate]:
        token, user_id = env("IG_ACCESS_TOKEN"), env("IG_USER_ID")
        if not token or not user_id or token.startswith("your_"):
            log.info("[instagram_discovery] no token — skipping")
            return []

        handles = [h.strip().lstrip("@") for h in handles if h and h.strip()]
        if not handles:
            log.info("[instagram_discovery] empty watchlist — add handles under "
                     "instagram_discovery.watchlist in config/sources.yaml")
            return []

        limit = limit or int(self.config.get("limit", 25))
        out: list[Candidate] = []
        unreachable: list[str] = []

        for handle in handles:
            field = (f"business_discovery.username({handle})"
                     f"{{{ACCOUNT_FIELDS},media.limit({limit})"
                     f"{{{MEDIA_FIELDS}}}}}")
            try:
                data = self.get_json(f"{API}/{user_id}",
                                     params={"fields": field, "access_token": token},
                                     retries=2)
            except Exception as exc:  # noqa: BLE001
                # Overwhelmingly this means "not a professional account".
                # Collected and reported once at the end rather than as a wall
                # of warnings, because a watchlist of 40 will always have some.
                unreachable.append(handle)
                log.debug("[instagram_discovery] %s unreachable: %s", handle, exc)
                continue

            account = data.get("business_discovery") or {}
            out.extend(self._parse(account, handle))

        if unreachable:
            log.warning("[instagram_discovery] %d/%d handles unreachable "
                        "(usually personal, not professional, accounts): %s",
                        len(unreachable), len(handles), ", ".join(unreachable[:8]))

        with_views = sum(1 for c in out if c.views)
        log.info("[instagram_discovery] %d reels from %d accounts, %d with real "
                 "view counts", len(out), len(handles) - len(unreachable), with_views)
        return out

    def _parse(self, account: dict, handle: str) -> list[Candidate]:
        media = (account.get("media") or {}).get("data") or []
        followers = account.get("followers_count")
        out: list[Candidate] = []

        for item in media:
            if item.get("media_type") != "VIDEO":
                continue

            permalink = item.get("permalink") or ""
            shortcode = permalink.rstrip("/").split("/")[-1] if permalink else ""
            caption = item.get("caption") or ""
            views = item.get("view_count")

            out.append(Candidate(
                platform="instagram",
                platform_id=shortcode or item["id"],
                url=permalink,
                title=caption[:180],
                caption=caption,
                creator=account.get("username", handle),
                creator_id=handle,
                published_at=item.get("timestamp"),
                views=int(views) if views not in (None, "") else None,
                likes=item.get("like_count"),
                comments=item.get("comments_count"),
                meta={
                    "discovered_via": f"business_discovery:{handle}",
                    "followers_count": followers,
                    "media_product_type": item.get("media_product_type"),
                    "ig_media_id": item.get("id"),
                    "instagram_native": True,
                    # Views relative to audience size separates a genuine
                    # breakout from a big account posting as usual.
                    "reach_multiple": (round(int(views) / followers, 2)
                                       if views and followers else None),
                },
                source="instagram_business_discovery",
            ))
        return out
