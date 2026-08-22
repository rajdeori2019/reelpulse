"""Does this thing actually work?

Four experiments, run against the real code paths. The point is to produce
numbers that could embarrass the project, not numbers that flatter it.

  1. NULL       — feed it data where nothing is true. How many "patterns" does
                  it report? Anything above ~5% of trials is a false discovery
                  problem, and association rule mining is notorious for this
                  because every extra itemset is another untested hypothesis.
  2. POWER      — plant effects of known size. Does it find them, and is the
                  reported lift close to the truth?
  3. STABILITY  — run ten independent weeks from the SAME generating process.
                  Rules that appear one week and vanish the next are noise being
                  reported as insight, and the user acts on them either way.
  4. RANKING    — is VVS actually correlated with latent virality, or is it
                  elaborate arithmetic over nothing?

Run: python eval/harness.py
"""
from __future__ import annotations

import math
import random
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scipy import stats as scipy_stats

from reelpulse.core.dedupe import cluster_candidates
from reelpulse.core.features import craft_itemset
from reelpulse.core.patterns import mine_rules
from reelpulse.core.score import score_clusters
from reelpulse.models import Candidate

NOW = datetime.now(timezone.utc)

HOOKS = ["pov", "wait_for_it", "question", "listicle", "shock_claim",
         "transformation", "tutorial", "contrarian", "stakes", "none_detected"]
HOOK_TEXT = {
    "pov": "POV you {x}", "wait_for_it": "wait for it the {x}",
    "question": "why does {x}", "listicle": "top 5 {x} things",
    "shock_claim": "nobody tells you {x}", "transformation": "before and after {x}",
    "tutorial": "how to {x}", "contrarian": "unpopular opinion {x}",
    "stakes": "i tried {x} for 30 days", "none_detected": "{x}",
}
TOPICS = ["food", "comedy", "fitness", "animals", "tech", "beauty", "travel"]
SUBJECTS = ["sourdough", "kettlebell", "retriever", "espresso", "keyboard",
            "eyeliner", "kayak", "origami", "bonsai", "ramen", "cello", "quilt",
            "telescope", "sneaker", "waffle", "hammock", "compost", "banjo"]


def _no_history(_fp, _at):
    return None


def make_week(n: int, rng: random.Random, *, effects: dict[str, float] | None = None
              ) -> tuple[list[Candidate], dict[str, float]]:
    """One week of reels.

    `effects` maps a craft attribute to a multiplier on views. With effects={}
    nothing is true, and any pattern the miner reports is a false positive.

    Every clip gets a unique subject word so the deduper never merges distinct
    clips — otherwise merging noise would confound the measurement.
    """
    effects = effects or {}
    out: list[Candidate] = []
    truth: dict[str, float] = {}

    for i in range(n):
        hook = rng.choice(HOOKS)
        topic = rng.choice(TOPICS)
        duration = rng.choice([5, 9, 13, 20, 28, 40, 55])
        hashtags = rng.choice([0, 2, 5, 9, 13])
        caption_words = rng.choice([0, 5, 15, 33])

        subject = f"{rng.choice(SUBJECTS)}{i}"
        title = HOOK_TEXT[hook].format(x=subject)

        # Latent "true virality": a lognormal draw, times any planted effects.
        latent = rng.lognormvariate(12.5, 1.3)
        multiplier = 1.0
        for attribute, size in effects.items():
            kind, _, value = attribute.partition(":")
            hit = ((kind == "hook" and hook == value)
                   or (kind == "topic" and topic == value)
                   or (kind == "duration" and value == "short" and duration <= 13)
                   or (kind == "hashtags" and value == "few" and hashtags <= 2))
            if hit:
                multiplier *= size
        truth[str(sorted(effects.items()))] = multiplier

        views = int(latent * multiplier * rng.uniform(0.7, 1.4))
        age = rng.uniform(6, 24 * 7)

        caption = " ".join(f"w{j}" for j in range(caption_words))
        caption += " " + " ".join(f"#{subject}{j}" for j in range(hashtags))

        out.append(Candidate(
            platform="youtube", platform_id=f"e{i}",
            url=f"https://youtube.com/shorts/e{i}",
            title=title, caption=caption, creator=f"c{rng.randint(1, 60)}",
            published_at=NOW - timedelta(hours=age), duration_s=float(duration),
            views=views, likes=int(views * rng.uniform(0.02, 0.10)),
            comments=int(views * rng.uniform(0.001, 0.01)),
            meta={"region": "US", "hashtags": [f"{subject}{j}" for j in range(hashtags)],
                  "_latent": latent, "_topic": topic, "_hook": hook},
            source="eval",
        ))
    return out, truth


