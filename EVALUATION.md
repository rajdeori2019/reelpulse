# Does this actually work?

Measured, not asserted. `python eval/harness.py` reproduces everything below.

**The disclaimer that matters most: this has never touched real Instagram data.**
Every number here comes from synthetic reels with a known ground truth, which is
the only way to measure a false discovery rate at all — you cannot check whether
a pattern is real unless you built the world it came from. It tells you the
machinery is sound. It does not tell you the *lexicons* are right, that hook
detection matches how humans read a hook, or that the craft features capture
what actually drives reach on Instagram. Only real data answers those, and
nobody has run this on real data yet.

---

## Summary

| What | Verdict |
|---|---|
| **Ranking (the leaderboard)** | ✅ Works. Spearman **0.80** vs latent virality |
| **Pattern mining, 1 week** | ⚠️ Underpowered. Finds a real 2× effect **24%** of the time |
| **Pattern mining, 6 weeks pooled** | ✅ Finds it **96%** of the time |
| **False patterns on pure noise** | ✅ ~**0.1** per report, 5–10% of reports |
| **Advice stability** | ⚠️ Improved but imperfect — 1.8 rules/report, 40% still one-off |

The headline: **the leaderboard is trustworthy from day one; the pattern mining
needs about six weeks of history before it is.** The tool says so on its own
dashboard rather than hiding it.

---

## What was broken, and what it cost

The first version filtered rules on lift alone, with no significance testing.
Measured against 40 weeks of data where **nothing was true** — views generated
independently of every craft feature:

| | Before | After |
|---|---|---|
| Weeks reporting at least one "pattern" | **100%** | **5%** |
| Mean fabricated rules per week | **6.5** | **0.07** |
| Max fabricated lift | 2.2× | 2.2× |

Every single week, it confidently reported six or seven patterns that did not
exist, with lifts high enough to look convincing. That is not a rounding error;
it is the analysis half of the product being decorative.

The cause is textbook and entirely self-inflicted: mining every combination of
craft attributes tests **~65 hypotheses per run**, and testing 65 hypotheses at
a 5% threshold yields roughly three false positives by construction.

### The fixes, in the order they were needed

**1. Significance testing with FDR control.** Every candidate rule now gets a
statistical test, and the whole family gets
[Benjamini-Hochberg](https://en.wikipedia.org/wiki/False_discovery_rate)
correction at q ≤ 0.10. This alone took false discovery from 100% of weeks to
5%.

**2. A rank test instead of a dichotomised one.** Correction has a price: recall
of a genuine 3× effect fell to **16%**. The miner had become honest and useless.
The cause was throwing away information — classifying each clip as "top quartile
or not" discards its actual position. Switching the primary test to
Mann-Whitney U on the continuous score recovered most of that (16% → 36% at 3×)
because it uses every clip's rank.

**3. Fewer hypotheses.** Three-attribute combinations roughly doubled the family
size, raising the bar for everything else. Capping at pairs bought power back
for single-attribute rules — which are also the only ones that read as advice.
"Open on a POV hook" is actionable. "POV hook + food + under 15s + 1–3 hashtags"
is a description of four clips.

**4. Pooling weeks — the one that actually mattered.** Diagnosis:

```
n= 200  hypotheses=65  median raw p for the TRUE effect = 0.0182   recall  4/12
n= 400  hypotheses=58  median raw p = 0.0003                       recall  9/12
n= 800  hypotheses=56  median raw p = 0.0000                       recall 12/12
```

The test was never the bottleneck. **One week does not contain enough clips.**
Mining now pools a rolling window (default 4 weeks, `--pool-weeks`):

| Weeks pooled | Clips | Recall of a real 2× effect | False rules on null data |
|---|---|---|---|
| 1 | 200 | 16% | 0.16 |
| 2 | 400 | 20% | 0.24 |
| 4 | 800 | **76%** | 0.08 |
| 6 | 1200 | **96%** | 0.12 |

Recall rises six-fold while false positives stay flat. Pooling is statistically
legitimate here because VVS is z-scored *within* each week before storage, so
two weeks are already on a common scale.

---

## The four experiments

### 1. Null — does it invent patterns?

40 weeks, 200 clips each, views independent of every feature. **Every rule
reported is by construction false.**

```
pct_weeks_reporting_something     5.0     (was 100.0)
mean_false_rules_per_week         0.07    (was 6.5)
```

5% is the expected rate at q ≤ 0.10 and cannot be driven to zero without
destroying power. It is a floor, not a bug.

### 2. Power — does it find what is there?

One week, 200 clips, one planted effect:

| True effect | Recall | Reported lift |
|---|---|---|
| 1.2× | 0% | — |
| 1.5× | 4% | 2.79× |
| 2.0× | 24% | 2.23× |
| 3.0× | 36% | 2.53× |

Two honest readings. **Small effects are invisible at one week's volume** — a
1.2× effect will not be found, and pretending otherwise would mean accepting the
false-positive rate that came with it. And **reported lift is unreliable as an
effect size**, which is why `superiority` is now reported alongside it: the
probability that a clip with the attribute outranks one without. It is bounded,
needs no base rate, and does not swing with the quartile cutoff.

### 3. Stability — does the advice reproduce?

Twelve independent weeks from the same generating process. Advice that changes
every week is noise, and the user acts on it either way.

| | Before | After (4-week pooling) |
|---|---|---|
| Distinct rules ever reported | 29 | **5** |
| Mean rules per report | 6.1 | 1.8 |
| Appeared exactly once | 9 | 2 |

Far less churn, and what survives is far more likely to be real. **40% of rules
still appear only once**, so this is improved rather than solved — treat a
single-appearance rule as a hypothesis, which is what the dashboard calls it.

### 4. Ranking — is the leaderboard real?

The half that always worked:

```
mean_spearman_vvs_vs_latent    0.803
min_spearman                   0.760
mean_top10_precision           0.62
```

VVS correlates **0.80** with latent virality across 20 independent weeks, never
dropping below 0.76. Of its top 10, **62%** are genuinely in the true top 10% —
against 10% for random selection.

---

## What this does not measure

- **Real Instagram data.** The largest gap by far. Synthetic reels have the craft-to-performance relationship I wrote into them.
- **Whether the features are the right features.** The eval plants effects on attributes the code already extracts. If real virality is driven by something not in `lexicons.yaml` — visual composition, audio choice, the creator's existing audience — no amount of correct statistics will surface it.
- **Causation.** Nothing here does. Rules are mined from clips that already surfaced; the comparison group is "less viral things that also went somewhat viral", not "everything posted".
- **Whether hook detection matches human judgment.** Keyword matching on captions is a proxy for what a viewer sees in the first three seconds, and an imperfect one.

## Reproducing

```bash
python eval/harness.py          # all experiments, ~4 minutes
pytest tests/test_stats.py -q   # statistics validated against scipy
pytest tests/ -q                # full suite, 122 tests
```

The regression guard lives in `tests/test_pipeline.py`:
`test_miner_does_not_fabricate_patterns_from_noise` fails if false discovery
control regresses, and `test_miner_still_finds_a_real_effect` fails if
correction is ever tightened into silence.
