"""Instagram-native discovery: hashtag budget, parsing, and scoring fairness.

The load-bearing test is `test_engagement_basis_is_not_penalised`. Instagram
Hashtag Search returns other people's reels with likes and comments but no view
count — Meta does not publish one on that edge. If the scorer treats that as
"zero magnitude", every Instagram-native discovery is punished for a gap in
Meta's API rather than for anything about the reel, and the leaderboard silently
collapses back to YouTube-only results. That is the exact failure this whole
module exists to prevent, so it gets an explicit test.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reelpulse.collectors.instagram_discovery import InstagramDiscoveryCollector
from reelpulse.collectors.instagram_hashtag import (MAX_UNIQUE_HASHTAGS,
                                                    HashtagBudget,
                                                    InstagramHashtagCollector)
from reelpulse.core.features import performance_features
from reelpulse.core.score import score_clusters
from reelpulse.db import Store
from reelpulse.models import Candidate, Cluster

NOW = datetime.now(timezone.utc)


def _no_history(_fp, _at):
    return None


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store(Path(tmp) / "t.db")
        yield s
        s.close()


# ---- hashtag budget -------------------------------------------------------

def test_budget_starts_full(store):
    assert HashtagBudget(store).remaining() == MAX_UNIQUE_HASHTAGS


def test_budget_decrements_on_spend(store):
    budget = HashtagBudget(store)
    budget.record("reels", "123")
    budget.record("funny", "456")
    assert budget.remaining() == MAX_UNIQUE_HASHTAGS - 2
    assert budget.spent() == {"reels", "funny"}


def test_repeating_a_hashtag_costs_nothing(store):
    """The window counts unique hashtags, not calls. Re-querying one already
    inside the window must not consume a second slot."""
    budget = HashtagBudget(store)
    for _ in range(5):
        budget.record("reels", "123")
    assert budget.remaining() == MAX_UNIQUE_HASHTAGS - 1


def test_plan_defers_overflow_instead_of_truncating_silently(store):
    budget = HashtagBudget(store)
    for i in range(28):
        budget.record(f"tag{i}", str(i))

    wanted = [f"new{i}" for i in range(10)]
    affordable, deferred = budget.plan(wanted)

    assert len(affordable) == 2          # only 2 slots left
    assert len(deferred) == 8            # and the rest are reported, not dropped
    assert set(affordable) | set(deferred) == set(wanted)


def test_already_spent_hashtags_are_prioritised(store):
    """Free hashtags come first so the remaining budget buys new coverage."""
    budget = HashtagBudget(store)
    budget.record("reels", "1")
    for i in range(29):
        budget.record(f"tag{i}", str(i))

    affordable, _ = budget.plan(["brandnew", "reels"])
    assert affordable == ["reels"]       # budget exhausted; the free one survives


def test_cached_hashtag_id_is_reused(store):
    budget = HashtagBudget(store)
    budget.record("reels", "17843601665757100")
    assert budget.cached_id("reels") == "17843601665757100"
    assert budget.cached_id("never_queried") is None


# ---- parsing --------------------------------------------------------------

def test_hashtag_parse_never_invents_a_view_count():
    """Hashtag Search has no view_count field. It must stay None, not 0 and
    certainly not estimated from likes."""
    collector = InstagramHashtagCollector({}, None)
    items = [{
        "id": "1", "media_type": "VIDEO",
        "permalink": "https://www.instagram.com/reel/ABC123/",
        "caption": "POV: you try sourdough", "timestamp": "2026-08-20T10:00:00+0000",
        "like_count": 412_000, "comments_count": 8_900,
    }]
    got = collector._parse(items, "sourdough", "top_media")
    assert len(got) == 1
    assert got[0].views is None
    assert got[0].likes == 412_000
    assert got[0].platform == "instagram"
    assert got[0].platform_id == "ABC123"
    assert got[0].meta["instagram_native"] is True


def test_hashtag_parse_skips_images():
    collector = InstagramHashtagCollector({}, None)
    items = [{"id": "1", "media_type": "IMAGE", "permalink": "x", "like_count": 5}]
    assert collector._parse(items, "x", "top_media") == []


def test_business_discovery_keeps_real_view_counts():
    """This endpoint DOES return view_count — the only free official source of
    it for reels you do not own."""
    collector = InstagramDiscoveryCollector({})
    account = {
        "username": "somebaker", "followers_count": 250_000,
        "media": {"data": [{
            "id": "9", "media_type": "VIDEO",
            "permalink": "https://www.instagram.com/reel/XYZ789/",
            "caption": "sourdough crumb", "timestamp": "2026-08-21T08:00:00+0000",
            "like_count": 90_000, "comments_count": 1_200, "view_count": 3_400_000,
        }]},
    }
    got = collector._parse(account, "somebaker")
    assert got[0].views == 3_400_000
    assert got[0].creator == "somebaker"
    assert got[0].meta["reach_multiple"] == 13.6      # 3.4M / 250k


def test_business_discovery_handles_missing_view_count():
    """view_count is null on some media. That must yield None, not a crash."""
    collector = InstagramDiscoveryCollector({})
    account = {"username": "x", "followers_count": 100, "media": {"data": [{
        "id": "1", "media_type": "VIDEO", "permalink": "p",
        "like_count": 10, "comments_count": 1,
    }]}}
    got = collector._parse(account, "x")
    assert got[0].views is None
    assert got[0].meta["reach_multiple"] is None


# ---- scoring fairness -----------------------------------------------------

def make(views=None, likes=0, comments=0, hours=20.0, pid="x", **kw):
    return Candidate(platform="instagram", platform_id=pid, url="u",
                     title="a reel about something specific and distinct " + pid,
                     published_at=NOW - timedelta(hours=hours), duration_s=12.0,
                     views=views, likes=likes, comments=comments, **kw)


def test_measurement_basis_is_recorded():
    viewed = Cluster("a", [make(views=1_000_000, likes=50_000, pid="a")])
    engaged = Cluster("b", [make(views=None, likes=50_000, comments=900, pid="b")])
    blind = Cluster("c", [make(views=None, likes=0, comments=0, pid="c")])

    assert performance_features(viewed, _no_history)["measurement_basis"] == "views"
    assert performance_features(engaged, _no_history)["measurement_basis"] == "engagement"
    assert performance_features(blind, _no_history)["measurement_basis"] == "none"


def test_engagement_basis_is_not_penalised():
    """A hugely-engaged Instagram-native reel with no published view count must
    not be buried beneath a mediocre reel that happens to have one."""
    pool = [
        Cluster(f"low{i}", [make(views=v, likes=v // 40, comments=v // 900,
                                 pid=f"low{i}")])
        for i, v in enumerate([120_000, 90_000, 70_000, 50_000, 40_000,
                               30_000, 25_000, 20_000, 15_000, 10_000])
    ]
    star = Cluster("star", [make(views=None, likes=900_000, comments=41_000,
                                 pid="star")])
    pool.append(star)

    ranked = score_clusters(pool, _no_history)
    position = next(c.rank for c in ranked if c.cluster_id == "star")

    assert "_penalty_unmeasured" not in star.components, (
        "an engagement-measured reel was penalised as if unmeasurable")
    assert position <= 3, (
        f"Instagram-native reel with 900k likes ranked #{position} of "
        f"{len(ranked)} — the view-count gap is being treated as low performance")


def test_truly_unmeasurable_clips_are_still_penalised():
    """The penalty must survive for clips with neither views nor engagement."""
    pool = [Cluster(str(i), [make(views=1000 * (i + 1), likes=i * 10, pid=str(i))])
            for i in range(8)]
    blind = Cluster("blind", [make(views=None, likes=0, comments=0, pid="blind")])
    pool.append(blind)

    score_clusters(pool, _no_history)
    assert blind.components.get("_penalty_unmeasured", 0) < 0


def test_engagement_basis_never_fakes_a_view_count():
    """Magnitude is basis-relative, but a view count is never fabricated.

    `magnitude` for an engagement-measured clip reflects likes and comments, so
    it is non-zero — but `views` stays 0 and `has_measured_views` stays false,
    so nothing downstream can mistake it for a real view count.
    """
    cluster = Cluster("a", [make(views=None, likes=500_000, comments=10_000)])
    features = performance_features(cluster, _no_history)

    assert features["views"] == 0.0                    # never back-filled
    assert features["has_measured_views"] == 0.0       # and flagged as such
    assert features["measurement_basis"] == "engagement"
    assert features["scale"] == 530_000.0              # likes + 3*comments
    assert features["magnitude"] > 5.0                 # ranked on that scale


def test_view_basis_ignores_engagement_for_scale():
    """The converse: a clip with views is ranked on views, not on likes."""
    cluster = Cluster("a", [make(views=2_000_000, likes=10, comments=1)])
    features = performance_features(cluster, _no_history)
    assert features["measurement_basis"] == "views"
    assert features["scale"] == 2_000_000.0


def test_cohort_zscore_removes_the_cross_basis_handicap():
    """The same percentile in each cohort should score alike.

    Likes run a few percent of views, so pooling both into one distribution
    hands engagement-measured clips a systematic handicap that reflects Meta's
    API surface rather than performance.
    """
    from reelpulse.core.score import cohort_zscore

    # Two cohorts, identical internal shape, very different absolute magnitude.
    values = [7.0, 6.5, 6.0, 5.5, 5.0] + [5.0, 4.5, 4.0, 3.5, 3.0]
    cohorts = ["views"] * 5 + ["engagement"] * 5
    out = cohort_zscore(values, cohorts)

    # Top of each cohort lands at the same standardised score.
    assert abs(out[0] - out[5]) < 1e-9
    # And the ordering inside each cohort is preserved.
    assert list(out[:5]) == sorted(out[:5], reverse=True)


def test_tiny_cohort_falls_back_to_pooled_zscore():
    """A cohort of one cannot support a standard deviation; it must not
    collapse to zero and silently rank mid-pack."""
    from reelpulse.core.score import cohort_zscore

    values = [9.0, 5.0, 4.0, 3.0, 2.0]
    cohorts = ["engagement"] + ["views"] * 4
    out = cohort_zscore(values, cohorts)
    assert out[0] > 0, "singleton cohort collapsed instead of using pooled stats"
