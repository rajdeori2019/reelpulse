"""Instagram Graph API — your own account, real numbers.

This is the honest half of the hybrid. Everything else in ReelPulse estimates;
this returns Meta's own figures, for free, under an official API — but only for
Instagram Business or Creator accounts you control.

Two jobs:
  1. Ground truth for `reelpulse calibrate`, which fits the VVS weights against
     view counts you can actually verify.
  2. Your personal benchmark line on the dashboard, so the top 10 is not just
     trivia — it is a gap you can measure yourself against.

Metric names follow Meta's April 2025 consolidation: `impressions`, `plays` and
`video_views` are gone; `views` is the single replacement.
"""
from __future__ import annotations

import logging

from ..config import env
from .base import Collector

log = logging.getLogger("reelpulse")

# graph.facebook.com, NOT graph.instagram.com.
#
# Meta ships two incompatible Instagram APIs:
#   graph.instagram.com  — "Instagram API with Instagram Login". No Facebook
#                          Page needed, but NO HASHTAG SEARCH.
#   graph.facebook.com   — "Instagram API with Facebook Login". Requires the
#                          account be linked to a Facebook Page, and is the only
#                          one that supports Hashtag Search.
#
# ReelPulse needs hashtag search, so the whole project is on the Facebook-Login
# path and every Instagram call must use the same host. Pointing own-media calls
# at graph.instagram.com while hashtag search used graph.facebook.com meant one
# token could never satisfy both.
API = "https://graph.facebook.com/v23.0"


class InstagramGraphCollector(Collector):
    name = "instagram_graph"
    service = "instagram_graph"
    requires = ["IG_ACCESS_TOKEN", "IG_USER_ID"]

    def collect(self) -> list:
        return []   # own media is stored separately, not ranked globally

    def fetch_own_media(self) -> list[dict]:
        token, user_id = env("IG_ACCESS_TOKEN"), env("IG_USER_ID")
        if not token or not user_id or token.startswith("your_"):
            log.info("[instagram_graph] no token — calibration will use defaults")
            return []
        if not self.enabled:
            return []

        cfg = self.config
        media = self.get_json(
            f"{API}/{user_id}/media",
            params={"fields": cfg.get("media_fields", "id,caption,permalink,timestamp"),
                    "limit": int(cfg.get("limit", 50)),
                    "access_token": token},
        )

        out: list[dict] = []
        for item in media.get("data", []):
            is_reel = item.get("media_product_type") == "REELS"
            metrics = list(cfg.get("insight_metrics", ["views", "reach"]))
            if is_reel:
                metrics += list(cfg.get("reel_only_metrics", []))

            values: dict[str, float] = {}
            try:
                insights = self.get_json(
                    f"{API}/{item['id']}/insights",
                    params={"metric": ",".join(metrics), "access_token": token},
                    retries=2,
                )
                for entry in insights.get("data", []):
                    vals = entry.get("values") or [{}]
                    values[entry["name"]] = vals[0].get("value", 0)
            except Exception as exc:  # noqa: BLE001
                # Insights 404 on media older than the retention window, or on
                # posts made before the account converted to Business. Normal.
                log.debug("[instagram_graph] insights unavailable for %s: %s",
                          item.get("id"), exc)

            out.append({
                "id": item["id"],
                "permalink": item.get("permalink"),
                "caption": item.get("caption", ""),
                "timestamp": item.get("timestamp"),
                "is_reel": is_reel,
                "metrics": {
                    **values,
                    "like_count": item.get("like_count"),
                    "comments_count": item.get("comments_count"),
                },
            })

        log.info("[instagram_graph] fetched %d own media (%d reels)",
                 len(out), sum(1 for m in out if m["is_reel"]))
        return out
