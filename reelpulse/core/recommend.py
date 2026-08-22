"""The recommender — the part that answers "so what do I post?"

Design commitment: **no unsourced advice**. Every recommendation this module
emits carries the rule it came from, that rule's lift, and how many reels it was
observed in. If a suggestion cannot cite evidence, it does not ship. Social
media tooling is full of confident, unfalsifiable advice; the point of building
this yourself is to not add to that pile.

Three entry points:

  plan(niche)          -> the highest-lift craft choices available in that niche
  predict(plan)        -> where a described reel would have landed this week
  next_best_change(p)  -> the single edit with the largest expected gain
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..config import load_weights
from .features import duration_bucket
from .patterns import confidence_label, summarise_rule


@dataclass
class ReelPlan:
    """A reel you are thinking about making, described in craft terms."""

    topic: str = "uncategorised"
    hook: str = "none_detected"
    duration_s: float = 15.0
    caption_words: int = 8
    hashtag_count: int = 3
    has_question: bool = False
    has_cta: bool = False
    has_emoji: bool = False
    has_number_in_hook: bool = False
    cross_posted: bool = False
    extras: dict[str, Any] = field(default_factory=dict)

    def itemset(self) -> set[str]:
        caption_bucket = ("caption_none" if self.caption_words == 0
                          else "caption_short_1_10w" if self.caption_words <= 10
                          else "caption_medium_11_30w" if self.caption_words <= 30
                          else "caption_long_30w_plus")
        tag_bucket = ("tags_0" if self.hashtag_count == 0
                      else "tags_1_3" if self.hashtag_count <= 3
                      else "tags_4_10" if self.hashtag_count <= 10
                      else "tags_11_plus")
        items = {
            f"hook:{self.hook}",
            f"topic:{self.topic}",
            f"duration:{duration_bucket(self.duration_s)}",
            f"caption:{caption_bucket}",
            f"hashtags:{tag_bucket}",
        }
        if self.has_question:
            items.add("caption:has_question")
        if self.has_cta:
            items.add("caption:has_cta")
        if self.has_emoji:
            items.add("caption:has_emoji")
        if self.has_number_in_hook:
            items.add("hook:has_number")
        if self.cross_posted:
            items.add("reach:cross_posted")
        return items


def _score_itemset(items: set[str], rules: list[dict]) -> tuple[float, list[dict]]:
    """Aggregate lift over every matching rule.

    Log-sum rather than product: overlapping rules share evidence, so
    multiplying their lifts would double-count the same underlying reels and
    manufacture confidence that is not there.
    """
    matched = [r for r in rules if set(r["antecedent"]) <= items]
    if not matched:
        return 0.0, []
    total = float(np.sum([np.log(r["lift"]) * np.sqrt(r["n"]) for r in matched]))
    normaliser = float(np.sum([np.sqrt(r["n"]) for r in matched])) or 1.0
    return total / normaliser, matched


def plan(rules: list[dict], niche: str | None = None, limit: int = 8
         ) -> list[dict[str, Any]]:
    """The strongest evidence-backed craft choices, optionally within a niche."""
    pool = rules
    if niche:
        topic_item = f"topic:{niche}"
        scoped = [r for r in rules if topic_item in r["antecedent"]]
        # Fall back to cross-niche rules when a niche is thin, but say so.
        pool = scoped if len(scoped) >= 3 else rules

    out = []
    for rule in sorted(pool, key=lambda r: (r["lift"], r["n"]), reverse=True)[:limit]:
        out.append({
            "recommendation": _imperative(rule),
            "evidence": summarise_rule(rule),
            "lift": round(rule["lift"], 2),
            "sample_size": rule["n"],
            "confidence": confidence_label(rule),
            "scoped_to_niche": bool(niche) and f"topic:{niche}" in rule["antecedent"],
        })
    return out


DURATION_PHRASE = {
    "ultra_short_0_7s": "keep it under 7 seconds",
    "short_7_15s": "keep it between 7 and 15 seconds",
    "mid_15_30s": "run 15 to 30 seconds",
    "long_30_60s": "run 30 to 60 seconds",
    "extended_60s_plus": "go past 60 seconds",
    "unknown_duration": "keep the runtime tight",
}

HASHTAG_PHRASE = {
    "tags_0": "post with no hashtags at all",
    "tags_1_3": "use just 1-3 hashtags",
    "tags_4_10": "use 4-10 hashtags",
    "tags_11_plus": "use 11 or more hashtags",
}

CAPTION_PHRASE = {
    "caption_none": "ship it with no caption",
    "caption_short_1_10w": "keep the caption under 10 words",
    "caption_medium_11_30w": "write an 11-30 word caption",
    "caption_long_30w_plus": "write a caption over 30 words",
}

REACH_PHRASE = {
    "cross_posted": "cross-post the same cut to Shorts and TikTok",
    "multi_region": "make it legible without language (it has to travel)",
}


def _imperative(rule: dict) -> str:
    """Turn a rule into an instruction someone can act on tomorrow.

    Ordered deliberately: the hook first, because that is the decision that has
    to be made before anything is shot; distribution last, because it is the
    decision made after.
    """
    order = {"hook": 0, "hook_secondary": 1, "topic": 2, "duration": 3,
             "caption": 4, "hashtags": 5, "reach": 6}
    items = sorted(rule["antecedent"], key=lambda i: order.get(i.partition(":")[0], 9))

    parts: list[str] = []
    scope = ""
    for item in items:
        kind, _, value = item.partition(":")
        readable = value.replace("_", " ")

        article = "an" if readable[:1].lower() in "aeiou" else "a"

        if kind == "hook" and value == "has_number":
            parts.append("put a number in the opening line")
        elif kind == "hook":
            parts.append(f"open on {article} {readable} hook")
        elif kind == "hook_secondary":
            parts.append(f"layer in {article} {readable} angle as well")
        elif kind == "topic":
            scope = f" (in {readable} content)"
        elif kind == "duration":
            parts.append(DURATION_PHRASE.get(value, f"run {readable}"))
        elif kind == "caption" and value == "has_question":
            parts.append("end the caption on a question")
        elif kind == "caption" and value == "has_cta":
            parts.append("include an explicit call to action")
        elif kind == "caption" and value == "has_emoji":
            parts.append("put an emoji in the caption")
        elif kind == "caption":
            parts.append(CAPTION_PHRASE.get(value, f"keep the caption {readable}"))
        elif kind == "hashtags":
            parts.append(HASHTAG_PHRASE.get(value, f"use {readable} hashtags"))
        elif kind == "reach":
            parts.append(REACH_PHRASE.get(value, readable))
        else:
            parts.append(readable)

    if not parts:
        return "No actionable instruction in this rule."
    sentence = parts[0][0].upper() + parts[0][1:]
    if len(parts) > 1:
        sentence += ", " + ", and ".join([", ".join(parts[1:-1]), parts[-1]]).strip(", ")
    return sentence + scope + "."


def predict(reel_plan: ReelPlan, rules: list[dict],
            clusters: list | None = None) -> dict[str, Any]:
    """Where this plan would have landed among the week's clips.

    This is a *relative positioning* estimate against observed data, not a view
    forecast. Anyone selling you a view forecast from public signals alone is
    selling you a number they cannot back.
    """
    score, matched = _score_itemset(reel_plan.itemset(), rules)

    percentile = None
    if clusters:
        peers = []
        for cluster in clusters:
            peer_score, _ = _score_itemset(
                set(cluster.tags.get("_itemset", [])) or set(), rules)
            peers.append(peer_score)
        if peers and any(peers):
            percentile = float((np.asarray(peers) < score).mean() * 100)

    return {
        "craft_score": round(score, 3),
        "percentile_vs_week": round(percentile, 1) if percentile is not None else None,
        "matched_rules": len(matched),
        "strongest_evidence": [summarise_rule(r) for r in
                               sorted(matched, key=lambda r: r["lift"], reverse=True)[:3]],
        "caveat": ("Relative craft positioning against this week's observed "
                   "clips. Not a view forecast."),
    }


def next_best_change(reel_plan: ReelPlan, rules: list[dict],
                     top_k: int = 3) -> list[dict[str, Any]]:
    """Greedy single-edit search: which one change buys the most lift?"""
    base, _ = _score_itemset(reel_plan.itemset(), rules)

    variants: list[tuple[str, ReelPlan]] = []

    for hook in {r.partition(":")[2] for rule in rules for r in rule["antecedent"]
                 if r.startswith("hook:") and r != "hook:has_number"}:
        if hook == reel_plan.hook:
            continue
        variant = ReelPlan(**{**reel_plan.__dict__, "hook": hook})
        variants.append((f"switch the hook to '{hook.replace('_', ' ')}'", variant))

    for seconds, label in ((6, "under 7s"), (11, "7-15s"),
                           (22, "15-30s"), (45, "30-60s")):
        if duration_bucket(seconds) == duration_bucket(reel_plan.duration_s):
            continue
        variant = ReelPlan(**{**reel_plan.__dict__, "duration_s": float(seconds)})
        variants.append((f"cut the runtime to {label}", variant))

    for words, label in ((0, "drop the caption entirely"),
                         (6, "trim the caption to ~6 words"),
                         (20, "expand the caption to ~20 words")):
        if words == reel_plan.caption_words:
            continue
        variant = ReelPlan(**{**reel_plan.__dict__, "caption_words": words})
        variants.append((label, variant))

    for count, label in ((0, "remove all hashtags"), (3, "use 1-3 hashtags"),
                         (7, "use 4-10 hashtags")):
        if count == reel_plan.hashtag_count:
            continue
        variant = ReelPlan(**{**reel_plan.__dict__, "hashtag_count": count})
        variants.append((label, variant))

    if not reel_plan.has_question:
        variants.append(("end the caption on a question",
                         ReelPlan(**{**reel_plan.__dict__, "has_question": True})))
    if not reel_plan.cross_posted:
        variants.append(("cross-post the same cut to Shorts and TikTok",
                         ReelPlan(**{**reel_plan.__dict__, "cross_posted": True})))

    scored = []
    for label, variant in variants:
        new_score, matched = _score_itemset(variant.itemset(), rules)
        delta = new_score - base
        if delta <= 0.001:
            continue
        support = max((r["n"] for r in matched), default=0)
        scored.append({
            "change": label,
            "expected_gain": round(delta, 3),
            "evidence_sample_size": support,
            "confidence": ("moderate" if support >= 8 else "tentative"),
        })

    scored.sort(key=lambda item: item["expected_gain"], reverse=True)
    return scored[:top_k]


def benchmark_against_own(own_media: list[dict], clusters: list) -> dict[str, Any]:
    """Where your own reels sit against the week's board. The reality check.

    Requires the Instagram Graph API half of the hybrid; without a token this
    returns a marker the report renders as "connect your account to see this".
    """
    reels = [m for m in own_media if m.get("is_reel")]
    views = [float(m["metrics"].get("views") or 0) for m in reels]
    views = [v for v in views if v > 0]
    if not views:
        return {"available": False,
                "reason": "No Instagram Graph API data. Set IG_ACCESS_TOKEN and IG_USER_ID."}

    board = [c.best_views for c in clusters[:10] if c.best_views]
    weights = load_weights()

    return {
        "available": True,
        "your_reels_analysed": len(views),
        "your_median_views": int(np.median(views)),
        "your_best_views": int(max(views)),
        "week_top10_median_views": int(np.median(board)) if board else None,
        "gap_multiple": (round(float(np.median(board)) / float(np.median(views)), 1)
                         if board and np.median(views) else None),
        "weights_in_use": weights,
        "note": ("Gap multiple is how many times bigger the median top-10 clip is "
                 "than your median reel. It is a scale reference, not a target — "
                 "the top 10 is a global board, and closing it is not the goal."),
    }
