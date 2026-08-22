"""ReelPulse command line.

    reelpulse doctor      what is configured, what is missing, what that costs you
    reelpulse collect     run every enabled collector, store a snapshot
    reelpulse run         collect -> cluster -> score -> mine -> report -> dashboard
    reelpulse calibrate   fit VVS weights against your own Graph API numbers
    reelpulse advise      evidence-backed recommendations for a planned reel
    reelpulse demo        seed synthetic data and run the whole pipeline offline
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click

from . import config as cfg
from .collectors import (InstagramDiscoveryCollector, InstagramGraphCollector,
                         InstagramHashtagCollector, InstagramOEmbedCollector,
                         RedditCollector, TopicMomentumCollector, YouTubeCollector)
from .core.analyze import analyse
from .core.calibrate import calibrate as run_calibrate
from .core.dedupe import cluster_candidates
from .core.features import craft_itemset
from .core.patterns import mine_from_rows, mine_rules
from .core.recommend import ReelPlan, benchmark_against_own, next_best_change, plan, predict
from .core.report import build_report, render_dashboard, week_key, write_report
from .core.score import score_clusters
from .db import Store
from .limits import LIMITS, RateLimiter
from .models import Candidate

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("reelpulse")

TEMPLATE = Path(__file__).resolve().parent / "dashboard" / "template.html"


@click.group()
@click.option("--db", default="data/reelpulse.db", show_default=True,
              help="SQLite path.")
@click.pass_context
def main(ctx: click.Context, db: str) -> None:
    """Weekly global short-form leaderboard + viral pattern analysis."""
    ctx.ensure_object(dict)
    ctx.obj["db"] = db


# ---------------------------------------------------------------------------

@main.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Show what is wired up and exactly what each missing key costs you."""
    checks = [
        ("IG_ACCESS_TOKEN", "Instagram (native discovery)", True,
         "Free with any Instagram Business/Creator account. This is what makes "
         "ReelPulse an Instagram tool: it unlocks Hashtag Search (other people's "
         "public reels, with likes and comments), Business Discovery (real view "
         "counts on a creator watchlist), your own insights, and weight "
         "calibration. Without it, discovery falls back to YouTube cross-posts "
         "only and Instagram-native reels are invisible."),
        ("IG_USER_ID", "Instagram user id", True, "Pairs with IG_ACCESS_TOKEN."),
        ("YOUTUBE_API_KEY", "YouTube Data API v3", True,
         "Free, 10k units/day. Supplies view counts at global scale for clips "
         "cross-posted to Shorts — the only free view-count source for reels "
         "outside a watchlist. Without it, ranking runs on engagement scale."),
        ("REDDIT_CLIENT_ID", "Reddit API", False,
         "Free OAuth. Supplies the off-platform share signal and real "
         "instagram.com/reel/ permalinks. Without it, breadth is weaker."),
        ("REDDIT_CLIENT_SECRET", "Reddit API secret", False, "Pairs with the client id."),
    ]

    click.echo("\nReelPulse configuration\n" + "-" * 60)
    blocking = False
    for key, label, required, note in checks:
        ok = cfg.has(key)
        mark = "OK  " if ok else ("MISS" if required else "----")
        click.echo(f"[{mark}] {label:<28} {key}")
        if not ok:
            click.echo(f"       {note}")
            if required:
                blocking = True

    click.echo("\nTokenless (no setup needed):")
    click.echo("  [OK  ] Instagram oEmbed        (Meta, tokenless since 15 Jun 2026)")
    click.echo("  [OK  ] Wikimedia Pageviews     (no key, no quota)")

    # The hashtag window is a hard Meta limit and the easiest thing to blow
    # through by accident, so surface what is left before anyone plans a sweep.
    if cfg.has("IG_ACCESS_TOKEN") and cfg.has("IG_USER_ID"):
        try:
            from .collectors import HashtagBudget
            budget = HashtagBudget(Store(ctx.obj["db"]))
            click.echo(f"\nInstagram hashtag budget: {budget.remaining()}/30 "
                       f"unique hashtags left in the rolling 7-day window")
            if budget.remaining() == 0:
                click.echo("  Exhausted. Instagram hashtag discovery is paused "
                           "until the oldest queries age out.")
        except Exception:  # noqa: BLE001
            pass
    click.echo("\nOptional extras:")
    click.echo("  yt-dlp installed:      " + ("yes" if _which("yt-dlp") else "no  (transcripts disabled)"))
    click.echo("  pytrends installed:    " + ("yes" if _importable("pytrends") else "no  (Wikipedia used instead)"))

    if blocking:
        click.echo("\nRun `reelpulse demo` to see the full pipeline with synthetic "
                   "data while you get a YouTube key.")
    click.echo()


