# Architecture

```
collectors/          →  core/dedupe  →  core/score  →  core/patterns  →  core/recommend
  youtube                clustering      VVS ranking     Apriori rules     evidence-backed
  reddit                 (IDF-gated)     (z-scored)      (lift/support)    advice
  instagram_oembed
  instagram_graph                              ↓
  trends                                  core/report  →  docs/index.html
                                          data/latest.json
```

Everything is a `Candidate` (one video on one platform at one moment) until
`dedupe` merges them into `Cluster`s (one underlying clip, seen in one or more
places). Everything downstream operates on clusters.

---

## The five decisions that shape this design

### 1. Instagram first, three ways — and YouTube for the gap

Instagram is queried natively through three official endpoints before any other
source is touched:

- **Hashtag Search** (`ig_hashtag_search` + `top_media`) — open discovery of
  other people's public reels. Returns likes, comments, caption and permalink.
  Hard-limited to **30 unique hashtags per rolling 7 days**, tracked in SQLite
  by `HashtagBudget` so a sweep defers overflow instead of being rejected. No
  view counts on this edge.
- **Business Discovery** (`business_discovery.username()`) — the only free
  official route to `view_count` on reels you do not own. Works from a curated
  watchlist; targets must be professional accounts.
- **Own Graph API** — full metrics on your own reels, for calibration.

What none of them covers: a view count for an arbitrary viral reel by a creator
you have not listed. That is a limit on Meta's side. So **YouTube Shorts** is
added as a measuring instrument for view-scale, since most globally viral clips
are cross-posted there within days and Shorts publishes exact counts free.

The alternative — a scraper — would trade these known, documented blind spots
for account bans, constant breakage, and a dataset you cannot publish.

### 1b. Measurement basis, and why cohort standardisation is required

Clips therefore arrive measured in different units: views for some, likes and
comments for Instagram-native ones. Every scale-dependent component
(`magnitude`, `velocity`, `acceleration`, `share_ratio`) is computed from
whichever metric exists, and `measurement_basis` records which.

Deriving those components from views alone would hand every Instagram-native
discovery a structural zero on the three most heavily weighted signals —
punishing it for a gap in Meta's API and quietly collapsing the board back to
YouTube-only results. That was a real bug, caught by
`test_engagement_basis_is_not_penalised`.

Removing the penalty is not sufficient either. Likes run a few percent of views,
so `log10(likes)` sits roughly 1.2 below `log10(views)` for the same reel —
a permanent handicap. `cohort_zscore` therefore standardises each basis against
its own peers, so the 90th percentile of engagement-measured clips scores like
the 90th percentile of view-measured ones. Cohorts below three members fall back
to pooled statistics rather than collapsing to zero.

**No view count is ever estimated from likes.** Cross-basis comparability is a
statistics problem, solved with statistics; inventing the missing number would
be the one thing this project exists to avoid.

### 2. Velocity over magnitude

Total views measure history. Views-per-hour measures what is happening now. A
tool that ranks by lifetime views mostly surfaces things that peaked weeks ago,
which is useless if the question is "what should I make this week". Default
weights put velocity at 1.60 against magnitude's 1.00.

Acceleration (1.30) needs two measurements, which is the entire reason
`daily-snapshot.yml` exists. Without snapshot history every run looks like a
first sighting and acceleration sits at neutral forever.

### 3. Dedupe produces a feature, not just tidiness

Merging cross-posts isn't housekeeping — the *number of places a clip appears*
is the best free proxy for global reach that no single API exposes. That makes
dedupe correctness load-bearing: a wrong merge fabricates breadth, which
directly inflates rank.

So matching is biased hard toward false negatives, with three gates:

