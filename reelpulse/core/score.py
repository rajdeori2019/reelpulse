"""The Viral Velocity Score.

The honest framing: nobody outside Meta can rank Instagram Reels by true global
view count, because Meta does not publish that number for content you do not
own. Any product claiming otherwise is scraping. So ReelPulse does not pretend
to reproduce a number it cannot see — it builds a defensible *estimator* out of
signals that are free and public, and shows its working.

VVS = sum over components of w_c * z(component_c)

Z-scoring inside the weekly pool is what makes the weights interpretable: every
component arrives on the same scale, so `velocity: 1.6` really does mean
"velocity matters 1.6x as much as magnitude", and retuning is a config edit
rather than a rewrite.

Measurement basis. Not every reel publishes the same numbers: YouTube Shorts
gives view counts, Instagram Business Discovery gives view counts for
professional accounts, and Instagram Hashtag Search gives likes and comments but
no views at all. Scale-dependent components are therefore computed from
whichever metric exists and standardised WITHIN that basis, so an
engagement-measured reel competes against other engagement-measured reels rather
than carrying a permanent handicap for a gap in Meta's API.

Two guardrails run after weighting:
  * clusters with neither views nor engagement get a fixed penalty, because
    their size is genuinely unknowable rather than merely small;
  * a creator appearing repeatedly in the same week is progressively damped, so
    one prolific aggregator account cannot own the entire board.
"""
from __future__ import annotations

import logging
from collections import defaultdict

import numpy as np

from ..config import load_weights, _load
from ..models import Cluster
from .features import craft_features, performance_features

log = logging.getLogger("reelpulse")

COMPONENTS = [
    "magnitude", "velocity", "acceleration", "breadth",
    "engagement_quality", "share_ratio", "topic_momentum", "recency",
]

# Components whose raw value depends on WHICH metric was available (views for
# most clips, likes+comments for Instagram-native ones). These are standardised
# within their own measurement basis, never across bases — see cohort_zscore.
SCALE_DEPENDENT = {"magnitude", "velocity", "acceleration", "share_ratio"}


def zscore(values: list[float]) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
    std = arr.std()
    if std < 1e-9:
        return np.zeros_like(arr)
    return (arr - arr.mean()) / std


def cohort_zscore(values: list[float], cohorts: list[str],
                  min_cohort: int = 3) -> np.ndarray:
    """Standardise each measurement basis against its own peers.

    Likes run roughly 3-8% of views, so log10(likes) sits about 1.2 below
    log10(views) for the very same reel. Pooling both into one distribution
    would hand every Instagram-native clip a permanent ~1.2 sigma handicap that
    reflects Meta's API surface, not the reel's performance.

    Standardising within basis asks the only question that is actually
    answerable: "how big is this clip *relative to clips measured the same
    way*". A reel in the 90th percentile of engagement-measured clips and one in
    the 90th percentile of view-measured clips then score alike, which is the
    correct comparison to make when the underlying units differ.

    Cohorts smaller than `min_cohort` cannot support a standard deviation, so
    they fall back to the pooled z-score rather than collapsing to zero.
    """
    arr = np.nan_to_num(np.asarray(values, dtype=float),
                        nan=0.0, posinf=0.0, neginf=0.0)
    pooled = zscore(list(arr))
    out = pooled.copy()

    for cohort in set(cohorts):
        idx = [i for i, c in enumerate(cohorts) if c == cohort]
        if len(idx) < min_cohort:
            continue                      # keep the pooled value
        sub = zscore([arr[i] for i in idx])
        for position, i in enumerate(idx):
            out[i] = sub[position]
    return out


def winsorize(values: np.ndarray, limit: float = 3.5) -> np.ndarray:
    """Clip extreme z-scores.

    One 400M-view clip in a pool of forty would otherwise flatten every other
    component to noise. Clipping at 3.5 sigma keeps the outlier at the top —
    where it belongs — without letting it erase the rest of the ranking.
    """
    return np.clip(values, -limit, limit)


