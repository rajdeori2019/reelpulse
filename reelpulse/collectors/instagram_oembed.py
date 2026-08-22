"""Instagram oEmbed — enrichment, not measurement.

Since Meta's 15 June 2026 change, `instagram_oembed` works with no access token
and no App Review. It confirms a reel URL is live and public and hands back an
official embed block, which is what makes the dashboard show the actual reel
instead of a dead link.

What it does NOT return: view counts, like counts, or author name (Meta removed
`author_name` and the thumbnail fields). Anyone promising you global Reels view
counts from a free API is either scraping or reselling scraped data. ReelPulse
does neither, and is honest that its magnitude signal comes from the Shorts
mirror instead.
"""
from __future__ import annotations

import logging

from ..limits import QuotaExhausted, ServiceCoolingDown
from ..models import Candidate
from .base import Collector

log = logging.getLogger("reelpulse")


class InstagramOEmbedCollector(Collector):
    """Enricher: takes existing instagram Candidates and fills in embed data."""

    name = "instagram_oembed"
    service = "instagram_oembed"
    requires: list[str] = []      # tokenless

    def collect(self) -> list[Candidate]:
        # This collector never discovers on its own; see enrich().
        return []

    def enrich(self, candidates: list[Candidate]) -> int:
        if not self.enabled:
            return 0
        targets = [c for c in candidates
                   if c.platform == "instagram" and "embed_html" not in c.meta]
        budget = int(self.config.get("requests_per_run", 60))
        endpoint = self.config.get(
            "endpoint", "https://graph.facebook.com/v25.0/instagram_oembed")

        enriched = 0
        for cand in targets[:budget]:
            try:
                data = self.get_json(endpoint, params={
                    "url": cand.url,
                    "omitscript": "true",
                    "hidecaption": "false",
                }, retries=2, timeout=15)
            except (QuotaExhausted, ServiceCoolingDown):
                # Stop the whole enrichment pass rather than grinding through
                # the remaining URLs generating refusals. Embeds are cosmetic;
                # the run is fine without them.
                log.info("[instagram_oembed] budget/cooldown reached — stopping "
                         "enrichment at %d of %d", enriched, len(targets))
                break
            except Exception as exc:  # noqa: BLE001
                # 404 here usually means deleted / private / age-gated. That is
                # itself information: mark it so the scorer can down-rank.
                cand.meta["oembed_status"] = "unavailable"
                cand.meta["oembed_error"] = str(exc)[:200]
                continue

            cand.meta["oembed_status"] = "ok"
            cand.meta["embed_html"] = data.get("html", "")
            cand.meta["embed_width"] = data.get("width")
            cand.meta["embed_type"] = data.get("type")
            if data.get("title") and not cand.caption:
                cand.caption = data["title"]
            if data.get("author_name") and not cand.creator:
                cand.creator = data["author_name"]
            enriched += 1

        log.info("[instagram_oembed] enriched %d/%d reels", enriched, len(targets))
        return enriched
