"""Keyword search: relevance gating, query syntax, and anchored scoring.

The load-bearing test here is `test_anchoring_prevents_big_fish_small_pond`.
Without anchoring, the best clip in a five-result keyword search gets the same
VVS as a genuine global smash, because z-scores are computed over whatever pool
you hand them. That is the single most misleading thing a keyword search can do,
so it gets an explicit test rather than a comment.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reelpulse.core.dedupe import cluster_candidates
from reelpulse.core.relevance import (TIER_HASHTAG, TIER_NONE, TIER_PARTIAL,
                                      TIER_TITLE_ALL, TIER_TITLE_PHRASE,
                                      expand, filter_candidates, match, parse_query)
from reelpulse.core.score import score_clusters
from reelpulse.models import Candidate

NOW = datetime.now(timezone.utc)


def _no_history(_fp, _at):
    return None


def make(title="", caption="", hashtags=(), views=1000, hours=10.0, pid="x", **kw):
    return Candidate(platform="youtube", platform_id=pid, url="u", title=title,
                     caption=caption, published_at=NOW - timedelta(hours=hours),
                     duration_s=12.0, views=views,
                     meta={"hashtags": list(hashtags)}, **kw)


# ---- query parsing --------------------------------------------------------

def test_plain_terms_are_all_required():
    q = parse_query("sourdough starter")
    assert q.required == ["sourdough", "starter"]
    assert not q.is_or


def test_quoted_phrase_is_extracted():
    q = parse_query('"cold plunge" recovery')
    assert q.phrases == ["cold plunge"]
    assert "recovery" in q.required


def test_or_switches_to_any_mode():
    q = parse_query("pilates OR reformer")
    assert q.is_or
    assert set(q.optional) == {"pilates", "reformer"}


def test_minus_excludes():
    q = parse_query("sourdough -discard")
    assert q.excluded == ["discard"]


def test_noise_words_are_dropped():
    q = parse_query("the best viral sourdough reel")
    assert q.required == ["sourdough"]


# ---- relevance matching ---------------------------------------------------

def test_title_phrase_beats_caption_mention():
    q = parse_query('"cold plunge"')
    strong = make(title="my cold plunge routine", pid="a")
    weak = make(title="morning routine", caption="includes a cold plunge", pid="b")
    assert match(strong, q)[0] == TIER_TITLE_PHRASE
    assert match(strong, q)[1] > match(weak, q)[1]


def test_all_terms_in_title_qualifies():
    q = parse_query("sourdough starter")
    assert match(make(title="sourdough starter from scratch"), q)[0] == TIER_TITLE_ALL


def test_partial_and_match_is_flagged_not_hidden():
    """A clip matching one of two required terms is real but weak.

    The contract: it clears the documented default gate (0.4) so `--min-relevance
    0.4 keeps partial matches` is true, but always scores below a full match so
    it can never outrank one.
    """
    q = parse_query("sourdough starter")
    tier, score = match(make(title="sourdough bread tutorial"), q)
    full = match(make(title="sourdough starter guide"), q)[1]

    assert tier == TIER_PARTIAL
    assert score >= 0.4, "partials cannot clear the default gate the help promises"
    assert score < full, "a partial match outranked a full one"


def test_more_matched_terms_scores_higher():
    q = parse_query("cold plunge recovery")
    one = match(make(title="cold shower", pid="a"), q)[1]
    two = match(make(title="cold plunge tips", pid="b"), q)[1]
    assert two > one


def test_exclusion_beats_a_perfect_match():
    q = parse_query('"cold plunge" -sponsored')
    cand = make(title="cold plunge routine", caption="sponsored by someone")
    assert match(cand, q) == (TIER_NONE, 0.0)


def test_hashtag_only_match_is_recognised():
    q = parse_query("pilates")
    cand = make(title="morning movement", hashtags=["pilates", "fitness"])
    assert match(cand, q)[0] == TIER_HASHTAG


def test_word_boundary_stops_substring_false_positives():
    """'match' must not match 'matches'. Same trap the topic classifier hit."""
    q = parse_query("match")
    assert match(make(title="nothing matches here"), q)[0] == TIER_NONE


def test_or_query_scores_multiple_hits_higher():
    q = parse_query("pilates OR reformer")
    one = make(title="pilates class", pid="a")
    both = make(title="pilates on the reformer", pid="b")
    assert match(both, q)[1] > match(one, q)[1]


def test_filter_reports_every_tier():
    q = parse_query("sourdough")
    pool = [make(title="sourdough loaf", pid="1"),
            make(title="banana bread", pid="2"),
            make(title="baking", hashtags=["sourdough"], pid="3")]
    kept, tally = filter_candidates(pool, q, min_relevance=0.4)
    assert len(kept) == 2
    assert tally[TIER_NONE] == 1
    assert all("relevance" in c.meta and "match_tier" in c.meta for c in kept)


def test_min_relevance_gates_partials():
    q = parse_query("sourdough starter")
    pool = [make(title="sourdough bread", pid="1")]           # partial only
    assert len(filter_candidates(pool, q, 0.3)[0]) == 1
    assert len(filter_candidates(pool, q, 0.7)[0]) == 0


def test_expansion_does_not_invent_synonyms():
    """Widening a search must not silently change what was asked for."""
    q = parse_query("sourdough")
    variants = expand(q, extra=3)
    assert all("sourdough" in v for v in variants)


# ---- anchored scoring -----------------------------------------------------

def _vvs_of(target, pool):
    clusters = cluster_candidates(pool)
    ranked = score_clusters(clusters, _no_history)
    hit = next(c for c in ranked if any(m.platform_id == target for m in c.members))
    return hit.vvs


def test_anchoring_prevents_big_fish_small_pond():
    """A modest clip topping a tiny keyword pool must not score like a smash.

    Unanchored, it is the best of five and z-scores near the top. Anchored
    against a realistic background pool, it lands where it actually belongs.
    """
    keyword_hits = [
        make(title=f"sourdough tip {i}", views=v, pid=f"k{i}", hours=40)
        for i, v in enumerate([90_000, 60_000, 40_000, 25_000, 10_000])
    ]
    background = [
        make(title=f"unrelated clip {i}", views=v, pid=f"b{i}", hours=40)
        for i, v in enumerate([40_000_000, 22_000_000, 15_000_000, 9_000_000,
                               6_000_000, 4_000_000, 3_000_000, 2_000_000,
                               1_500_000, 1_200_000, 900_000, 700_000,
                               600_000, 500_000, 400_000, 300_000,
                               250_000, 200_000, 150_000, 120_000])
    ]

    alone = _vvs_of("k0", keyword_hits)
    anchored = _vvs_of("k0", keyword_hits + background)
    assert anchored < alone, (
        f"anchoring did not deflate the small-pond winner "
        f"(alone={alone:.2f}, anchored={anchored:.2f})")


def test_anchoring_preserves_a_genuine_smash():
    """The flip side: anchoring must not flatten a clip that really is huge."""
    keyword_hits = [make(title="sourdough", views=38_000_000, pid="k0", hours=40)]
    background = [
        make(title=f"unrelated {i}", views=v, pid=f"b{i}", hours=40)
        for i, v in enumerate([2_000_000, 1_000_000, 800_000, 600_000, 400_000,
                               300_000, 200_000, 150_000, 100_000, 80_000,
                               60_000, 40_000, 30_000, 20_000, 10_000])
    ]
    clusters = cluster_candidates(keyword_hits + background)
    ranked = score_clusters(clusters, _no_history)
    assert ranked[0].primary.platform_id == "k0"


# ---- reach: is a #1 actually big? -----------------------------------------

def test_a_quiet_niche_is_called_out():
    """The failure this module exists for: 75 results, best of them tiny, and
    the tool presented it as a leaderboard with a 2-view clip at #3."""
    from reelpulse.core.reach import assess

    background = [float(v) for v in range(50_000, 5_000_000, 50_000)]
    tiny = [34_133.0, 1_513.0, 2.0, 574.0, 337.0]

    result = assess(tiny, background)
    assert result["verdict"] == "nothing_viral"
    assert "Nothing matching this search went meaningfully viral" in result["headline"]