def _which(binary: str) -> bool:
    import shutil
    return shutil.which(binary) is not None


def _importable(module: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(module) is not None


# ---------------------------------------------------------------------------

def _collect(sources: dict, store: Store | None = None,
             limiter: RateLimiter | None = None) -> list[Candidate]:
    window = int(sources.get("window_days", 7))

    youtube_cfg = dict(sources.get("youtube", {}))
    youtube_cfg["_window_days"] = window

    # ONE limiter shared by every collector. Two Instagram collectors hitting
    # the same app-level hourly budget must draw from the same ledger, or each
    # thinks it has the whole allowance and together they breach it.
    limiter = limiter or RateLimiter(store)

    candidates: list[Candidate] = []

    # Instagram-native discovery first, so that if a later source fails the run
    # still has genuine Instagram content rather than a YouTube-only board.
    candidates += InstagramHashtagCollector(
        sources.get("instagram_hashtag", {}), store, limiter).safe_collect()
    candidates += InstagramDiscoveryCollector(
        sources.get("instagram_discovery", {}), limiter).safe_collect()

    candidates += YouTubeCollector(youtube_cfg, limiter).safe_collect()
    candidates += RedditCollector(sources.get("reddit", {}), limiter).safe_collect()

    native = sum(1 for c in candidates if c.meta.get("instagram_native"))
    log.info("collected %d candidates (%d Instagram-native, %d from other sources)",
             len(candidates), native, len(candidates) - native)
    if not native:
        log.warning("No Instagram-native results. Set IG_ACCESS_TOKEN + IG_USER_ID "
                    "to discover reels directly on Instagram instead of relying "
                    "on YouTube cross-posts.")

    oembed = InstagramOEmbedCollector(sources.get("instagram_oembed", {}), limiter)
    oembed.enrich(candidates)
    return candidates


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def limits(ctx: click.Context, as_json: bool) -> None:
    """Show API budget spend, pacing and any active cooldowns.

    Check this before a big sweep, and after anything looks throttled.
    """
    store = Store(ctx.obj["db"])
    limiter = RateLimiter(store)
    limiter.prune()
    rows = limiter.status()

    if as_json:
        click.echo(json.dumps(rows, indent=2))
        store.close()
        return

    click.echo("\nAPI budgets\n" + "-" * 76)
    click.echo(f"{'service':<20}{'used':>18}  {'left':>10}  {'pace':>9}  status")
    for row in rows:
        if row["quota"] is None:
            used = "no published cap"
            left = "-"
            bar = ""
        else:
            used = f"{row['spent']:g}/{row['quota']:g} {row['unit']}"
            left = f"{row['remaining']:g}"
            filled = int((row["pct_used"] or 0) / 10)
            bar = "#" * min(filled, 10) + "." * max(0, 10 - filled)

        if row["cooling_down_until"]:
            status = f"COOLING DOWN until {row['cooling_down_until'][11:19]}Z"
        elif row["self_throttle_s"]:
            status = f"self-throttling +{row['self_throttle_s']}s/req"
        elif row["quota"] and (row["pct_used"] or 0) > 80:
            status = f"{bar} tight"
        elif bar:
            status = bar
        else:
            status = "ok"

        click.echo(f"{row['service']:<20}{used:>18}  {left:>10}  "
                   f"{row['per_minute']:>6g}/min  {status}")

    # The hashtag window is a separate Meta limit with its own accounting.
    try:
        from .collectors import HashtagBudget
        budget = HashtagBudget(store)
        click.echo(f"\nInstagram hashtags: {budget.remaining()}/30 unique left "
                   f"in the rolling 7-day window")
        spent = sorted(budget.spent())
        if spent:
            click.echo("  spent on: " + ", ".join("#" + h for h in spent[:12])
                       + (f" (+{len(spent) - 12} more)" if len(spent) > 12 else ""))
    except Exception:  # noqa: BLE001
        pass

    reserved = [r for r in rows if r["reserved"]]
    if reserved:
        click.echo("\nHeld in reserve for scheduled runs (ad-hoc commands cannot "
                   "spend this):")
        for row in reserved:
            click.echo(f"  {row['service']:<20}{row['reserved']:g} {row['unit']}")
    click.echo()
    store.close()


@main.command()
@click.pass_context
def collect(ctx: click.Context) -> None:
    """Run collectors once and store a snapshot (this is the daily cron job)."""
    sources = cfg.load_sources()
    store = Store(ctx.obj["db"])
    candidates = _collect(sources, store)
    stored = store.upsert_candidates(candidates)
    click.echo(f"stored {stored} candidate snapshots")

    graph = InstagramGraphCollector(sources.get("instagram_graph", {}),
                                    RateLimiter(store))
    own = graph.fetch_own_media()
    if own:
        store.save_own_media(own)
        click.echo(f"stored {len(own)} of your own media")
    store.close()


# ---------------------------------------------------------------------------

@main.command()
@click.option("--top-n", default=None, type=int, help="Leaderboard size.")
@click.option("--skip-collect", is_flag=True,
              help="Score whatever is already in the database.")
@click.option("--no-momentum", is_flag=True,
              help="Skip topic-momentum lookups (much faster).")
@click.option("--pool-weeks", default=4, show_default=True,
              help="Weeks of history to pool for pattern mining. More weeks = "
                   "more statistical power and steadier advice.")
@click.option("--out", default="docs/index.html", show_default=True)
@click.pass_context
def run(ctx: click.Context, top_n: int | None, skip_collect: bool,
        no_momentum: bool, pool_weeks: int, out: str) -> None:
    """The full weekly pipeline."""
    sources = cfg.load_sources()
    top_n = top_n or int(sources.get("top_n", 10))
    store = Store(ctx.obj["db"])

    # 1. collect
    if skip_collect:
        candidates = _load_from_db(store, int(sources.get("window_days", 7)))
        click.echo(f"loaded {len(candidates)} candidates from the database")
    else:
        candidates = _collect(sources, store)
        store.upsert_candidates(candidates)

    if not candidates:
        click.echo("No candidates. Run `reelpulse doctor`, or `reelpulse demo` "
                   "to try the pipeline offline.", err=True)
        sys.exit(1)

    minimum = int(sources.get("min_candidates", 40))
    if len(candidates) < minimum:
        log.warning("only %d candidates (want >=%d) — weekly z-scores will be "
                    "noisy; widen `queries` or `regions` in config/sources.yaml",
                    len(candidates), minimum)

    # 2. cluster
    clusters = cluster_candidates(candidates)
    click.echo(f"{len(candidates)} candidates -> {len(clusters)} distinct clips")

    # 3. score
    momentum_fn = None
    if not no_momentum:
        momentum = TopicMomentumCollector(sources.get("trends", {}),
                                          sources.get("wikipedia", {}),
                                          RateLimiter(store))
        momentum_fn = momentum.momentum_for

    ranked = score_clusters(clusters, store.prior_snapshot, momentum_fn=momentum_fn)

    # 4. mine patterns, then explain each reel using them
    #
    # Mining pools the last few weeks rather than this week alone. One week of
    # a few hundred clips does not contain enough signal to survive
    # multiple-testing correction: measured recall of a planted 3x effect was
    # 4/12 at 200 clips and 12/12 at 800. Pooling is also what makes the advice
    # reproduce week to week instead of churning.
    week = week_key()
    store.save_mining_rows(week, [(c.cluster_id, sorted(craft_itemset(c.tags)), c.vvs)
                                  for c in ranked])
    pooled, spanned = store.mining_rows(weeks=pool_weeks)
    if len(pooled) >= len(ranked):
        rules = mine_from_rows(pooled, weeks_pooled=spanned)
        click.echo(f"mined {len(rules)} patterns from {len(pooled)} clips "
                   f"across {spanned} week(s)")
        if spanned < 3:
            click.echo("  (only %d week(s) of history — patterns get markedly "
                       "more reliable from ~3 weeks on)" % spanned)
    else:
        rules = mine_rules(ranked)
        click.echo(f"mined {len(rules)} patterns from this week alone")
    analyse(ranked, rules)

    # 5. recommend + benchmark
    own = store.own_media()
    report = build_report(
        ranked, rules, top_n=top_n,
        benchmark=benchmark_against_own(own, ranked),
        recommendations=plan(rules, limit=8),
        stats={
            "candidates": len(candidates),
            "clusters": len(clusters),
            "with_view_counts": sum(1 for c in ranked if c.best_views),
            "instagram_permalinks": sum(1 for c in ranked if c.instagram),
            "platforms": sorted({p for c in ranked for p in c.platforms}),
        },
    )

    # 6. persist + render
    store.save_clusters(week, report["top"])
    store.save_rules(week, rules)
    path = write_report(report)
    dashboard = render_dashboard(report, TEMPLATE, out)
    store.close()

    click.echo(f"\nreport   -> {path}")
    click.echo(f"dashboard-> {dashboard}")
    click.echo(f"\nTop {min(top_n, len(ranked))} this week ({week}):")
    for item in report["top"]:
        views = f"{item['views']:,}" if item["views"] else "n/a"
        click.echo(f"  {item['rank']:>2}. [VVS {item['vvs']:+.2f}] {item['title'][:62]}")
        click.echo(f"      {views} views | {'+'.join(item['platforms'])} | {item['creator'][:30]}")


def _load_from_db(store: Store, window_days: int) -> list[Candidate]:
    import json as _json
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    cur = store.conn.execute(
        """SELECT c.*, s.views, s.likes, s.comments, s.shares, s.saves, s.collected_at
           FROM candidates c
           JOIN snapshots s ON s.fingerprint = c.fingerprint
           WHERE s.collected_at = (SELECT MAX(collected_at) FROM snapshots
                                   WHERE fingerprint = c.fingerprint)
             AND (c.published_at IS NULL OR c.published_at >= ?)""",
        (cutoff,),
    )
    out = []
    for row in cur.fetchall():
        out.append(Candidate(
            platform=row["platform"], platform_id=row["platform_id"], url=row["url"],
            title=row["title"] or "", caption=row["caption"] or "",
            creator=row["creator"] or "", creator_id=row["creator_id"] or "",
            published_at=row["published_at"], duration_s=row["duration_s"],
            views=row["views"], likes=row["likes"], comments=row["comments"],
            shares=row["shares"], saves=row["saves"],
            meta=_json.loads(row["meta"] or "{}"),
            collected_at=row["collected_at"],
        ))
    return out


# ---------------------------------------------------------------------------

@main.command()
@click.option("--dry-run", is_flag=True, help="Show the fit without writing weights.yaml.")
@click.pass_context
def calibrate(ctx: click.Context, dry_run: bool) -> None:
    """Fit VVS weights against your own Instagram Graph API view counts."""
    sources = cfg.load_sources()
    store = Store(ctx.obj["db"])

    own = store.own_media()
    if not own:
        own = InstagramGraphCollector(sources.get("instagram_graph", {})).fetch_own_media()
        if own:
            store.save_own_media(own)

    result = run_calibrate(own, write=not dry_run)
    store.close()
    click.echo(json.dumps(result, indent=2))


# ---------------------------------------------------------------------------

@main.command()
@click.option("--topic", default="uncategorised", help="Your niche, e.g. food, fitness.")
@click.option("--hook", default="none_detected", help="Planned hook archetype.")
@click.option("--duration", default=15.0, type=float, help="Planned runtime in seconds.")
@click.option("--caption-words", default=8, type=int)
@click.option("--hashtags", default=3, type=int)
@click.option("--question", is_flag=True, help="Caption ends on a question.")
@click.option("--cta", is_flag=True, help="Caption has a call to action.")
@click.option("--cross-post", is_flag=True, help="Same cut goes to Shorts/TikTok too.")
@click.option("--week", default=None, help="Use rules mined in a specific week.")
@click.pass_context
def advise(ctx: click.Context, topic: str, hook: str, duration: float,
           caption_words: int, hashtags: int, question: bool, cta: bool,
           cross_post: bool, week: str | None) -> None:
    """Evidence-backed recommendations for a reel you are planning."""
    store = Store(ctx.obj["db"])
    raw = store.rules(week)
    store.close()

    if not raw:
        click.echo("No mined rules yet. Run `reelpulse run` (or `reelpulse demo`) "
                   "at least once first.", err=True)
        sys.exit(1)

    rules = [{
        "antecedent": r["antecedent"].split(" & "),
        "consequent": r["consequent"], "support": r["support"],
        "confidence": r["confidence"], "lift": r["lift"], "n": r["n"],
        "base_rate": 0.25,
    } for r in raw]

    reel = ReelPlan(topic=topic, hook=hook, duration_s=duration,
                    caption_words=caption_words, hashtag_count=hashtags,
                    has_question=question, has_cta=cta, cross_posted=cross_post)

    click.echo("\nYour plan")
    click.echo(json.dumps(predict(reel, rules), indent=2))

    click.echo("\nHighest-leverage single changes")
    changes = next_best_change(reel, rules)
    if not changes:
        click.echo("  Nothing in this week's data beats your current plan.")
    for i, change in enumerate(changes, 1):
        click.echo(f"  {i}. {change['change']}")
        click.echo(f"     +{change['expected_gain']} craft score "
                   f"({change['confidence']}, n={change['evidence_sample_size']})")

    scoped = plan(rules, niche=topic, limit=5)
    in_niche = sum(1 for item in scoped if item["scoped_to_niche"])
    if in_niche:
        click.echo(f"\nStrongest patterns in {topic}")
    else:
        # Say so rather than passing cross-niche rules off as niche insight.
        click.echo(f"\nStrongest patterns overall "
                   f"(too few {topic}-specific clips this week to scope to it)")
    for item in scoped:
        click.echo(f"  - {item['recommendation']}")
        click.echo(f"    {item['evidence']}")

    click.echo("\nThese are correlations from one week of survivor-biased data. "
               "Treat each as a hypothesis to A/B, not a rule.\n")


# ---------------------------------------------------------------------------

@main.command()
@click.argument("query")
@click.option("--days", default=7, show_default=True, help="Look-back window.")
@click.option("--top", default=20, show_default=True, help="How many to show.")
@click.option("--regions", default=None,
              help="Comma-separated ISO codes. Default: config sources.yaml.")
@click.option("--variants", default=3, show_default=True,
              help="Query variants to try (each x region costs 100 quota units).")
@click.option("--max-searches", default=12, show_default=True,
              help="Hard cap on search.list calls. 12 = 1,200 of your 10k daily units.")
@click.option("--min-relevance", default=0.4, show_default=True,
              help="0.4 keeps partial matches; 0.7 requires a full term match; "
                   "0.9 demands the term in the title.")
@click.option("--anchor/--no-anchor", default=True, show_default=True,
              help="Z-score against your stored global pool, not just these results.")
@click.option("--instagram/--no-instagram", default=True, show_default=True,
              help="Also query Instagram Hashtag Search. Spends from the hard "
                   "30-unique-hashtags-per-7-days budget.")
@click.option("--out", default=None, help="Also write a dashboard here.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def search(ctx: click.Context, query: str, days: int, top: int,
           regions: str | None, variants: int, max_searches: int,
           min_relevance: float, anchor: bool, instagram: bool,
           out: str | None, as_json: bool) -> None:
    """Find trending reels for a KEYWORD and stack rank them.

    \b
      reelpulse search "sourdough"
      reelpulse search '"cold plunge" -ad' --days 14 --top 30
      reelpulse search "pilates OR reformer" --min-relevance 0.7

    Query syntax: quoted "exact phrase", OR between terms, -excluded.
    """
    from .core.relevance import expand, filter_candidates, parse_query

    sources = cfg.load_sources()
    store = Store(ctx.obj["db"])

    limiter = RateLimiter(store)
    parsed = parse_query(query)
    if not parsed.terms:
        click.echo("Nothing searchable in that query.", err=True)
        sys.exit(1)

    region_list = ([r.strip().upper() for r in regions.split(",")] if regions
                   else sources.get("youtube", {}).get("regions", ["US"])[:6])

    # ---- collect -------------------------------------------------------
    found: list[Candidate] = []

    # Instagram-native first. A keyword search should look on Instagram before
    # it looks at Instagram's shadow on other platforms.
    if instagram:
        hashtag_terms = [t.replace(" ", "") for t in (parsed.phrases + parsed.required
                                                      + parsed.optional)][:2]
        ig = InstagramHashtagCollector(sources.get("instagram_hashtag", {}),
                                       store, limiter)
        if ig.budget and ig.budget.remaining() == 0:
            click.echo("Instagram hashtag budget exhausted for this 7-day window "
                       "— searching other sources only.", err=True)
        else:
            before = ig.budget.remaining() if ig.budget else None
            found += ig.discover(hashtag_terms, edge="top_media")
            if before is not None:
                after = ig.budget.remaining()
                if after < before:
                    click.echo(f"Instagram hashtag budget: {after}/30 left this "
                               f"week (this search spent {before - after})")

    youtube_cfg = dict(sources.get("youtube", {}))
    found += YouTubeCollector(youtube_cfg, limiter).search_keyword(
        expand(parsed, extra=variants - 1), regions=region_list,
        days=days, max_searches=max_searches)

    reddit = RedditCollector(sources.get("reddit", {}), limiter)
    found += reddit.search(" ".join(parsed.terms),
                           time_filter="week" if days <= 7 else "month")

    if not found:
        click.echo("Nothing came back. Check `reelpulse doctor` — keyword search "
                   "needs IG_ACCESS_TOKEN (Instagram) and/or YOUTUBE_API_KEY.",
                   err=True)
        sys.exit(1)

    # ---- relevance gate ------------------------------------------------
    relevant, tally = filter_candidates(found, parsed, min_relevance)
    click.echo(f"\n{len(found)} fetched -> {len(relevant)} relevant "
               f"(tiers: {', '.join(f'{k}={v}' for k, v in sorted(tally.items()) if v)})")

    if not relevant:
        click.echo(f"\nNothing cleared the relevance bar for '{query}'. Either "
                   f"the term is too niche for this week, or try "
                   f"--min-relevance 0.3.", err=True)
        store.close()
        sys.exit(1)

    # Enrich Instagram permalinks so results are clickable reels, not bare ids.
    InstagramOEmbedCollector(sources.get("instagram_oembed", {}), limiter).enrich(relevant)
    store.upsert_candidates(relevant)

    # ---- score ---------------------------------------------------------
    # Anchoring matters. VVS z-scores every component across the pool it is
    # given. Score a keyword's results alone and the best sourdough clip of a
    # quiet week gets the same "+3.1" as a genuine global smash — a big fish in
    # a pond of five. Mixing in the stored global pool keeps the scale honest;
    # only the keyword matches are displayed.
    scoring_pool = list(relevant)
    if anchor:
        keys = {c.key for c in relevant}
        scoring_pool += [c for c in _load_from_db(store, days) if c.key not in keys]

    clusters = cluster_candidates(scoring_pool)
    ranked = score_clusters(clusters, store.prior_snapshot, momentum_fn=None)

    relevant_keys = {c.key for c in relevant}
    hits = [c for c in ranked if any(m.key in relevant_keys for m in c.members)]

    # Whether anchoring actually happened is decided on DISTINCT background
    # clips, after clustering — not on raw candidate count before it. Sixty
    # stored candidates that dedupe down to four clips anchor nothing, and
    # reporting that run as "anchored" would overstate the scores' comparability.
    MIN_ANCHOR_CLIPS = 15
    background_clips = len(ranked) - len(hits)
    anchored = anchor and background_clips >= MIN_ANCHOR_CLIPS

    # `rank` off score_clusters is the position within the whole scoring pool.
    # When anchored that pool includes the background, so a clip can be #1 for
    # the keyword and #37 overall. Both are worth knowing: display rank becomes
    # the keyword position, and the global position is kept as context.
    for position, cluster in enumerate(hits, start=1):
        cluster.tags["global_rank"] = cluster.rank
        cluster.tags["global_pool_size"] = len(ranked)
        cluster.tags["keyword_rank"] = position
        cluster.rank = position

    # Patterns mined from the keyword pool only — this is what "why do reels
    # about X do well" actually means.
    rules = mine_rules(hits) if len(hits) >= 8 else []
    analyse(hits, rules)

    # ---- output --------------------------------------------------------
    # Everything in the scoring pool that is NOT a keyword match becomes the
    # reference distribution for "is this actually big".
    hit_ids = {id(c) for c in hits}
    background_scales = [c.features.get("scale", 0.0) for c in ranked
                         if id(c) not in hit_ids and c.features.get("scale")]

    payload = build_report(hits, rules, top_n=top,
                           background_scales=background_scales,
                           recommendations=plan(rules, limit=6),
                           benchmark={"available": False,
                                      "reason": "keyword search mode"},
                           stats={"query": query, "fetched": len(found),
                                  "relevant": len(relevant),
                                  "anchored": anchored,
                                  "background_clips": background_clips,
                                  "match_tiers": tally})
    payload["methodology"]["headline"] = (
        f"Trending short-form clips matching '{query}' over the last {days} days, "
        f"stack ranked by Viral Velocity Score"
        + (". Scores are anchored against the wider stored pool, so a rank here "
           "is comparable to the weekly leaderboard."
           if anchored else
           f". NOT anchored — only {background_clips} background clips in "
           f"storage (need {MIN_ANCHOR_CLIPS}), so scores are relative to these "
           "results only and are not comparable across searches. Run "
           "`reelpulse collect` a few times to build a baseline."))

    reach = payload["reach"]

    if as_json:
        click.echo(json.dumps(payload, indent=2))
    else:
        # The verdict goes ABOVE the ranking. Printed underneath, it reads as a
        # footnote to a leaderboard; printed above, it frames what follows.
        if reach["verdict"] == "nothing_viral":
            click.echo("\n" + "=" * 68)
            click.echo("  NOTHING HERE WENT VIRAL")
            click.echo("=" * 68)
            click.echo("  " + reach["headline"].replace("**", ""))
            click.echo("=" * 68)
        else:
            click.echo(f"\n{reach['headline']}")

        click.echo(f"\nTop {min(top, len(hits))} for '{query}'"
                   f"{'' if anchored else '  [UNANCHORED — see note below]'}\n")
        for cluster, item in zip(hits, payload["top"]):
            member = next((m for m in cluster.members if "relevance" in m.meta), None)
            tier = member.meta.get("match_tier", "?") if member else "?"
            views = f"{item['views']:,}" if item["views"] else "no view count"
            overall = (f" | #{item['tags']['global_rank']} of "
                       f"{item['tags']['global_pool_size']} overall"
                       if anchored and item.get("tags", {}).get("global_rank") else "")
            click.echo(f"{item['rank']:>3}. [VVS {item['vvs']:+.2f}] {item['title'][:64]}")
            pct = item.get("reach_percentile")
            band = ("" if pct is None else
                    f" (top {100 - pct:.0f}%)" if pct >= 50 else
                    f" (bottom {max(pct, 1):.0f}%)")
            reach_note = (f" | reach: {item['reach_tier']}{band}"
                          if item.get("reach_tier") else "")
            click.echo(f"     {views} | {item['views_per_hour']:,.0f}/hr | "
                       f"{'+'.join(item['platforms'])} | match: {tier}{overall}"
                       f"{reach_note}")
            if item.get("instagram_url"):
                click.echo(f"     {item['instagram_url']}")

        if rules:
            click.echo(f"\nWhat the winners for '{query}' have in common:")
            for pattern in payload["patterns"][:5]:
                click.echo(f"  {pattern['lift']:.2f}x (n={pattern['n']}, "
                           f"{pattern['strength']}) {' + '.join(pattern['antecedent'])}")
        else:
            click.echo(f"\nOnly {len(hits)} clips matched — too few to mine "
                       "reliable patterns. Widen --days or lower --min-relevance.")

        if not anchored:
            click.echo(f"\nNote: unanchored ({background_clips} background clips "
                       f"stored, need {MIN_ANCHOR_CLIPS}). Scores are relative to "
                       "this result set only — the top result here is the best of "
                       "these, not necessarily a big reel. Run `reelpulse collect` "
                       "a few times, then re-search for scores comparable to the "
                       "weekly board.")

    if out:
        click.echo(f"\ndashboard -> {render_dashboard(payload, TEMPLATE, out)}")
    store.close()


@main.command()
@click.option("--n", default=140, show_default=True, help="Synthetic clips to generate.")
@click.option("--out", default="docs/index.html", show_default=True)
@click.pass_context
def demo(ctx: click.Context, n: int, out: str) -> None:
    """Run the entire pipeline offline on synthetic data. No keys needed."""
    from scripts.seed_demo import synthesize

    store = Store(ctx.obj["db"])
    candidates = synthesize(n)
    store.upsert_candidates(candidates)

    clusters = cluster_candidates(candidates)
    ranked = score_clusters(clusters, store.prior_snapshot, momentum_fn=None)
    rules = mine_rules(ranked)
    analyse(ranked, rules)

    report = build_report(ranked, rules, top_n=10,
                          recommendations=plan(rules, limit=8),
                          benchmark={"available": False,
                                     "reason": "demo mode — no Graph API data"},
                          stats={"candidates": len(candidates),
                                 "clusters": len(clusters),
                                 "mode": "DEMO — synthetic data, not real reels"})
    report["methodology"]["headline"] = (
        "DEMO MODE. Every clip below is synthetic. This exists to show the "
        "pipeline end to end without API keys.")

    week = week_key()
    store.save_clusters(week, report["top"])
    store.save_rules(week, rules)
    write_report(report)
    dashboard = render_dashboard(report, TEMPLATE, out)
    store.close()

    click.echo(f"\ndemo dashboard -> {dashboard}")
    click.echo(f"{len(candidates)} synthetic candidates -> {len(clusters)} clips, "
               f"{len(rules)} patterns mined")
    for item in report["top"][:10]:
        click.echo(f"  {item['rank']:>2}. [VVS {item['vvs']:+.2f}] {item['title'][:60]}")


if __name__ == "__main__":
    main(obj={})