- **Duration** must match within 2s (unknown duration doesn't disqualify).
- **Mutual distinctive tokens** — if each side names a subject the other never
  mentions, they're different clips. This is what stops formulaic titles from
  collapsing. "How to fix your *training* in 10 seconds" and "How to fix your
  *coding* in 10 seconds" scored 0.78 similar under plain Jaccard, because
  "how", "fix", "your" and "seconds" counted as much as the subject word. On the
  demo fixture that bug merged 278 candidates down to 78 clips instead of ~180.
- **IDF-weighted overlap** ≥ 0.40 on what remains.

A `youtube_id` carried by a Reddit post is treated as a certain link and skips
the fuzzy path entirely.

### 4. Z-scoring inside the week makes weights mean something

Each VVS component is z-scored across that week's pool before weighting. That's
what lets `velocity: 1.60` genuinely mean "velocity matters 1.6× as much as
magnitude" — otherwise you'd be comparing log-views against a 0–1 engagement
ratio and the weights would be meaningless.

Z-scores are winsorized at 3.5σ. One 400M-view clip in a pool of forty would
otherwise flatten every other component into noise; clipping keeps the outlier
on top, where it belongs, without erasing the rest of the ranking.

### 4b. Mining is significance-tested, and pools weeks

Measured, not assumed — see [EVALUATION.md](EVALUATION.md). Filtering rules on
lift alone reported patterns in **100% of weeks built from pure noise**,
averaging 6.5 fabricated rules each. Mining every attribute combination tests
~65 hypotheses per run; at a 5% threshold that yields ~3 false positives by
construction.

Three corrections, in the order the evidence demanded them:

- **Rank test + Benjamini-Hochberg FDR** at q ≤ 0.10. Took false discovery from 100% of weeks to 5%.
- **Mann-Whitney on the continuous score**, not Fisher on quartile membership. Correction had cut recall of a real 3× effect to 16%; dichotomising discards each clip's actual position, and the rank test recovers most of that power.
- **Pooling weeks.** The diagnosis was decisive: median raw p for a true effect was 0.018 at 200 clips and 0.0000 at 800. The test was never the bottleneck — one week does not contain enough clips. Recall of a real 2× effect: 16% at one week, 76% at four, 96% at six, with false positives flat.

Pooling is legitimate because VVS is z-scored *within* each week before storage,
so weeks are already on a common scale.

### 5. Every recommendation carries its evidence

`core/recommend.py` will not emit a suggestion it cannot trace to a mined rule,
with that rule's lift and sample size attached. `predict()` deliberately returns
a *relative craft position*, never a view forecast — public signals cannot
support a view forecast, and returning one would be the single most dishonest
thing this codebase could do.

---

## Module map

| Module | Responsibility |
|---|---|
| `models.py` | `Candidate`, `Cluster`, derived properties (age, velocity, fingerprint) |
| `db.py` | SQLite; the `snapshots` table is what makes acceleration possible |
| `config.py` | YAML + `.env` loading. Nothing tunable lives in code |
| `limits.py` | Quota ledger, token buckets, usage-header feedback, circuit breaker |
| `collectors/` | One module per source. Each degrades to `[]` rather than crashing a run |
| `core/dedupe.py` | Cross-platform clustering; produces the `breadth` feature |
| `core/features.py` | Performance features (how big) and craft features (why) |
| `core/score.py` | VVS + `explain()` — why a clip ranked where it ranked |
| `core/patterns.py` | Apriori, significance testing, FDR control, Occam pass |
| `core/stats.py` | Fisher exact, Mann-Whitney U, Benjamini-Hochberg, Wilson CI |
| `core/analyze.py` | Per-clip "why it worked" — craft inference, not scoring maths |
| `core/recommend.py` | `plan` / `predict` / `next_best_change` |
| `core/calibrate.py` | Ridge fit against your own Graph API numbers, with shrinkage |
| `core/relevance.py` | Query parsing and tiered keyword matching for `search` |
| `core/report.py` | `latest.json` + single-file dashboard render |

Two questions get answered separately and should not be confused: `explain()`
says why a clip *ranked* where it did (a fact about the maths), while
`analyze.why_it_worked()` says which craft choices the week's data associates
with outperformance (an inference, with its evidence attached).

---

## Keyword search adds two failure modes the weekly board doesn't have

**Irrelevance.** The weekly board asks "what went big", and every answer is
on-topic by construction. A keyword search asks "what went big *about X*", where
a clip can be enormously viral and completely off-topic. So relevance is a
**hard gate applied before scoring**, never a scoring component — mixing them
would let a 40M-view clip with a weak keyword match outrank a perfectly on-topic
one, which is precisely what makes most social listening tools untrustworthy.

**Scale distortion.** VVS z-scores across whatever pool it's handed. Five
sourdough clips scored alone produce a `+3.2` top result identical to a genuine
global smash's. The fix is to mix the stored global pool into the scoring set
and display only the keyword matches — the keyword rank and the global rank are
then both reported, because "#1 for sourdough, #15 overall" is the sentence that
actually informs a decision.

Whether anchoring happened is decided on **distinct background clips after
clustering**, not raw candidate count before it. Sixty stored candidates that
dedupe to four clips anchor nothing, and calling that run "anchored" would
overstate how comparable its scores are.

## Rate limiting is body-aware, not status-aware

Neither platform returns HTTP 429. Meta throttles with **400** plus an error
code (4/17/32/613/80000-80014); YouTube exhausts quota with **403** plus
`reason: quotaExceeded`. A status-code-only handler classifies both as permanent
client errors, retries them as if they were bugs, and keeps hitting a throttled
endpoint — the behaviour that turns a temporary throttle into a restricted app.

`limits.classify()` therefore inspects the response body, and returns
`(is_rate_limited, is_retryable, reason)` separately: a 400 caused by a
malformed field must NOT open a cooldown, or a real bug gets masked as a rate
limit and silently disables the collector.

Quota state is persisted because the limits themselves span processes. Two
things follow that are easy to get wrong:

- **Cost is per-unit, not per-call.** YouTube's `search.list` costs 100 units
  against a 10,000/day budget; `videos.list` costs 1. Counting calls
  under-reports spend 100x on exactly the endpoint that exhausts the quota.
- **The window is not always rolling.** YouTube refills at midnight Pacific.
  A rolling 24h window keeps counting spend the platform has already forgiven,
  refusing runs against a full budget. `window_start()` models the real
  boundary, with a conservative fallback when tzdata is absent.

Both Instagram collectors share one limiter instance because they draw on the
same app-level hourly budget. Separate ledgers would each believe they had the
whole allowance and together breach it.

## The Occam pass on mined rules

After Apriori, a rule is dropped if a strictly *more general* rule achieves
essentially the same lift (within 2%). "POV hook + food + 7–15s" adding 0.01
lift over plain "POV hook" is not a third insight — it's the same insight
wearing two extra conditions, and a reader will over-fit to those conditions.

The comparison runs against the full rule set rather than only the already-kept
ones, so the result doesn't depend on iteration order.

---

## Extending it

**A new signal source:** subclass `Collector`, return `Candidate`s, register it
in `cli._collect`. The `safe_collect()` wrapper handles failure. Then add a
component name to `COMPONENTS` in `core/score.py` and a weight in
`config/weights.yaml`.

**New hook archetypes or topics:** edit `config/lexicons.yaml`. No code change —
the analyzer picks them up, and because matching is keyword-based every
classification stays auditable. When a clip is tagged `hook:pov` you can see
exactly which token did it.

**A different notion of "winning":** `mine_rules(top_quantile=...)` defaults to
the top quartile. Using a quantile rather than an absolute threshold keeps rules
stable across quiet weeks and blockbuster weeks alike.

---

## Known limitations

- **Survivor bias.** Rules are mined only from clips that already surfaced. The comparison group is "less viral things that also went somewhat viral", not "everything posted". This is stated on the dashboard and cannot be fixed without data nobody has.
- **English-leaning lexicons.** Hook and topic detection use English keyword lists. Non-English clips fall to `uncategorised`, which is now excluded from mining rather than producing a meaningless "make your content uncategorised" rule.
- **Instagram-native clips are underweighted.** Anything never cross-posted has no verified view count.
- **Rule-based hook detection is shallow.** It reads titles and captions, not frames. A visual hook with a bland caption is invisible to it.
- **Small weekly pools produce noisy z-scores.** Below `min_candidates` (40) the run logs a warning and tells you to widen queries or regions.
- **Keyword search matches text, not pixels.** A reel about sourdough captioned "🥖✨" is invisible to it. No free API searches short-form video by visual content, so this is a ceiling rather than a gap — the honest workaround is searching the audio or creator instead.
