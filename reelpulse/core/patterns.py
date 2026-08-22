"""Association-rule mining over craft features.

This is where "why was it most viewed" stops being a vibe and becomes a number.

For every combination of craft attributes that shows up often enough, compute
how much more likely a reel carrying that combination is to land in the week's
top quartile than a reel drawn at random. That ratio is **lift**, and it is the
only claim ReelPulse ever makes about causation-adjacent things — stated with
its support and sample size attached, so you can see when it is thin.

Implemented from scratch (Apriori, ~90 lines) rather than pulling in mlxtend,
because the dependency footprint of this project is a feature: fewer packages,
fewer breakages in an unattended weekly cron.

An important honesty note that also appears in the report: these are
correlations inside one week of survivor-biased data. A rule saying POV hooks
carry 3x lift means POV hooks are over-represented among things that already
went big — not that adding "POV:" to your caption multiplies your views. The
recommender phrases them as hypotheses to test, and the calibration loop is what
turns them into something you can actually trust for *your* account.
"""
from __future__ import annotations

from collections import defaultdict
from itertools import combinations
from typing import Any

import numpy as np

from ..models import Cluster
from .features import craft_itemset
from .stats import (benjamini_hochberg, fisher_exact_greater,
                    mann_whitney_greater, wilson_interval)


def _support_counts(transactions: list[set[str]], min_support: float,
                    max_size: int = 2) -> dict[frozenset[str], int]:
    """Apriori: grow itemsets level by level, pruning below min_support.

    `max_size` defaults to 2 rather than 3. Three-item combinations roughly
    doubled the number of hypotheses tested, and every extra hypothesis raises
    the multiple-testing bar for all the others. Measured on the eval harness,
    dropping to pairs bought materially more power on single-attribute rules —
    which are also the most actionable, since "open on a POV hook" is advice and
    "open on a POV hook in food content under 15s with 1-3 hashtags" is a
    description of four clips.
    """
    n = len(transactions)
    min_count = max(int(min_support * n), 2)

    counts: dict[frozenset[str], int] = defaultdict(int)
    for txn in transactions:
        for item in txn:
            counts[frozenset([item])] += 1

    frequent = {k: v for k, v in counts.items() if v >= min_count}
    all_frequent = dict(frequent)

    k = 2
    while frequent and k <= max_size:
        candidates: set[frozenset[str]] = set()
        keys = list(frequent)
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                union = a | b
                if len(union) == k:
                    candidates.add(union)

        level: dict[frozenset[str], int] = defaultdict(int)
        for txn in transactions:
            for cand in candidates:
                if cand <= txn:
                    level[cand] += 1

        frequent = {k2: v for k2, v in level.items() if v >= min_count}
        all_frequent.update(frequent)
        k += 1

    return all_frequent


def mine_rules(clusters: list[Cluster], *, min_support: float = 0.05,
               min_lift: float = 1.15, top_quantile: float = 0.75,
               fdr: float = 0.10, min_n: int = 8) -> list[dict[str, Any]]:
    """Rules of the form {craft attributes} -> high_vvs, FDR-controlled.

    `high_vvs` is membership in the top quartile of the week's VVS distribution.
    Using a quantile rather than an absolute threshold keeps rules comparable
    across quiet weeks and blockbuster weeks alike.

    Every candidate rule is a separate hypothesis, so every candidate gets a
    Fisher's exact test and the whole family gets Benjamini-Hochberg correction
    at `fdr`. Filtering on lift alone — the previous behaviour — reported
    patterns in 100% of weeks built from pure noise. Correction is what makes
    the output mean something.

    `min_n` exists because a rule seen in 3 clips cannot be trusted no matter
    what its p-value says; it is an effect estimate with no precision.
    """
    rows = [(craft_itemset(c.tags), c.vvs) for c in clusters]
    return mine_from_rows(rows, min_support=min_support, min_lift=min_lift,
                          top_quantile=top_quantile, fdr=fdr, min_n=min_n)