def score_clusters(clusters: list[Cluster], prior_lookup,
                   momentum_fn=None, weights: dict[str, float] | None = None
                   ) -> list[Cluster]:
    if not clusters:
        return []

    weights = weights or load_weights()
    penalties = (_load("weights.yaml").get("penalties") or {})

    # ---- raw features --------------------------------------------------
    for cluster in clusters:
        cluster.features = performance_features(cluster, prior_lookup)
        cluster.tags = craft_features(cluster)
        if momentum_fn:
            momentum, terms = momentum_fn(cluster.primary.text)
            cluster.features["topic_momentum"] = float(momentum)
            cluster.tags["entities"] = terms
        else:
            cluster.features["topic_momentum"] = 0.0
            cluster.tags["entities"] = []

    # ---- standardise each component ------------------------------------
    # Scale-dependent components are standardised within measurement basis;
    # everything else (ratios, counts, decay terms) is already unit-comparable
    # and pools across the whole week.
    bases = [c.features.get("measurement_basis", "none") for c in clusters]
    z: dict[str, np.ndarray] = {}
    for component in COMPONENTS:
        raw = [c.features.get(component, 0.0) for c in clusters]
        z[component] = winsorize(
            cohort_zscore(raw, bases) if component in SCALE_DEPENDENT
            else zscore(raw))

    # ---- weighted sum --------------------------------------------------
    for i, cluster in enumerate(clusters):
        components = {c: float(z[c][i]) for c in COMPONENTS}
        total = sum(weights.get(c, 0.0) * v for c, v in components.items())

        basis = cluster.features.get("measurement_basis", "none")
        if basis == "none":
            # Neither views nor engagement: the size really is unknowable.
            penalty = float(penalties.get("unmeasured_magnitude", -0.5))
            total += penalty
            components["_penalty_unmeasured"] = penalty

        cluster.components = components
        cluster.vvs = total

    # ---- creator diversity damping -------------------------------------
    repeat_penalty = float(penalties.get("creator_repeat", -0.35))
    if repeat_penalty:
        seen: dict[str, int] = defaultdict(int)
        for cluster in sorted(clusters, key=lambda c: c.vvs, reverse=True):
            creator = (cluster.primary.creator or "").strip().lower()
            if not creator:
                continue
            seen[creator] += 1
            if seen[creator] > 2:
                hit = repeat_penalty * (seen[creator] - 2)
                cluster.vvs += hit
                cluster.components["_penalty_creator_repeat"] = hit

    ranked = sorted(clusters, key=lambda c: c.vvs, reverse=True)
    for position, cluster in enumerate(ranked, start=1):
        cluster.rank = position
    return ranked


def explain(cluster: Cluster, weights: dict[str, float] | None = None,
            top_k: int = 4) -> list[str]:
    """Plain-English reasons this cluster scored where it did.

    Ordered by each component's actual contribution (weight x z), not by weight
    alone — so the explanation reflects this specific reel, not the config file.
    """
    weights = weights or load_weights()
    contributions = {
        name: weights.get(name, 0.0) * value
        for name, value in cluster.components.items()
        if not name.startswith("_")
    }
    ordered = sorted(contributions.items(), key=lambda kv: abs(kv[1]), reverse=True)

    basis = cluster.features.get("measurement_basis", "views")
    phrasing = {
        "magnitude": ("sheer view count" if basis == "views"
                      else "total likes and comments (Instagram publishes no "
                           "view count for this reel)"),
        "velocity": ("views accumulated per hour since posting" if basis == "views"
                     else "engagement accumulated per hour since posting"),
        "acceleration": ("its view rate was still climbing at last check" if basis == "views"
                         else "its engagement rate was still climbing at last check"),
        "breadth": "the same clip appears on {n} platforms",
        "engagement_quality": "unusually high comments-per-view",
        "share_ratio": "heavy off-platform resharing",
        "topic_momentum": "it rode a topic that was spiking globally",
        "recency": "posted very recently in the window",
    }

    lines: list[str] = []
    for name, contribution in ordered[:top_k]:
        if abs(contribution) < 0.05:
            continue
        text = phrasing.get(name, name)
        if "{n}" in text:
            text = text.format(n=int(cluster.features.get("breadth", 1)))
        direction = "boosted by" if contribution > 0 else "held back by"
        lines.append(f"{direction} {text} ({contribution:+.2f})")

    if cluster.components.get("_penalty_unmeasured"):
        lines.append("penalised: no verifiable view count on any platform")
    if cluster.components.get("_penalty_creator_repeat"):
        lines.append("damped: this creator already appears higher in the week")
    return lines
