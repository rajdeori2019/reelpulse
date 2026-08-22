"""Did anything here actually go viral?

A ranking always produces a #1. That is its job, and it is also its most
dangerous property: rank a set of clips whose best member has 34,000 views and
whose third member has *two*, and the output looks exactly like a leaderboard of
hits. The first real keyword search on this tool did precisely that — "career
guidance" over 7 days returned 75 clips, ranked them confidently, and put a
2-view video at #3.

Rank is relative. Reach is absolute. Reporting rank without reach is how a
search for a quiet niche gets mistaken for a discovery.

So every result is also placed against the stored background pool — the clips
the weekly run already collected — and the whole result set gets a verdict. If
nothing clears the middle of that distribution, the report says so plainly
instead of dressing up noise as a top 10.

The absolute floor is deliberately crude and deliberately low. It exists to
catch the case anchoring cannot: a search run before any history exists, where
percentiles are meaningless but two views is still two views.
"""
from __future__ import annotations

from typing import Any

# Percentile of the background pool -> label. Ordered high to low.
TIERS: list[tuple[float, str]] = [
    (0.95, "viral"),
    (0.75, "strong"),
    (0.50, "moderate"),
    (0.25, "low"),
    (0.00, "negligible"),
]

# Below this, nothing is called viral regardless of context. A clip cannot be a
# meaningful find on a few hundred views, however thin the field it won.
ABSOLUTE_FLOOR = 5_000.0

# The verdict turns negative when nothing reaches this tier.
VERDICT_TIER = "moderate"
_ORDER = {label: i for i, (_, label) in enumerate(reversed(TIERS))}


def percentile_of(scale: float, background: list[float]) -> float | None:
    """Fraction of background clips this one exceeds. None without a pool."""
    if not background:
        return None
    below = sum(1 for value in background if value < scale)
    return below / len(background)


def tier_for(scale: float, background: list[float],
             floor: float = ABSOLUTE_FLOOR) -> tuple[str, float | None]:
    """(label, percentile). The absolute floor overrides the percentile.

    Overriding matters: in a pool of uniformly tiny clips, a slightly less tiny
    clip sits at the 99th percentile and would otherwise be labelled 'viral'.
    """
    pct = percentile_of(scale, background)
    if scale < floor:
        return "negligible", pct
    if pct is None:
        # No history to compare against. Clearing the floor is all we can say.
        return "unrated", None
    for threshold, label in TIERS:
        if pct >= threshold:
            return label, pct
    return "negligible", pct


def assess(scales: list[float], background: list[float],
           floor: float = ABSOLUTE_FLOOR) -> dict[str, Any]:
    """Reach verdict for a whole result set."""
    if not scales:
        return {"verdict": "empty", "headline": "No results.",
                "best_tier": None, "tiers": {}, "anchored": bool(background)}

    labelled = [tier_for(s, background, floor) for s in scales]
    tiers = [label for label, _ in labelled]
    counts: dict[str, int] = {}
    for label in tiers:
        counts[label] = counts.get(label, 0) + 1

    best = max(tiers, key=lambda t: _ORDER.get(t, -1))
    best_scale = max(scales)
    anchored = bool(background)

    if not anchored:
        cleared = sum(1 for s in scales if s >= floor)
        return {
            "verdict": "unrated" if cleared else "nothing_viral",
            "anchored": False,
            "best_tier": "unrated" if cleared else "negligible",
            "best_scale": best_scale,
            "tiers": counts,
            "headline": (
                f"No stored history to compare against, so these results cannot "
                f"be placed on a scale. {cleared} of {len(scales)} clear a basic "
                f"floor of {floor:,.0f}."
                if cleared else
                f"Nothing here reached even {floor:,.0f} — the biggest result has "
                f"{best_scale:,.0f}. This is not a leaderboard of hits."),
        }

    passes = _ORDER.get(best, -1) >= _ORDER[VERDICT_TIER]
    return {
        "verdict": "ok" if passes else "nothing_viral",
        "anchored": True,
        "best_tier": best,
        "best_scale": best_scale,
        "best_percentile": round((percentile_of(best_scale, background) or 0) * 100, 1),
        "background_clips": len(background),
        "tiers": counts,
        "headline": (
            f"Top result sits in the {percentile_of(best_scale, background) * 100:.0f}th "
            f"percentile of the {len(background)} clips already collected "
            f"({best} reach)."
            if passes else
            f"**Nothing matching this search went meaningfully viral.** The best "
            f"result ({best_scale:,.0f}) is below the median of the "
            f"{len(background)} clips already collected. These are ranked "
            f"relative to each other, not because any of them are hits — try a "
            f"wider --days window, a broader term, or accept that this niche was "
            f"quiet."),
    }