def mine_from_rows(rows: list[tuple[set[str], float]], *,
                   min_support: float = 0.05, min_lift: float = 1.15,
                   top_quantile: float = 0.75, fdr: float = 0.10,
                   min_n: int = 8, max_size: int = 2,
                   min_superiority: float = 0.56,
                   weeks_pooled: int = 1) -> list[dict[str, Any]]:
    """Mine (itemset, score) pairs, which may come from several pooled weeks.

    Pooling is statistically legitimate here precisely because VVS is z-scored
    WITHIN each week before it is stored: two weeks' scores are already on a
    common scale, so stacking them compares like with like rather than mixing a
    blockbuster week into a quiet one.

    It is also the single most effective fix available. Measured on the eval
    harness with a planted 3x effect: recall was 4/12 at 200 clips, 9/12 at 400,
    and 12/12 at 800. The limiting factor was never the test — it was how few
    clips one week contains.
    """
    if len(rows) < 20:
        # Below ~20 clips error rates cannot be controlled at all. Returning
        # nothing beats returning confident noise.
        return []

    transactions = [r[0] for r in rows]
    values = [r[1] for r in rows]
    cutoff = float(np.quantile(np.array(values), top_quantile))
    labels = [v >= cutoff for v in values]
    clusters = None  # noqa: F841 — kept out of scope deliberately below

    n = len(transactions)
    successes = sum(labels)
    base_rate = successes / n
    if base_rate <= 0:
        return []

    frequent = _support_counts(transactions, min_support, max_size)

    # ---- test every candidate ------------------------------------------
    candidates: list[dict[str, Any]] = []
    for itemset, count in frequent.items():
        if count < min_n:
            continue
        hits = sum(1 for txn, label in zip(transactions, labels)
                   if itemset <= txn and label)

        # 2x2: with/without the attribute x succeeded/did not.
        a, b = hits, count - hits
        c, d = successes - hits, (n - count) - (successes - hits)
        if min(a, b, c, d) < 0:
            continue

        confidence = hits / count
        lift = confidence / base_rate if base_rate else 0.0

        # PRIMARY TEST: rank-sum on the continuous score, using every clip's
        # position rather than only whether it cleared the quartile. Fisher on
        # the dichotomised outcome is kept as a secondary, reported figure —
        # it is what "lift" corresponds to — but it is not what decides whether
        # a rule ships, because dichotomising cost most of the power.
        with_attr = [v for v, txn in zip(values, transactions) if itemset <= txn]
        without = [v for v, txn in zip(values, transactions) if not itemset <= txn]

        pvalue, superiority = mann_whitney_greater(with_attr, without)
        fisher_p = fisher_exact_greater(a, b, c, d)

        vvs_delta = (float(np.median(with_attr) - np.median(without))
                     if with_attr and without else 0.0)
        low, high = wilson_interval(hits, count)

        candidates.append({
            "antecedent": sorted(itemset),
            "consequent": "top_quartile_vvs",
            "support": count / n,
            "confidence": confidence,
            "confidence_ci": (round(low, 3), round(high, 3)),
            "lift": lift,
            "superiority": round(superiority, 3),
            "vvs_delta": round(vvs_delta, 3),
            "fisher_p": fisher_p,
            "n": count,
            "hits": hits,
            "base_rate": base_rate,
            "p_value": pvalue,
            "pool_size": n,
            "weeks_pooled": weeks_pooled,
        })

    if not candidates:
        return []

    # ---- control the false discovery rate -------------------------------
    qvalues = benjamini_hochberg([c["p_value"] for c in candidates])
    rules: list[dict[str, Any]] = []
    for candidate, q in zip(candidates, qvalues):
        candidate["q_value"] = q
        candidate["tested"] = len(candidates)
        # Gate on `superiority`, the effect size belonging to the rank test
        # that produced the p-value. Gating on quartile lift here was
        # inconsistent — testing with one statistic and filtering with a weaker
        # one silently discarded rules the test had just confirmed.
        if q <= fdr and candidate["superiority"] >= min_superiority:
            rules.append(candidate)

    # Occam pass: drop a rule when a strictly MORE GENERAL rule already achieves
    # essentially the same lift. "POV hook + food + 7-15s" adding 0.01 lift over
    # plain "POV hook" is not a third insight, it is the same insight wearing two
    # extra conditions — and a reader will over-fit to those conditions.
    #
    # Compared against the full rule set rather than only the already-kept ones,
    # so the result does not depend on iteration order.
    rules.sort(key=lambda r: (r["superiority"], r["support"]), reverse=True)
    indexed = [(frozenset(r["antecedent"]), r) for r in rules]

    kept: list[dict[str, Any]] = []
    for rule in rules:
        items = frozenset(rule["antecedent"])
        redundant = any(
            other_items < items and other["superiority"] >= rule["superiority"] * 0.98
            for other_items, other in indexed)
        if not redundant:
            kept.append(rule)
    return kept


