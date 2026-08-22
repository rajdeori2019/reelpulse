"""Statistics validated against known values and against the failure modes
the evaluation harness actually caught.

These are not decorative. Before FDR correction the miner reported patterns in
100% of weeks built from pure noise, averaging 6.5 fabricated rules per week.
The tests below are what stop that regressing.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reelpulse.core.stats import (benjamini_hochberg, fisher_exact_greater,
                                  mann_whitney_greater, wilson_interval)


# ---- Fisher's exact -------------------------------------------------------

def test_fisher_matches_the_textbook_tea_tasting_value():
    """Fisher's own lady-tasting-tea table: 4/4 correct gives p = 1/70."""
    assert fisher_exact_greater(4, 0, 0, 4) == pytest.approx(1 / 70, rel=1e-9)


def test_fisher_matches_scipy():
    scipy = pytest.importorskip("scipy.stats")
    for table in [(8, 2, 5, 15), (3, 7, 2, 18), (12, 4, 9, 25), (1, 9, 1, 9)]:
        mine = fisher_exact_greater(*table)
        theirs = scipy.fisher_exact([[table[0], table[1]],
                                     [table[2], table[3]]], alternative="greater")[1]
        assert mine == pytest.approx(theirs, rel=1e-9)


def test_fisher_returns_one_when_there_is_no_effect():
    assert fisher_exact_greater(0, 10, 10, 0) == pytest.approx(1.0)


def test_fisher_handles_empty_groups():
    assert fisher_exact_greater(0, 0, 0, 0) == 1.0


# ---- Mann-Whitney ---------------------------------------------------------

def test_mann_whitney_matches_scipy():
    scipy = pytest.importorskip("scipy.stats")
    rng = random.Random(11)
    for _ in range(6):
        a = [rng.gauss(0.6, 1) for _ in range(18)]
        b = [rng.gauss(0.0, 1) for _ in range(90)]
        mine, _ = mann_whitney_greater(a, b)
        theirs = scipy.mannwhitneyu(a, b, alternative="greater",
                                    use_continuity=True)[1]
        assert mine == pytest.approx(theirs, rel=0.02, abs=0.005)


def test_superiority_is_the_probability_of_outranking():
    """0.5 means no effect; 1.0 means every A beats every B."""
    _, sup = mann_whitney_greater([10, 11, 12], [1, 2, 3])
    assert sup == pytest.approx(1.0)
    _, sup = mann_whitney_greater([1, 2, 3], [10, 11, 12])
    assert sup == pytest.approx(0.0)
    _, sup = mann_whitney_greater([1, 3], [2, 4])
    assert 0.2 <= sup <= 0.5


def test_mann_whitney_handles_ties():
    """All-tied data is exactly no evidence, and must not divide by zero."""
    p, sup = mann_whitney_greater([5, 5, 5, 5], [5, 5, 5, 5])
    assert p >= 0.4
    assert sup == pytest.approx(0.5)


def test_mann_whitney_beats_dichotomised_fisher_on_power():
    """The measured reason the primary test was switched.

    Same data, same effect: the rank test uses every clip's position while
    Fisher sees only whether each cleared the quartile.
    """
    rng = random.Random(5)
    a = [rng.gauss(0.7, 1) for _ in range(25)]
    b = [rng.gauss(0.0, 1) for _ in range(75)]

    rank_p, _ = mann_whitney_greater(a, b)

    cutoff = sorted(a + b)[int(len(a + b) * 0.75)]
    hits_a = sum(1 for v in a if v >= cutoff)
    hits_b = sum(1 for v in b if v >= cutoff)
    fisher_p = fisher_exact_greater(hits_a, len(a) - hits_a,
                                    hits_b, len(b) - hits_b)

    assert rank_p < fisher_p, (
        f"rank test ({rank_p:.4f}) should be more sensitive than dichotomised "
        f"Fisher ({fisher_p:.4f})")


# ---- Benjamini-Hochberg ---------------------------------------------------

def test_bh_matches_a_worked_example():
    pvals = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205]
    q = benjamini_hochberg(pvals)
    assert q[0] == pytest.approx(0.008, abs=1e-9)
    assert q[1] == pytest.approx(0.032, abs=1e-9)
    assert all(q[i] <= q[i + 1] + 1e-12 for i in range(len(q) - 1))


def test_bh_preserves_input_order():
    pvals = [0.5, 0.001, 0.2]
    q = benjamini_hochberg(pvals)
    assert q[1] < q[2] < q[0]


def test_bh_never_lowers_a_p_value():
    pvals = [0.01, 0.2, 0.5, 0.9]
    assert all(a >= b - 1e-12 for a, b in zip(benjamini_hochberg(pvals), pvals))


def test_bh_controls_false_discoveries_under_the_null():
    """40 families of 60 pure-null tests. At q<=0.10 the share of families
    reporting anything should sit near 10%, not near 100%."""
    rng = random.Random(3)
    families_with_a_discovery = 0
    trials = 40
    for _ in range(trials):
        pvals = [rng.random() for _ in range(60)]
        if any(q <= 0.10 for q in benjamini_hochberg(pvals)):
            families_with_a_discovery += 1
    rate = families_with_a_discovery / trials
    assert rate <= 0.30, f"{rate:.0%} of null families reported a discovery"


def test_bh_empty():
    assert benjamini_hochberg([]) == []


# ---- Wilson ---------------------------------------------------------------

def test_wilson_stays_inside_zero_one():
    for successes, trials in [(0, 5), (5, 5), (1, 3), (50, 100)]:
        low, high = wilson_interval(successes, trials)
        assert 0.0 <= low <= high <= 1.0


def test_wilson_is_wide_when_the_sample_is_small():
    """The whole point: a 60% hit rate from 5 clips is not 60%."""
    narrow = wilson_interval(60, 100)
    wide = wilson_interval(3, 5)
    assert (wide[1] - wide[0]) > (narrow[1] - narrow[0]) * 2


def test_wilson_matches_known_value():
    low, high = wilson_interval(50, 100)
    assert low == pytest.approx(0.404, abs=0.005)
    assert high == pytest.approx(0.596, abs=0.005)
