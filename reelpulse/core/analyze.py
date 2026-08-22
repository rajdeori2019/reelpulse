"""Per-reel "why did this get watched" write-up.

Two different questions get answered here and they should not be confused:

  * `explain()` in core/score.py says why a reel ranked where it ranked. That is
    a fact about the scoring maths.
  * `why_it_worked()` here says which *craft choices* the reel made that the
    week's data associates with outperformance. That is an inference, and every
    line of it carries the rule and sample size it came from.

Where a transcript is available (via yt-dlp on the public YouTube mirror, opt-in
and never on Instagram), the first three seconds get analysed separately —
because in short-form video the opening line is doing most of the work and
averaging it into the whole caption hides that.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from typing import Any

from ..models import Cluster
from .features import detect_hooks, craft_itemset
from .patterns import confidence_label, summarise_rule

log = logging.getLogger("reelpulse")


def fetch_transcript(cluster: Cluster, timeout: int = 45) -> str | None:
    """Auto-captions from the YouTube mirror, if yt-dlp is installed.

    Deliberately YouTube-only. yt-dlp is never pointed at Instagram here: that
    would mean pulling media Meta's terms do not permit us to pull, and the
    whole design premise of this project is that it stays inside sanctioned
    access. No transcript is a smaller loss than a takedown.
    """
    if not shutil.which("yt-dlp"):
        return None
    yt = next((m for m in cluster.members if m.platform == "youtube"), None)
    if not yt:
        return None

    try:
        result = subprocess.run(
            ["yt-dlp", "--skip-download", "--write-auto-subs", "--sub-lang", "en",
             "--sub-format", "vtt", "--output", "-", "--print", "%(title)s",
             "--quiet", "--no-warnings", yt.url],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        if result.returncode != 0:
            return None
        return None if not result.stdout else result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.debug("yt-dlp transcript failed: %s", exc)
        return None


def opening_line(cluster: Cluster) -> str:
    """Best available stand-in for the first three seconds."""
    primary = cluster.primary
    text = primary.title or primary.caption or ""
    text = re.sub(r"#\w+", "", text).strip()
    sentence = re.split(r"(?<=[.!?])\s", text)[0] if text else ""
    return (sentence or text)[:140]


def why_it_worked(cluster: Cluster, rules: list[dict[str, Any]],
                  max_reasons: int = 5) -> list[dict[str, Any]]:
    """Match this reel's craft attributes against the week's mined rules."""
    items = craft_itemset(cluster.tags)
    matched = []
    for rule in rules:
        if set(rule["antecedent"]) <= items:
            matched.append({
                "rule": summarise_rule(rule),
                "lift": rule["lift"],
                "n": rule["n"],
                "confidence": confidence_label(rule),
                "attributes": rule["antecedent"],
            })
    matched.sort(key=lambda r: (r["lift"], r["n"]), reverse=True)
    return matched[:max_reasons]


def structural_notes(cluster: Cluster) -> list[str]:
    """Observations that hold regardless of what this week's rules say."""
    tags = cluster.tags
    features = cluster.features
    notes: list[str] = []

    duration = tags.get("duration_s")
    if duration and duration <= 8:
        notes.append(
            f"{duration:.0f}s runtime — short enough that a rewatch costs the "
            "viewer nothing, which is how replay counts inflate view totals.")
    elif duration and duration >= 55:
        notes.append(
            f"{duration:.0f}s runtime — long for short-form, so it held "
            "attention on content rather than on loop mechanics.")

    if tags.get("primary_hook") != "none_detected":
        notes.append(
            f"Opens on a '{tags['primary_hook'].replace('_', ' ')}' hook: "
            f"\"{opening_line(cluster)}\"")

    if features.get("engagement_quality", 0) > 0:
        per_100k = features["engagement_quality"] * 100_000
        if per_100k > 4000:
            notes.append(
                f"{per_100k:,.0f} weighted engagements per 100k views — well "
                "above typical, meaning people argued about it, not just watched.")

    if cluster.breadth >= 3:
        notes.append(
            f"Present on {cluster.breadth} platforms ({', '.join(sorted(cluster.platforms))}) "
            "— reposting at that spread is the clearest free evidence of real global reach.")

    if features.get("topic_momentum", 0) > 0.6:
        entities = ", ".join(tags.get("entities", [])[:2])
        notes.append(
            f"Topic momentum was rising the same week ({entities}) — this reel "
            "caught a wave rather than starting one, so the format may not "
            "transfer to a cold topic.")
    elif features.get("topic_momentum", 0) <= 0.05 and features.get("views", 0) > 0:
        notes.append(
            "No detectable topic spike behind it — the performance came from "
            "execution, which is the more repeatable kind.")

    region_count = tags.get("region_count", 0)
    if region_count >= 3:
        notes.append(
            f"Surfaced in {region_count} regional result sets "
            f"({', '.join(tags.get('regions', [])[:5])}) — it crossed language "
            "or culture boundaries rather than peaking in one market.")

    if tags.get("hashtag_count", 0) == 0:
        notes.append("Zero hashtags — distribution came from the ranker, not from tags.")
    elif tags.get("hashtag_count", 0) >= 11:
        notes.append(
            f"{tags['hashtag_count']} hashtags — heavy tagging, which correlates "
            "with aggregator accounts more than with original creators.")

    return notes


def analyse(clusters: list[Cluster], rules: list[dict[str, Any]]) -> None:
    """Attach `why` to every cluster, in place."""
    for cluster in clusters:
        evidence = why_it_worked(cluster, rules)
        cluster.tags["evidence"] = evidence
        # Cached so recommend.predict() can position a plan against real peers
        # without recomputing the itemset for every candidate variant.
        cluster.tags["_itemset"] = sorted(craft_itemset(cluster.tags))
        cluster.why = structural_notes(cluster) + [
            f"[{item['confidence']}] {item['rule']}" for item in evidence
        ]
