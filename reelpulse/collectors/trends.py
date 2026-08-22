"""Topic momentum — is the clip riding a wave, or making one?

Two free sources, both optional:

  * Wikipedia pageviews (default on). Official Wikimedia REST API, no key, no
    quota anxiety, and a startlingly good proxy for "the world suddenly cares
    about this person/thing". If a reel's dominant entity spiked on Wikipedia
    the same week, that reel is surfing.

  * Google Trends via pytrends (default off). Unofficial, rate-limited and
    liable to break without notice, so it is opt-in rather than a dependency
    you have to babysit.

Momentum matters because it separates two very different lessons. A reel that
went big on a news wave teaches you about timing. A reel that went big with flat
topic momentum teaches you about craft — and craft is the part you can repeat.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from .base import Collector

log = logging.getLogger("reelpulse")

STOPWORDS = set("""
a an the and or but if then than that this these those with without from into
onto for to of in on at by is are was were be been being do does did doing have
has had you your yours i me my we our they them he she it its as so not no yes
new best top viral shorts reel reels video watch full part like subscribe follow
what when where why how who which all can will just get got make made
""".split())


def entities(text: str, limit: int = 3) -> list[str]:
    """Cheap entity guess: capitalised multi-word runs, then frequent nouns.

    Not NER-grade, and deliberately so — a real NER model would add hundreds of
    megabytes for a signal that only needs to be directionally right.
    """
    proper = re.findall(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){0,2})\b", text or "")
    proper = [p for p in proper if p.lower() not in STOPWORDS]
    if proper:
        return [p for p, _ in Counter(proper).most_common(limit)]

    words = [w for w in re.findall(r"[a-zA-Z]{4,}", (text or "").lower())
             if w not in STOPWORDS]
    return [w for w, _ in Counter(words).most_common(limit)]


class TopicMomentumCollector(Collector):
    name = "trends"
    service = "wikimedia"

    def __init__(self, config: dict, wiki_config: dict | None = None,
                 limiter=None) -> None:
        super().__init__(config, limiter)
        self.wiki = wiki_config or {}
        self._cache: dict[str, float] = {}

    def collect(self) -> list:
        return []

    # ---- Wikipedia -----------------------------------------------------

    def _wiki_slope(self, term: str) -> float:
        """Normalised slope of daily pageviews over the last 14 days.

        Returns roughly -1..+3. 0 means flat or unknown.
        """
        if term in self._cache:
            return self._cache[term]
        if not self.wiki.get("enabled", True):
            return 0.0

        end = datetime.now(timezone.utc) - timedelta(days=1)
        start = end - timedelta(days=13)
        article = term.strip().replace(" ", "_")
        url = (f"{self.wiki.get('endpoint')}/"
               f"{self.wiki.get('project', 'en.wikipedia.org')}/all-access/user/"
               f"{article}/daily/{start:%Y%m%d}/{end:%Y%m%d}")

        score = 0.0
        try:
            data = self.get_json(url, retries=1, timeout=12)
            series = [int(i.get("views", 0)) for i in data.get("items", [])]
            if len(series) >= 8:
                first = sum(series[:7]) / 7 or 1.0
                last = sum(series[-7:]) / 7
                score = max(min((last - first) / first, 3.0), -1.0)
        except Exception:  # noqa: BLE001 — no article, no signal, no problem
            score = 0.0

        self._cache[term] = score
        return score

    # ---- Google Trends (optional) --------------------------------------

    def _pytrends_slope(self, terms: list[str]) -> dict[str, float]:
        if not self.config.get("enabled", False) or not terms:
            return {}
        try:
            from pytrends.request import TrendReq  # noqa: PLC0415
        except ImportError:
            log.info("[trends] pytrends not installed — skipping Google Trends")
            return {}
        try:
            client = TrendReq(hl="en-US", tz=0)
            client.build_payload(terms[:5],
                                 timeframe=self.config.get("timeframe", "now 7-d"),
                                 geo=self.config.get("geo", ""))
            frame = client.interest_over_time()
            if frame.empty:
                return {}
            out = {}
            for term in terms[:5]:
                if term not in frame:
                    continue
                series = frame[term].tolist()
                half = max(len(series) // 2, 1)
                first = sum(series[:half]) / half or 1.0
                last = sum(series[half:]) / max(len(series) - half, 1)
                out[term] = max(min((last - first) / first, 3.0), -1.0)
            return out
        except Exception as exc:  # noqa: BLE001
            log.info("[trends] Google Trends unavailable (%s)", exc)
            return {}

    # ---- public --------------------------------------------------------

    def momentum_for(self, text: str) -> tuple[float, list[str]]:
        terms = entities(text)
        if not terms:
            return 0.0, []
        google = self._pytrends_slope(terms)
        scores = [google.get(t, self._wiki_slope(t)) for t in terms]
        return (max(scores) if scores else 0.0), terms