def analyse_week(candidates: list[Candidate], **kw) -> tuple[list, list[dict]]:
    clusters = cluster_candidates(candidates)
    ranked = score_clusters(clusters, _no_history, momentum_fn=None)
    return ranked, mine_rules(ranked, **kw)


# ---------------------------------------------------------------------------

def experiment_null(trials: int = 40, n: int = 200, **kw) -> dict:
    """Nothing is true. Everything reported is a false positive."""
    counts, lifts, any_rule = [], [], 0
    for t in range(trials):
        rng = random.Random(1000 + t)
        candidates, _ = make_week(n, rng, effects={})
        _, rules = analyse_week(candidates, **kw)
        counts.append(len(rules))
        lifts.extend(r["lift"] for r in rules)
        any_rule += 1 if rules else 0

    return {
        "trials": trials,
        "mean_false_rules_per_week": round(statistics.mean(counts), 2),
        "median": statistics.median(counts),
        "max": max(counts),
        "pct_weeks_reporting_something": round(any_rule / trials * 100, 1),
        "mean_false_lift": round(statistics.mean(lifts), 2) if lifts else 0.0,
        "max_false_lift": round(max(lifts), 2) if lifts else 0.0,
    }


def experiment_power(sizes=(1.2, 1.5, 2.0, 3.0), trials: int = 25,
                     n: int = 200, **kw) -> list[dict]:
    """Plant one known effect and see whether it is recovered."""
    results = []
    for size in sizes:
        found, reported_lifts, total_rules = 0, [], []
        for t in range(trials):
            rng = random.Random(5000 + t)
            candidates, _ = make_week(n, rng, effects={"hook:pov": size})
            _, rules = analyse_week(candidates, **kw)
            total_rules.append(len(rules))
            hit = next((r for r in rules if "hook:pov" in r["antecedent"]), None)
            if hit:
                found += 1
                reported_lifts.append(hit["lift"])
        results.append({
            "true_effect": size,
            "recall_pct": round(found / trials * 100, 1),
            "mean_reported_lift": (round(statistics.mean(reported_lifts), 2)
                                   if reported_lifts else None),
            "mean_total_rules": round(statistics.mean(total_rules), 1),
        })
    return results


def experiment_stability(weeks: int = 12, n: int = 200, **kw) -> dict:
    """Same process, different weeks. How much advice survives?"""
    per_week = []
    for w in range(weeks):
        rng = random.Random(9000 + w)
        candidates, _ = make_week(n, rng, effects={"hook:pov": 2.0,
                                                   "duration:short": 1.6})
        _, rules = analyse_week(candidates, **kw)
        per_week.append({frozenset(r["antecedent"]) for r in rules})

    seen: dict[frozenset, int] = {}
    for rules in per_week:
        for rule in rules:
            seen[rule] = seen.get(rule, 0) + 1

    if not seen:
        return {"weeks": weeks, "note": "no rules mined at all"}

    replicated = sum(1 for count in seen.values() if count >= weeks * 0.5)
    once_only = sum(1 for count in seen.values() if count == 1)

    return {
        "weeks": weeks,
        "distinct_rules_ever_reported": len(seen),
        "mean_rules_per_week": round(statistics.mean(len(r) for r in per_week), 1),
        "replicated_in_half_of_weeks": replicated,
        "appeared_exactly_once": once_only,
        "pct_advice_that_is_one_off": round(once_only / len(seen) * 100, 1),
    }


