# ReelPulse

A weekly top-10 leaderboard for globally viral short-form video, an analysis of
*why* those clips won, and an evidence-backed recommender for your own reels.

100% open source. Every data source is free and officially sanctioned. Nothing
here scrapes Instagram.

---

## Read this first

Instagram gives you **three** official free routes to other people's reels, and
ReelPulse uses all three. None of them is complete on its own:

| Source | Finds | Gives you | Constraint |
|---|---|---|---|
| **Hashtag Search** | Other people's public reels by hashtag | likes, comments, caption, permalink | **30 unique hashtags per rolling 7 days.** No view counts |
| **Business Discovery** | A named professional account's reels | likes, comments, **real view counts** | Watchlist only — no "find popular accounts" call. Professional accounts only |
| **Your own Graph API** | Your reels | Everything Meta measures | Your account only |

The gap they leave: **Instagram publishes no view count for a reel you don't own
unless its creator is on your watchlist.** That's a hard limit on Meta's side,
not an implementation gap. Every product advertising a "global top Reels by
views" chart is either scraping Instagram (against Meta's terms, bans accounts,
breaks constantly) or reselling someone's scrape.

So for global view-scale, ReelPulse adds a fourth source: most reels that go
globally viral get cross-posted to **YouTube Shorts** within days, and Shorts
publishes exact view counts through a free official API. That's a measuring
instrument pointed at the same clip, not a substitute subject.

**How the ranking handles the mismatch.** Every clip is ranked on whichever
metric Instagram actually publishes for it, and each basis is standardised
against its own cohort:

- `basis: views` — ranked against other view-measured clips
- `basis: engagement` — ranked against other engagement-measured clips

Likes run a few percent of views, so pooling both into one distribution would
hand every Instagram-native reel a permanent handicap reflecting Meta's API
surface rather than performance. **No view count is ever estimated from likes.**
The dashboard labels every row with the basis it was ranked on.

What you get is an **estimated** leaderboard with its methodology on the face of
it — not Meta's internal ranking, and it doesn't pretend to be. What it's
genuinely good at is the second half: given a pool of clips that demonstrably
went big, what did they have in common, and what does that imply for what you
post next.

---

## Setup

Two paths. Both are free.

### A. Hosted, hands-off (recommended)