def test_a_genuine_hit_passes():
    from reelpulse.core.reach import assess

    background = [float(v) for v in range(50_000, 5_000_000, 50_000)]
    result = assess([12_000_000.0, 400_000.0], background)
    assert result["verdict"] == "ok"
    assert result["best_tier"] == "viral"


def test_absolute_floor_overrides_a_flattering_percentile():
    """In a pool of uniformly tiny clips, a slightly less tiny clip sits at the
    99th percentile. It is still not viral."""
    from reelpulse.core.reach import tier_for

    background = [float(v) for v in range(1, 400)]
    label, pct = tier_for(450.0, background)
    assert pct > 0.95
    assert label == "negligible"


def test_unanchored_says_so_rather_than_guessing():
    from reelpulse.core.reach import assess

    result = assess([9_000_000.0], [])
    assert result["anchored"] is False
    assert result["verdict"] == "unrated"
    assert "No stored history" in result["headline"]


def test_unanchored_still_catches_obviously_tiny_results():
    from reelpulse.core.reach import assess

    result = assess([2.0, 337.0], [])
    assert result["verdict"] == "nothing_viral"
    assert "not a leaderboard of hits" in result["headline"]


def test_empty_result_set():
    from reelpulse.core.reach import assess
    assert assess([], [1.0, 2.0])["verdict"] == "empty"
