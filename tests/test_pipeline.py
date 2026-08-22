"""Tests that assert behaviour, not just that code runs.

The important one is `test_mining_recovers_planted_signal`: the demo generator
plants known relationships (POV hooks and sub-10s runtimes get a real view
multiplier), so if the miner cannot recover them the analysis half of this
project is broken even when everything imports cleanly.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reelpulse.core.dedupe import cluster_candidates, similarity
from reelpulse.core.features import (caption_shape, craft_itemset, craft_features,
                                     detect_hooks, detect_topic, duration_bucket,
                                     performance_features)
from reelpulse.core.patterns import mine_rules
from reelpulse.core.recommend import ReelPlan, next_best_change, plan, predict
from reelpulse.core.score import score_clusters, zscore
from reelpulse.models import Candidate, Cluster
from scripts.seed_demo import synthesize

NOW = datetime.now(timezone.utc)


def _no_history(_fingerprint, _at):
    return None


def make(views=1000, hours_old=10.0, **kw):
    defaults = dict(platform="youtube", platform_id="x", url="u",
                    title="t", published_at=NOW - timedelta(hours=hours_old),
                    duration_s=12.0, views=views)
    defaults.update(kw)
    return Candidate(**defaults)


# ---- features -------------------------------------------------------------

def test_duration_buckets_cover_boundaries():
    assert duration_bucket(3) == "ultra_short_0_7s"
    assert duration_bucket(7) == "short_7_15s"
    assert duration_bucket(15) == "mid_15_30s"
    assert duration_bucket(59) == "long_30_60s"
    assert duration_bucket(200) == "extended_60s_plus"
    assert duration_bucket(None) == "unknown_duration"


def test_hooks_only_fire_on_the_opening():
    assert "pov" in detect_hooks("POV: you wake up in 1823")
    # Same trigger buried past the 120-char window must NOT count — the hook is
    # what stops the scroll, and nobody reads character 400 first.
    buried = "x" * 200 + " pov you wake up"
    assert "pov" not in detect_hooks(buried)


def test_topic_detection_picks_the_dominant_lexicon():
    assert detect_topic("best gym workout for abs, training tips") == "fitness"
    assert detect_topic("zzz nothing matches here") == "uncategorised"


def test_caption_shape_counts():
    shape = caption_shape("Try this 3 step recipe #food #cook ? follow for more")
    assert shape["hashtag_count"] == 2
    assert shape["has_question"] is True
    assert shape["has_cta"] is True
    assert shape["has_number_in_hook"] is True


def test_velocity_is_views_over_age_not_raw_views():
    fresh = Cluster("a", [make(views=100_000, hours_old=2)])
    stale = Cluster("b", [make(views=500_000, hours_old=160)])
    f = performance_features(fresh, _no_history)
    s = performance_features(stale, _no_history)
    assert s["magnitude"] > f["magnitude"]      # stale has more total views
    assert f["velocity"] > s["velocity"]        # but fresh is moving faster


def test_missing_views_flagged_not_faked():
    cluster = Cluster("c", [make(views=None)])
    features = performance_features(cluster, _no_history)
    assert features["has_measured_views"] == 0.0
    assert features["views"] == 0.0             # never invented


# ---- dedupe ---------------------------------------------------------------

def test_same_clip_across_platforms_merges():
    a = make(platform="youtube", platform_id="1", title="Wait for it: the cake collapses")
    b = make(platform="instagram", platform_id="2", title="Wait for it the cake collapses")
    clusters = cluster_candidates([a, b])
    assert len(clusters) == 1
    assert clusters[0].breadth == 2


def test_different_clips_do_not_merge():
    a = make(platform="youtube", platform_id="1", title="How to fix a bicycle chain")
    b = make(platform="instagram", platform_id="2", title="Puppy meets snow first time")
    assert len(cluster_candidates([a, b])) == 2


def test_duration_mismatch_blocks_a_merge():
    """Identical text but very different runtimes are not the same clip."""
    a = make(platform="youtube", platform_id="1", title="Wait for it cake", duration_s=8.0)
    b = make(platform="instagram", platform_id="2", title="Wait for it cake", duration_s=55.0)
    assert similarity(a, b) == 0.0


def test_templated_titles_do_not_collapse():
    """The regression that mattered: formulaic titles differing only in subject.

    Plain Jaccard scored these 0.78 similar — over the 0.62 threshold — because
    'how', 'fix', 'your' and 'seconds' counted as much as the subject word. IDF
    weighting has to keep them apart, or breadth becomes fiction.
    """
    pool = [
        make(platform="youtube", platform_id=f"t{i}", duration_s=12.0,
             title=f"How to fix your {subject} in 10 seconds")
        for i, subject in enumerate(["training", "coding", "recipe", "bicycle",
                                     "posture", "camera", "budget", "sleep"])
    ]
    clusters = cluster_candidates(pool)
    assert len(clusters) == len(pool), (
        f"{len(pool)} distinct subjects collapsed into {len(clusters)} clusters")


def test_true_duplicates_still_merge_despite_idf():
    """The IDF gate must not be so strict that real cross-posts stop merging."""
    a = make(platform="youtube", platform_id="1", duration_s=11.0,
             title="Wait for it: the sourdough collapses on camera")
    b = make(platform="instagram", platform_id="2", duration_s=11.0,
             title="wait for it the sourdough collapses on camera")
    noise = [make(platform="youtube", platform_id=f"n{i}", duration_s=11.0,
                  title=f"How to fix your {s} in 10 seconds")
             for i, s in enumerate(["training", "coding", "recipe"])]
    clusters = cluster_candidates([a, b] + noise)
    merged = [c for c in clusters if c.breadth == 2]
    assert len(merged) == 1
    assert len(clusters) == 4


def test_reddit_youtube_id_is_a_certain_link():
    yt = make(platform="youtube", platform_id="abc123", title="totally unrelated words")
    rd = make(platform="reddit", platform_id="r1", title="completely different text",
              views=None, meta={"youtube_id": "abc123"})
    clusters = cluster_candidates([yt, rd])
    assert len(clusters) == 1


# ---- scoring --------------------------------------------------------------

def test_zscore_survives_a_constant_column():
    assert list(zscore([5.0, 5.0, 5.0])) == [0.0, 0.0, 0.0]


def test_unmeasured_views_are_penalised():
    pool = [Cluster(str(i), [make(views=1000 * (i + 1), platform_id=str(i))])
            for i in range(8)]
    pool.append(Cluster("blind", [make(views=None, platform_id="blind")]))
    ranked = score_clusters(pool, _no_history)
    blind = next(c for c in ranked if c.cluster_id == "blind")
    assert blind.components.get("_penalty_unmeasured", 0) < 0


def test_ranking_is_stable_and_complete():
    pool = [Cluster(str(i), [make(views=1000 * (i + 1), platform_id=str(i))])
            for i in range(12)]
    ranked = score_clusters(pool, _no_history)
    assert [c.rank for c in ranked] == list(range(1, 13))
    assert all(ranked[i].vvs >= ranked[i + 1].vvs for i in range(len(ranked) - 1))


# ---- pattern mining -------------------------------------------------------

@pytest.fixture(scope="module")
def demo_run():
    candidates = synthesize(200)
    clusters = cluster_candidates(candidates)
    ranked = score_clusters(clusters, _no_history)
    rules = mine_rules(ranked)
    return ranked, rules


def test_mining_recovers_planted_signal(demo_run):
    """The generator gives POV/wait-for-it hooks and short runtimes a real
    advantage. If mining works, those show up as high-lift antecedents."""
    _, rules = demo_run
    assert rules, "no rules mined from 200 synthetic clips"

    antecedents = {item for rule in rules for item in rule["antecedent"]}
    planted = {"hook:pov", "hook:wait_for_it", "duration:ultra_short_0_7s",
               "duration:short_7_15s"}
    assert antecedents & planted, (
        f"miner recovered none of the planted signals. Found: {sorted(antecedents)}")


def test_every_rule_carries_its_uncertainty(demo_run):
    _, rules = demo_run
    for rule in rules:
        assert rule["n"] >= 2
        assert 0 < rule["support"] <= 1
        assert 0 <= rule["confidence"] <= 1
        assert rule["lift"] >= 1.15


def test_no_rule_is_redundant_with_a_stronger_subset(demo_run):
    _, rules = demo_run
    for rule in rules:
        items = set(rule["antecedent"])
        for other in rules:
            if set(other["antecedent"]) < items:
                assert other["lift"] < rule["lift"] * 0.98


# ---- recommender ----------------------------------------------------------

def test_every_recommendation_cites_evidence(demo_run):
    _, rules = demo_run
    for item in plan(rules, limit=8):
        assert item["evidence"]
        assert item["sample_size"] >= 2
        assert item["confidence"] in {"strong", "moderate", "tentative"}


def test_predict_refuses_to_forecast_views(demo_run):
    _, rules = demo_run
    result = predict(ReelPlan(topic="food", hook="pov", duration_s=9), rules)
    assert "views" not in result
    assert "Not a view forecast" in result["caveat"]


def test_next_best_change_only_suggests_improvements(demo_run):
    _, rules = demo_run
    weak = ReelPlan(topic="food", hook="none_detected", duration_s=90,
                    caption_words=40, hashtag_count=15)
    changes = next_best_change(weak, rules)
    assert all(c["expected_gain"] > 0 for c in changes)


def test_craft_itemset_is_all_actionable_strings():
    cluster = Cluster("x", [make(title="POV: you try this 3 step recipe",
                                 caption="quick #food #cook")])
    items = craft_itemset(craft_features(cluster))
    assert all(":" in item for item in items)
    assert any(item.startswith("hook:") for item in items)


# ---- the regression that matters most -------------------------------------

def test_miner_does_not_fabricate_patterns_from_noise():
    """Data where nothing is true must usually produce no rules.

    Before FDR correction this reported patterns in 100% of null weeks,
    averaging 6.5 fabricated rules each with lifts up to 2.2x. Users acted on
    those. This test is the guard against that regressing.
    """
    import random as _random

    from reelpulse.core.patterns import mine_from_rows

    hooks = ["pov", "wait_for_it", "question", "listicle", "tutorial",
             "contrarian", "none_detected"]
    durations = ["ultra_short_0_7s", "short_7_15s", "mid_15_30s", "long_30_60s"]
    tags = ["tags_0", "tags_1_3", "tags_4_10", "tags_11_plus"]

    weeks_with_rules = 0
    trials = 20
    for t in range(trials):
        rng = _random.Random(400 + t)
        rows = []
        for _ in range(250):
            itemset = {
                f"hook:{rng.choice(hooks)}",
                f"duration:{rng.choice(durations)}",
                f"hashtags:{rng.choice(tags)}",
            }
            # Score is independent of every attribute above.
            rows.append((itemset, rng.gauss(0, 1)))
        if mine_from_rows(rows):
            weeks_with_rules += 1

    rate = weeks_with_rules / trials
    assert rate <= 0.25, (
        f"{rate:.0%} of pure-noise datasets produced 'patterns' — "
        "false discovery control has regressed")


def test_miner_still_finds_a_real_effect():
    """The counterpart: correction must not silence genuine signal."""
    import random as _random

    from reelpulse.core.patterns import mine_from_rows

    rng = _random.Random(7)
    hooks = ["pov", "question", "tutorial", "listicle", "none_detected"]
    rows = []
    for _ in range(900):
        hook = rng.choice(hooks)
        score = rng.gauss(1.1 if hook == "pov" else 0.0, 1.0)
        rows.append(({f"hook:{hook}", f"duration:{rng.choice(['short_7_15s', 'mid_15_30s'])}"},
                     score))

    rules = mine_from_rows(rows, weeks_pooled=4)
    assert any("hook:pov" in r["antecedent"] for r in rules), \
        "a strong planted effect in 900 clips was not recovered"


def test_every_shipped_rule_carries_its_statistics():
    """A rule without a q-value cannot be judged by the reader."""
    import random as _random

    from reelpulse.core.patterns import mine_from_rows

    rng = _random.Random(21)
    rows = []
    for _ in range(800):
        hook = rng.choice(["pov", "question", "tutorial", "none_detected"])
        rows.append(({f"hook:{hook}"},
                     rng.gauss(1.0 if hook == "pov" else 0.0, 1.0)))

    for rule in mine_from_rows(rows):
        assert 0 <= rule["q_value"] <= 1
        assert 0 <= rule["superiority"] <= 1
        assert rule["n"] >= 8
        assert rule["tested"] >= 1
        assert len(rule["confidence_ci"]) == 2


def test_tiny_pools_report_nothing_rather_than_guessing():
    from reelpulse.core.patterns import mine_from_rows
    rows = [({"hook:pov"}, 1.0), ({"hook:question"}, 0.0)] * 5
    assert mine_from_rows(rows) == []


def test_region_sampling_covers_every_region_over_time():
    """The bug the first live run exposed.

    Truncating the region x query product meant only the first
    budget/len(queries) regions were EVER sampled — six of twelve countries were
    permanently invisible while the board called itself global.
    """
    import itertools

    regions = ["US", "IN", "BR", "ID", "GB", "MX", "PH", "NG", "DE", "JP", "TR", "EG"]
    queries = ["#reels", "#shorts viral", "instagram reel", "trending"]
    budget = 24

    pairs = list(itertools.product(regions, queries))

    # Old behaviour: always the same prefix.
    old = {r for r, _ in pairs[:budget]}
    assert len(old) == 6, "fixture no longer reproduces the original bug"

    # New behaviour: walk the list by week and every region gets reached.
    seen = set()
    for week in range(1, 53):
        offset = week * max(budget // len(queries), 1)
        start = offset % len(pairs)
        window = (pairs[start:] + pairs[:start])[:budget]
        assert len(window) == budget, "a week sampled the wrong number of pairs"
        seen |= {r for r, _ in window}

    assert seen == set(regions), f"never sampled: {set(regions) - seen}"
