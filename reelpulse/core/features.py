"""Turn a Cluster into the feature vector the scorer and the pattern miner share.

Two families of features:

  * PERFORMANCE features (views, velocity, breadth...) answer "how big".
    These feed the VVS.
  * CRAFT features (hook type, duration bucket, caption shape...) answer "why".
    These feed pattern mining and the recommender.

Keeping them in one place means a rule discovered by the miner is expressed in
exactly the vocabulary the recommender can act on — no translation layer where
meaning quietly leaks.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

from ..config import load_lexicons
from ..models import Cluster

LEX = load_lexicons()

EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]",
    flags=re.UNICODE,
)


# ---------------------------------------------------------------------------
# craft features
# ---------------------------------------------------------------------------

def duration_bucket(seconds: float | None) -> str:
    if seconds is None:
        return "unknown_duration"
    for low, high, label in LEX.get("duration_buckets", []):
        if low <= seconds < high:
            return label
    return "extended_60s_plus"


def detect_hooks(text: str) -> list[str]:
    """Every hook archetype whose trigger appears in the first ~120 chars.

    Front-loaded on purpose: a hook that only shows up at character 400 of a
    description was not the thing that stopped the scroll.
    """
    head = (text or "")[:120].lower()
    found = []
    for hook, triggers in (LEX.get("hooks") or {}).items():
        if any(trigger in head for trigger in triggers):
            found.append(hook)
    return found or ["none_detected"]


# Topic terms are compiled once at import rather than per call. Rebuilding ~130
# patterns for every clip cost ~3ms each, which is invisible on a demo and very
# visible on a real run of several thousand candidates.
_TOPIC_PATTERNS: dict[str, list[re.Pattern]] = {
    topic: [re.compile(rf"\b{re.escape(term)}\b") for term in terms]
    for topic, terms in (LEX.get("topics") or {}).items()
}


def detect_topic(text: str) -> str:
    """Word-boundary matched, deliberately.

    Naive substring matching classified "nothing matches here" as sports,
    because "match" is a sports term and "matches" contains it. Topic terms are
    single words, so they must match as words. Hook triggers are phrases and are
    matched as substrings on purpose.
    """
    lowered = (text or "").lower()
    scores: dict[str, int] = {}
    for topic, patterns in _TOPIC_PATTERNS.items():
        hits = sum(1 for pattern in patterns if pattern.search(lowered))
        if hits:
            scores[topic] = hits
    if not scores:
        return "uncategorised"
    return max(scores.items(), key=lambda kv: kv[1])[0]


def caption_shape(text: str) -> dict[str, Any]:
    text = text or ""
    hashtags = re.findall(r"#\w+", text)
    ctas = [c for c in (LEX.get("ctas") or []) if c in text.lower()]
    words = len(text.split())
    return {
        "caption_words": words,
        "caption_len_bucket": ("caption_none" if words == 0
                               else "caption_short_1_10w" if words <= 10
                               else "caption_medium_11_30w" if words <= 30
                               else "caption_long_30w_plus"),
        "hashtag_count": len(hashtags),
        "hashtag_bucket": ("tags_0" if not hashtags
                           else "tags_1_3" if len(hashtags) <= 3
                           else "tags_4_10" if len(hashtags) <= 10
                           else "tags_11_plus"),
        "emoji_count": len(EMOJI_RE.findall(text)),
        "has_emoji": bool(EMOJI_RE.search(text)),
        "has_question": "?" in text[:160],
        "has_cta": bool(ctas),
        "ctas": ctas,
        "has_number_in_hook": bool(re.search(r"\b\d+\b", text[:80])),
        "all_caps_words": len(re.findall(r"\b[A-Z]{3,}\b", text[:120])),
    }


def craft_features(cluster: Cluster) -> dict[str, Any]:
    primary = cluster.primary
    text = " ".join({m.text for m in cluster.members})
    shape = caption_shape(primary.caption or primary.title)

    hooks = detect_hooks(primary.title or primary.caption)
    regions = sorted({m.meta.get("region", "") for m in cluster.members
                      if m.meta.get("region")})

    return {
        "hooks": hooks,
        "primary_hook": hooks[0],
        "topic": detect_topic(text),
        "duration_s": primary.duration_s,
        "duration_bucket": duration_bucket(primary.duration_s),
        "platforms": sorted(cluster.platforms),
        "regions": regions,
        "region_count": len(regions),
        "creator": primary.creator,
        "has_instagram_mirror": cluster.instagram is not None,
        **shape,
    }


def craft_itemset(features: dict[str, Any]) -> set[str]:
    """The categorical view of a cluster, for association-rule mining.

    Only discrete, human-sayable facts go in — a rule you cannot repeat as an
    instruction is not worth mining.
    """
    items = {
        f"duration:{features['duration_bucket']}",
        f"caption:{features['caption_len_bucket']}",
        f"hashtags:{features['hashtag_bucket']}",
    }
    # "uncategorised topic" and "no hook detected" are absences of information,
    # not craft choices. Mining them produces rules nobody can act on
    # ("recommendation: make your content uncategorised"), so they never enter
    # the itemset.
    if features["primary_hook"] != "none_detected":
        items.add(f"hook:{features['primary_hook']}")
    if features["topic"] != "uncategorised":
        items.add(f"topic:{features['topic']}")
    if features.get("has_question"):
        items.add("caption:has_question")
    if features.get("has_cta"):
        items.add("caption:has_cta")
    if features.get("has_emoji"):
        items.add("caption:has_emoji")
    if features.get("has_number_in_hook"):
        items.add("hook:has_number")
    if features.get("all_caps_words", 0) >= 2:
        items.add("hook:shouty_caps")
    if features.get("region_count", 0) >= 3:
        items.add("reach:multi_region")
    if len(features.get("platforms", [])) >= 2:
        items.add("reach:cross_posted")
    for hook in features.get("hooks", [])[1:]:
        items.add(f"hook_secondary:{hook}")
    return items


# ---------------------------------------------------------------------------
# performance features
# ---------------------------------------------------------------------------

def performance_features(cluster: Cluster, prior_lookup) -> dict[str, float]:
    """`prior_lookup(fingerprint, at) -> row|None` supplies snapshot history."""
    primary = cluster.primary
    views = cluster.best_views
    age_h = primary.age_hours if primary.published_at else float("nan")

    likes = sum(m.likes or 0 for m in cluster.members)
    comments = sum(m.comments or 0 for m in cluster.members)
    shares = sum(m.shares or 0 for m in cluster.members)
    engagement_total = likes + 3 * comments

    # --- measurement basis --------------------------------------------
    # Instagram Hashtag Search returns other people's reels with like_count and
    # comments_count but NO view_count — Meta does not expose one on that edge.
    #
    # Every scale-dependent signal here (magnitude, velocity, acceleration) is
    # therefore computed from whichever metric IS published for a given clip,
    # and the basis is recorded. Deriving them from views alone would hand every
    # Instagram-native discovery a structural zero on the three most heavily
    # weighted components, punishing it for a gap in Meta's API rather than for
    # anything about the reel — and quietly collapsing the board back to
    # YouTube-only results.
    #
    # A view count is NEVER estimated from likes. Cross-basis comparability is
    # handled in score.py by standardising each basis within its own cohort,
    # which is honest; inventing a number would not be.
    if views:
        basis, scale = "views", float(views)
    elif engagement_total:
        basis, scale = "engagement", float(engagement_total)
    else:
        basis, scale = "none", 0.0

    velocity = scale / age_h if scale and age_h == age_h and age_h > 0 else 0.0

    # --- acceleration -------------------------------------------------
    # Rate now vs rate at the last snapshot >=8h old, on the same metric as the
    # basis. No history -> 0.0 (neutral), never a penalty.
    acceleration = 0.0
    prior = prior_lookup(primary.fingerprint, primary.collected_at)
    if prior and scale:
        if basis == "views":
            prior_scale = float(prior["views"] or 0)
        else:
            prior_scale = float((prior["likes"] or 0) + 3 * (prior["comments"] or 0))

        if prior_scale:
            prior_at = datetime.fromisoformat(prior["collected_at"])
            if prior_at.tzinfo is None:
                prior_at = prior_at.replace(tzinfo=timezone.utc)
            elapsed_h = max(
                (primary.collected_at - prior_at).total_seconds() / 3600.0, 1.0)
            recent_rate = max(scale - prior_scale, 0) / elapsed_h
            if velocity > 0:
                acceleration = math.log10(
                    max(recent_rate, 1.0) / max(velocity, 1.0) + 1e-9)

    # Engagement quality is a *ratio*, so it stays comparable across bases only
    # when a denominator exists. With views, it is engagement per view. Without,
    # the ratio is undefined — engagement already IS the scale metric, and
    # dividing it by itself would just return 1.0 for every such clip.
    engagement_quality = (engagement_total / views) if views else 0.0
    share_ratio = (shares / (views / 100_000)) if views else float(shares)

    age_days = (age_h / 24.0) if age_h == age_h else 7.0
    recency = math.exp(-age_days / 3.5)

    return {
        "views": float(views),
        "scale": scale,
        "magnitude": math.log10(scale + 1),
        "velocity": math.log10(velocity + 1),
        "acceleration": acceleration,
        "breadth": float(cluster.breadth),
        "engagement_quality": engagement_quality,
        "share_ratio": math.log10(share_ratio + 1),
        "recency": recency,
        "age_hours": age_h if age_h == age_h else -1.0,
        "likes": float(likes),
        "comments": float(comments),
        "shares": float(shares),
        "has_measured_views": 1.0 if views else 0.0,
        "measurement_basis": basis,
    }