def experiment_ranking(trials: int = 20, n: int = 200) -> dict:
    """Does VVS track latent virality, or is it arithmetic over nothing?"""
    spearman, top10_precision = [], []
    for t in range(trials):
        rng = random.Random(7000 + t)
        candidates, _ = make_week(n, rng, effects={})
        ranked, _ = analyse_week(candidates)

        pairs = [(c.vvs, c.primary.meta.get("_latent", 0)) for c in ranked
                 if c.primary.meta.get("_latent")]
        if len(pairs) < 20:
            continue
        vvs = [p[0] for p in pairs]
        latent = [p[1] for p in pairs]
        rho, _ = scipy_stats.spearmanr(vvs, latent)
        spearman.append(rho)

        # Of the top 10 by VVS, how many are in the true top 10% by latent?
        cutoff = sorted(latent, reverse=True)[max(len(latent) // 10, 1) - 1]
        top = sorted(pairs, key=lambda p: -p[0])[:10]
        top10_precision.append(sum(1 for _, l in top if l >= cutoff) / 10)

    return {
        "trials": len(spearman),
        "mean_spearman_vvs_vs_latent": round(statistics.mean(spearman), 3),
        "min_spearman": round(min(spearman), 3),
        "mean_top10_precision": round(statistics.mean(top10_precision), 2),
    }


def show(title: str, payload) -> None:
    print(f"\n{title}\n" + "-" * len(title))
    rows = payload if isinstance(payload, list) else [payload]
    for row in rows:
        for key, value in row.items():
            print(f"  {key:<38} {value}")
        if len(rows) > 1:
            print()


if __name__ == "__main__":
    print("=" * 66)
    print("ReelPulse evaluation — current shipped settings")
    print("=" * 66)

    show("1. NULL: patterns reported when nothing is true", experiment_null())
    show("2. POWER: recovery of a planted effect", experiment_power())
    show("3. STABILITY: same process, 12 independent weeks",
         experiment_stability())
    show("4. RANKING: VVS vs latent virality", experiment_ranking())


# ---------------------------------------------------------------------------
# pooled mining — the fix the diagnosis pointed to
# ---------------------------------------------------------------------------

def experiment_pooled(pool_sizes=(1, 2, 4, 6), effect: float = 2.0,
                      trials: int = 25, n: int = 200) -> list[dict]:
    """Same weekly volume, but mining over a rolling window of weeks."""
    from reelpulse.core.features import craft_itemset
    from reelpulse.core.patterns import mine_from_rows

    results = []
    for weeks in pool_sizes:
        found, false_rules = 0, []
        for t in range(trials):
            rows, null_rows = [], []
            for w in range(weeks):
                rng = random.Random(20000 + t * 100 + w)
                cands, _ = make_week(n, rng, effects={"hook:pov": effect})
                ranked, _ = analyse_week(cands)
                rows += [(craft_itemset(c.tags), c.vvs) for c in ranked]

                rng2 = random.Random(60000 + t * 100 + w)
                nc, _ = make_week(n, rng2, effects={})
                nranked, _ = analyse_week(nc)
                null_rows += [(craft_itemset(c.tags), c.vvs) for c in nranked]

            rules = mine_from_rows(rows, weeks_pooled=weeks)
            if any("hook:pov" in r["antecedent"] for r in rules):
                found += 1
            false_rules.append(len(mine_from_rows(null_rows, weeks_pooled=weeks)))

        results.append({
            "weeks_pooled": weeks,
            "clips_in_pool": weeks * n,
            "recall_pct": round(found / trials * 100, 1),
            "false_rules_on_null_data": round(statistics.mean(false_rules), 2),
        })
    return results


def experiment_stability_pooled(weeks: int = 12, pool: int = 4,
                                n: int = 200) -> dict:
    """Stability when each report mines a rolling window instead of one week."""
    from reelpulse.core.features import craft_itemset
    from reelpulse.core.patterns import mine_from_rows

    history, per_report = [], []
    for w in range(weeks):
        rng = random.Random(9000 + w)
        cands, _ = make_week(n, rng, effects={"hook:pov": 2.0,
                                              "duration:short": 1.6})
        ranked, _ = analyse_week(cands)
        history.append([(craft_itemset(c.tags), c.vvs) for c in ranked])

        window = [row for wk in history[-pool:] for row in wk]
        rules = mine_from_rows(window, weeks_pooled=min(len(history), pool))
        per_report.append({frozenset(r["antecedent"]) for r in rules})

    seen: dict[frozenset, int] = {}
    for rules in per_report:
        for rule in rules:
            seen[rule] = seen.get(rule, 0) + 1
    if not seen:
        return {"weeks": weeks, "note": "no rules mined"}

    return {
        "weeks": weeks,
        "pool_weeks": pool,
        "distinct_rules_ever_reported": len(seen),
        "mean_rules_per_report": round(statistics.mean(len(r) for r in per_report), 1),
        "replicated_in_half_of_reports": sum(1 for c in seen.values() if c >= weeks * 0.5),
        "appeared_exactly_once": sum(1 for c in seen.values() if c == 1),
        "pct_advice_that_is_one_off": round(
            sum(1 for c in seen.values() if c == 1) / len(seen) * 100, 1),
    }