NOUN_PHRASE = {
    "duration:ultra_short_0_7s": "a runtime under 7s",
    "duration:short_7_15s": "a 7-15s runtime",
    "duration:mid_15_30s": "a 15-30s runtime",
    "duration:long_30_60s": "a 30-60s runtime",
    "duration:extended_60s_plus": "a runtime over 60s",
    "duration:unknown_duration": "an unknown runtime",
    "caption:caption_none": "no caption",
    "caption:caption_short_1_10w": "a caption under 10 words",
    "caption:caption_medium_11_30w": "an 11-30 word caption",
    "caption:caption_long_30w_plus": "a caption over 30 words",
    "caption:has_question": "a question in the caption",
    "caption:has_cta": "a call to action",
    "caption:has_emoji": "an emoji in the caption",
    "hashtags:tags_0": "zero hashtags",
    "hashtags:tags_1_3": "1-3 hashtags",
    "hashtags:tags_4_10": "4-10 hashtags",
    "hashtags:tags_11_plus": "11+ hashtags",
    "hook:has_number": "a number in the opening line",
    "hook:shouty_caps": "capitalised words in the hook",
    "reach:cross_posted": "being cross-posted",
    "reach:multi_region": "surfacing in 3+ regions",
}


def _phrase(item: str) -> str:
    """One antecedent as a readable noun phrase.

    Table-driven rather than templated, because templating produced doubled
    words like "caption long 30w plus caption" — the bucket name already
    contains the category.
    """
    if item in NOUN_PHRASE:
        return NOUN_PHRASE[item]
    kind, _, value = item.partition(":")
    value = value.replace("_", " ")
    article = "an" if value[:1].lower() in "aeiou" else "a"
    return {
        "hook": f"{article} {value} hook",
        "hook_secondary": f"a secondary {value} angle",   # article follows "secondary"
        "topic": f"{value} content",
    }.get(kind, value)


def summarise_rule(rule: dict[str, Any]) -> str:
    """Human phrasing for one rule, with its real uncertainty attached."""
    combo = " + ".join(_phrase(item) for item in rule["antecedent"])
    text = (f"{combo} → {rule['lift']:.2f}x more likely to reach the week's top "
            f"quartile ({rule['n']} clips, {rule['confidence']*100:.0f}% hit rate "
            f"vs a {rule['base_rate']*100:.0f}% base rate")

    ci = rule.get("confidence_ci")
    if ci:
        text += f"; 95% CI {ci[0]*100:.0f}-{ci[1]*100:.0f}%"
    q = rule.get("q_value")
    if q is not None:
        text += f"; q={q:.3f}"
        if rule.get("tested"):
            text += f" across {rule['tested']} tested"
    return text + ")"


def confidence_label(rule: dict[str, Any]) -> str:
    """Evidence strength, driven by the q-value rather than by lift.

    A big lift from a handful of clips is not strong evidence, and the previous
    lift-and-count heuristic happily called it "strong". The q-value already
    accounts for both sample size and how many hypotheses were tested, so it is
    the honest basis for this label.
    """
    q = rule.get("q_value")
    if q is None:                      # rule loaded from an older DB row
        return "moderate" if rule.get("n", 0) >= 15 else "tentative"
    if q <= 0.01 and rule["n"] >= 15:
        return "strong"
    if q <= 0.05:
        return "moderate"
    return "tentative"