1. Fork this repo.
2. Get an **Instagram token** — see [Instagram setup](#instagram-setup) below.
   It's the fiddliest step and has one non-obvious hard requirement: your
   Instagram account must be linked to a **Facebook Page**.
3. Get a **YouTube Data API key** — [console.cloud.google.com](https://console.cloud.google.com) → new project → enable "YouTube Data API v3" → Credentials → API key. No credit card.
4. Repo → Settings → Secrets and variables → Actions → add `IG_ACCESS_TOKEN`,
   `IG_USER_ID`, `YOUTUBE_API_KEY`.
5. Repo → Settings → Pages → Source: **GitHub Actions**.
6. Actions tab → "Weekly report" → **Run workflow**.

Done. Your dashboard is live at `https://<you>.github.io/<repo>/` and rebuilds
every Monday at 06:00 UTC. There is nothing to host and nothing to pay for.

Optional: `REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET` adds the off-platform
share signal.

**Then edit `config/sources.yaml`:** set `instagram_hashtag.hashtags` to tags in
your niche (budget: 30 unique per 7 days) and
`instagram_discovery.watchlist` to creator handles you want real view counts
for. Those two lists are what make the output about *your* corner of Instagram
rather than the generic global feed.

### B. Local

```bash
git clone <your fork> && cd reelpulse
pip install -r requirements.txt
cp .env.example .env          # add your YouTube key
python -m reelpulse doctor    # tells you what's missing and what it costs
python -m reelpulse run
open docs/index.html
```

**No keys yet?** `python -m reelpulse demo` runs the entire pipeline on
synthetic data and opens a real dashboard. Good for seeing the shape of the
thing in about four seconds.

---

## Commands

| Command | What it does |
|---|---|
| `reelpulse doctor` | What's configured, what's missing, and what each gap costs you |
| `reelpulse instagram-setup` | Test an Instagram token and auto-find your account id |
| `reelpulse collect` | One collection pass + snapshot. This is the daily cron job |
| `reelpulse run` | The full pipeline: collect → cluster → score → mine → report → dashboard |
| `reelpulse search` | Find trending reels for a **keyword** and stack rank them |
| `reelpulse calibrate` | Refit the scoring weights against your own Instagram numbers |
| `reelpulse advise` | Recommendations for a reel you're planning |
| `reelpulse limits` | API budget spend, pacing, and any active cooldowns |
| `reelpulse demo` | The whole pipeline offline on synthetic data, no keys |

```bash
python -m reelpulse advise --topic food --hook pov --duration 9 --hashtags 2 --question
```

---

## Instagram setup

The step that unlocks native Instagram discovery. It has one requirement people
miss, and it is not optional:

> **Hashtag Search only works with "Instagram API with _Facebook_ Login", which
> requires your Instagram professional account to be linked to a Facebook Page.**

Meta ships two Instagram APIs. The newer "Instagram Login" path needs no Facebook
Page and is much easier to set up — and it **does not support Hashtag Search at
all**. Without hashtag search there is no open discovery of other people's reels,
which is the whole point. So: Facebook Page, no way around it.

| | Instagram Login | **Facebook Login** |
|---|---|---|
| Facebook Page needed | no | **yes** |
| Hashtag Search | ✗ | **✓** |
| Business Discovery | ✓ | ✓ |
| Your own insights | ✓ | ✓ |

### Steps

1. **Instagram account → Professional.** In the app: Settings → Account type →
   switch to Business or Creator. Free.
2. **Link it to a Facebook Page.** Create one if you don't have one — it can be
   empty and unpublished. Instagram app → Settings → Accounts Centre → add the
   Page.
3. **Create a Meta app.** [developers.facebook.com/apps](https://developers.facebook.com/apps)
   → Create app → type **Business** → add the **Instagram** product.
4. **Generate a token** in the
   [Graph API Explorer](https://developers.facebook.com/tools/explorer/): pick
   your app, click "Generate Access Token", and grant these four permissions —
   `instagram_basic`, `pages_show_list`, `pages_read_engagement`,
   `instagram_manage_insights`.
5. **Make it long-lived.** The Explorer gives you a token that dies in about an
   hour. Exchange it in the
   [Access Token Tool](https://developers.facebook.com/tools/accesstoken/) —
   click the info icon next to your token, then "Extend Access Token". You get
   ~60 days.
6. **Verify it:**

```bash
python -m reelpulse instagram-setup --token EAAxxxxx
```

That command is the point of this section. It tells you exactly which of the
three capabilities work, distinguishes a missing permission from an unlinked
Page — Meta words those almost identically — and **prints the `IG_USER_ID` it
found**, so you never have to hunt for it.

```
[OK  ] Token valid
       expires 2026-10-21 (59 days)
[OK  ] All required permissions granted
[OK  ] Instagram account linked to a Facebook Page
       @you (id 17841400000000) via Page 'Your Page' — 4,210 followers
[OK  ] Hashtag Search (open discovery)
[OK  ] Business Discovery (real view counts)
[OK  ] Your own media

Everything works. Put these in your .env or GitHub Secrets:
  IG_ACCESS_TOKEN=EAAxxxxx...
  IG_USER_ID=17841400000000
```

**No App Review needed.** You're using your own account, which counts as
Standard Access. App Review only applies when other people's accounts are
involved.

**Tokens expire every ~60 days.** When Instagram-native results silently vanish
from your weekly board, that's what happened. Re-run `instagram-setup` to
confirm, generate a new token, update the secret.

---

## Keyword search

```bash
python -m reelpulse search "sourdough"
python -m reelpulse search '"cold plunge" -sponsored' --days 14 --top 30
python -m reelpulse search "pilates OR reformer" --min-relevance 0.7
python -m reelpulse search "skincare" --out docs/skincare.html --json
```

Query syntax: `"exact phrase"`, `OR` between terms, `-excluded`.

Output is a stack-ranked list by the same Viral Velocity Score the weekly board
uses, plus the patterns specific to that keyword — which is usually the more
interesting half. *"What do the top sourdough reels have in common"* is a
different and more actionable question than *"what went viral globally"*.

### Three things worth understanding before you trust the output

**1. Relevance is a hard gate, not a ranking factor.** YouTube's search is
generous — a query for "sourdough" returns plenty of popular baking clips that
never mention it. Every result is tiered by *where* the term matched, and the
tier is printed next to it:

| Tier | Meaning | Score |
|---|---|---|
| `title_phrase` | Exact phrase in the title | 1.00 |
| `title_all_terms` | All terms in the title | 0.92 |
| `caption_phrase` / `caption_all_terms` | Matched in the caption | 0.80 / 0.72 |
| `hashtag` | Only complete once hashtags count — weaker on purpose | 0.62 |
| `partial` | Some but not all required terms | 0.40–0.65 |

`--min-relevance 0.4` (default) keeps partials, `0.7` requires a full term
match, `0.9` demands it in the title. Relevance never boosts rank — a weak
match can't ride a big view count to the top.

**2. Anchoring stops "big fish, small pond."** VVS z-scores every component
across the pool it's given. Score five sourdough clips on their own and the best
of them gets a `+3.2` — the same number a genuine global smash would get. So the
search mixes your stored global pool into the scoring set (only keyword matches
are displayed), and reports both ranks:

```
1. [VVS +3.64] sourdough starter cold method
   4,200,000 | 121,205/hr | youtube | match: title_all_terms | #15 of 109 overall
```

`#1 for sourdough, #15 overall` is the useful sentence. Anchoring needs ≥15
stored background clips — run `reelpulse collect` a few times first. Until then
every search prints a loud `[UNANCHORED]` warning and says the scores aren't
comparable across searches.

**3. It matches text, not pictures.** A reel about sourdough with a caption that
just says "🥖✨" is invisible to keyword search. There is no free API that
searches short-form video by visual content, so this is a real ceiling, not an
implementation gap.

### Quota

Each `search.list` call costs 100 of your 10,000 free daily units, **regardless
of how many results it returns** — so the lever is call count, not page size.

| | Calls | Units | Searches/day |
|---|---|---|---|
| Weekly report | 24 | 2,400 | — |
| Keyword search (default) | 4 | 400 | ~14 |
| Keyword search (`--max-searches 12`) | 12 | 1,200 | ~4 |

The default is 4 calls: 2 query variants x 2 regions. A keyword search rarely
needs the six-region fan-out the weekly board uses, and each extra region
multiplies the cost.

**Check before you spend.** `--dry-run` prints the exact queries and the unit
cost without touching the API:

```bash
python -m reelpulse search '"career advice" OR "career guidance"' --dry-run
```

```
  searches  : 4 calls x 100 units = 400 units
  budget    : 8,000 available of 10,000
  queries actually sent to YouTube:
    [US] 'career advice'
    [IN] 'career advice'
```

That output is worth reading before any real search — it shows what the parser
made of your query, which is where a search most often goes wrong. Running out
mid-day is not damage (the limiter refuses before sending, so nothing gets
throttled) but it does mean waiting for the midnight-Pacific reset.

---

## Not getting blocked

Every request goes through a rate limiter that enforces quota **before sending**,
because the request that would breach a limit cannot succeed — and making it is
what escalates a throttle into a restriction.

**The trap this is built around: neither platform signals a rate limit with HTTP
429.**

| Platform | How it actually says "slow down" |
|---|---|
| Meta | HTTP **400** with error code `4`, `17`, `32`, `613`, or `80000–80014` |
| YouTube | HTTP **403** with `reason: quotaExceeded` / `rateLimitExceeded` |
| Reddit | HTTP 429 with `X-Ratelimit-Reset` |

A 429-only handler reads the first two as permanent bugs, retries them, and
keeps hammering a throttled endpoint. Detection here reads the response *body*,
not just the status code.

**Six mechanisms, in the order they engage:**

1. **Persistent quota ledger.** Spend is written to SQLite, so limits that span
   runs are actually enforced — an in-memory counter resets every process and
   enforces nothing. YouTube's units are counted correctly too: `search.list`
   costs 100, `videos.list` costs 1. Counting calls instead of units would
   under-report spend 100× on the endpoint that drains the budget.

2. **Token buckets.** Pace requests inside a run so a burst never exceeds the
   documented per-minute ceiling.

3. **Usage-header feedback.** Meta reports consumption as a percentage on every
   response (`X-App-Usage`, `X-Business-Use-Case-Usage`). Above 80% the limiter
   slows itself down rather than discovering the wall by hitting it.

4. **Persistent circuit breaker.** On a real throttle the service enters
   cooldown — honouring Meta's own `estimated_time_to_regain_access`, which is
   reported in **minutes**, not seconds. The cooldown survives process exit, so
   tomorrow's cron run doesn't immediately re-trigger it.

5. **Distinct-counted budgets.** Instagram's hashtag window allows 30 *unique
   hashtags* per rolling 7 days, however many times each is queried. Counting
   calls would halve the usable budget for nothing; counting tags means a
   re-query of one already inside the window is free and is never refused, even
   when the window is otherwise full. One hashtag costs two API calls (resolve,
   then fetch) and exactly one slot. Every hashtag request is charged to both
   ceilings it sits under — the weekly slot *and* one of the app's 200 Graph
   calls per hour — because Meta enforces both.

6. **A ledger that outlives the runner.** On GitHub Actions the database lives
   in a cache GitHub evicts after **seven days** without a hit — the same length
   as the hashtag window. An eviction would make every budget read as full, and
   the first symptom would be Meta rejecting a sweep with an error that says
   nothing about why. So the live part of the ledger is exported to
   `data/api_ledger.json` and committed. Every workflow merges it before
   spending and writes it back afterwards, including on failure — a run that
   died half-way still spent what it spent.

   ```bash
   python -m reelpulse ledger import   # before a run
   python -m reelpulse ledger export   # after, always
   ```

**Nothing reaches the network unmetered.** This is enforced structurally rather
than by review: `tests/test_no_unmetered_calls.py` walks the source with the AST
and fails if any function sends an HTTP request without acquiring budget first
and booking the outcome after. It was written because two call sites — the
Instagram setup doctor and Reddit's OAuth token exchange — sat outside the
limiter for weeks precisely because they were small enough that nobody looked.

**Reserve headroom.** Each service holds back a slice of quota (YouTube 20%,
Instagram 25%) that ad-hoc commands cannot touch. Three keyword searches on a
Sunday night can't leave the Monday cron job with an empty tank.

**Wall-clock resets are modelled.** YouTube's quota refills at midnight Pacific,
not on a rolling 24h window. Spend 8,000 units at 11pm PT and a naive rolling
window would still count them at 00:30 PT, refusing runs against a budget the
platform already refilled.

```bash
python -m reelpulse limits
```

```
service                           used        left       pace  status
youtube                  2400/10000 units      5600      60/min  ##........
instagram_graph            37/200 calls         113      30/min  #.........
instagram_oembed            0/500 calls         450      30/min  ..........

instagram_hashtags        3/30 hashtags        27      30/min  #.........

Hashtags inside the 7-day window (re-querying these is free):
  #reels, #reelsinstagram, #viralreels

Held in reserve for scheduled runs (ad-hoc commands cannot spend this):
  youtube             2000 units
  instagram_graph     50 calls
```

Retries use **full jitter** — several collectors backing off on fixed intervals
would retry in lockstep and produce a synchronised burst that looks exactly like
an attack.

---

## How the ranking works

Each week's clips are scored by a **Viral Velocity Score** — a weighted sum of
eight components, each z-scored across that week's pool so the weights mean what
they say:

| Component | What it captures | Default weight |
|---|---|---|
| `velocity` | Views per hour since posting | **1.60** |
| `acceleration` | Is the view rate still climbing? | **1.30** |
| `magnitude` | log₁₀ of total views | 1.00 |
| `breadth` | How many platforms carry the same clip | 0.90 |
| `engagement_quality` | (likes + 3×comments) / views | 0.80 |
| `share_ratio` | Off-platform reposts per 100k views | 0.70 |
| `recency` | exp(−age_days / 3.5) | 0.60 |
| `topic_momentum` | Was the subject spiking that week? | 0.45 |

Velocity outweighs magnitude on purpose. A clip with 8M views in 30 hours is a
more useful object of study than one with 20M accumulated over a month — the
first is a live pattern, the second is history.

Two guardrails run after weighting: clips with no verifiable view count anywhere
take a fixed penalty (their size is *unknowable*, not small), and a creator
appearing more than twice in one week is progressively damped so a single
aggregator account can't own the board.

All of this lives in `config/weights.yaml`. Change it without touching code.

---

## How well does it work?

Measured, not asserted — full numbers and methodology in
**[EVALUATION.md](EVALUATION.md)**, reproducible with `python eval/harness.py`.

| What | Verdict |
|---|---|
| Ranking (the leaderboard) | ✅ Spearman **0.80** vs latent virality; 62% top-10 precision |
| Pattern mining, 1 week | ⚠️ Finds a real 2× effect **24%** of the time |
| Pattern mining, 6 weeks pooled | ✅ Finds it **96%** of the time |
| False patterns on pure noise | ✅ ~0.1 per report |

**The leaderboard is trustworthy from day one. The pattern mining needs about
six weeks of history before it is** — and the dashboard says so rather than
hiding it.

This matters because an earlier version of this code was badly wrong. Filtering
rules on lift alone, with no significance testing, it reported patterns in
**100% of weeks containing no signal whatsoever**, averaging 6.5 fabricated
"insights" each. Mining every attribute combination tests ~65 hypotheses per
run; without correction, a handful clear any threshold by chance. Rules are now
rank-tested and Benjamini-Hochberg corrected at a 10% false discovery rate, and
mining pools a rolling multi-week window because the real bottleneck turned out
to be sample size, not statistics.

**And the caveat that outranks all of the above: this has never been run against
real Instagram data.** Those numbers come from synthetic reels with known ground
truth — the only way to measure a false discovery rate at all. They say the
machinery is sound. They say nothing about whether the hook taxonomy in
`lexicons.yaml` matches how a human reads a hook.

---

## How "why did it go viral" works

Every clip is tagged with **craft features** — hook archetype (12 of them),
duration bucket, caption shape, hashtag count, topic, cross-post breadth. Then
[Apriori association-rule mining](https://en.wikipedia.org/wiki/Apriori_algorithm)
finds which combinations are over-represented in the week's top quartile, and by
how much.

Every candidate rule is a hypothesis, so every one gets a Mann-Whitney rank test
and the whole family gets Benjamini-Hochberg FDR correction. Rules ship with:

- **superiority** — the probability a clip with these attributes outranks one without. 0.5 = no effect. Reported because lift measurably *understates*: a planted 3× effect came back as 1.87× lift.
- **q-value** — FDR-adjusted significance, and how many hypotheses were tested to get it
- **n** and a 95% confidence interval on the hit rate
- a **strength label** driven by the q-value, not by lift — a big lift from six clips is not strong evidence, and the old heuristic happily called it "strong"

> `duration:short_7_15s + reach:cross_posted` → beats a random clip **71%** of
> the time (2.14× lift, 26 clips, q=0.004 across 58 tested)

If nothing clears significance, the report says so instead of padding.

**The honest caveat, which is also printed on the dashboard:** these are
correlations inside one week of survivor-biased data. A 2.1× lift on POV hooks
means POV hooks are over-represented among clips that already went big. It does
not mean typing "POV:" doubles your views. Treat every rule as a hypothesis to
A/B on your own account.

---

## How the recommender works

The design rule is **no unsourced advice**. If a suggestion can't cite the rule
it came from, its lift, and its sample size, it doesn't ship. Social tooling is
full of confident unfalsifiable advice; the point of building your own is to not
add to that pile.

- `plan()` — the strongest evidence-backed craft choices, optionally within your niche
- `predict()` — where a described reel would sit against this week's clips. It returns a *relative craft position*, never a view forecast, because nobody can forecast views from public signals and anyone who says otherwise is guessing
- `next_best_change()` — greedy single-edit search: of every change you could make, which one buys the most lift

### The calibration loop

This is where your own account earns its keep. `reelpulse calibrate` pulls your
reels' real view counts from the Graph API and fits the VVS weights against them
with ridge regression. Below 12 reels it refuses to fit and says so. Above that,
fitted weights are shrunk toward the shipped defaults in proportion to how
little data you have — 12 reels nudge the config, 60+ mostly replace it.

The global board teaches you the shape. Your own account teaches you the scale.

---

## What this does not do

- It does not report Instagram's real global view counts. Nobody outside Meta can.
- It does not scrape Instagram, use logged-in sessions, or touch private data.
- It does not predict view counts. It positions craft choices against observed outcomes.
- It does not prove causation. Association rules on survivor-biased data cannot.

See [LEGAL.md](LEGAL.md) for the boundaries, and [ARCHITECTURE.md](ARCHITECTURE.md)
for how the parts fit together.

---

## Tests

```bash
pytest tests/ -q
```

The suite asserts behaviour, not just imports. The load-bearing test is
`test_mining_recovers_planted_signal`: the demo generator plants known
relationships in synthetic data, and the miner has to find them. If it can't,
the analysis half of the project is broken even when everything imports fine.

## License

MIT. Use it, fork it, sell what you build on it.
