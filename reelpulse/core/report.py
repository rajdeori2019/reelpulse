"""Build data/latest.json and render the static dashboard."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import Cluster
from .patterns import confidence_label, summarise_rule
from .score import explain


def week_key(when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    year, week, _ = when.isocalendar()
    return f"{year}-W{week:02d}"


def cluster_payload(cluster: Cluster) -> dict[str, Any]:
    primary = cluster.primary
    instagram = cluster.instagram
    return {
        "cluster_id": cluster.cluster_id,
        "rank": cluster.rank,
        "vvs": round(cluster.vvs, 4),
        "title": cluster.title[:200],
        "creator": primary.creator,
        "published_at": primary.published_at.isoformat() if primary.published_at else None,
        "duration_s": primary.duration_s,
        "views": cluster.best_views,
        # Which metric this row was ranked on. Surfaced rather than hidden:
        # "3.4M views" and "900k likes, no published view count" are different
        # claims and the reader is entitled to know which one they are reading.
        "measurement_basis": cluster.features.get("measurement_basis", "none"),
        "instagram_native": any(m.meta.get("instagram_native")
                                for m in cluster.members),
        "discovered_via": next((m.meta.get("discovered_via")
                                for m in cluster.members
                                if m.meta.get("discovered_via")), None),
        "likes": int(cluster.features.get("likes", 0)),
        "comments": int(cluster.features.get("comments", 0)),
        "shares": int(cluster.features.get("shares", 0)),
        "views_per_hour": round(10 ** cluster.features.get("velocity", 0) - 1, 1),
        "platforms": sorted(cluster.platforms),
        "breadth": cluster.breadth,
        "urls": {m.platform: m.url for m in cluster.members},
        "instagram_url": instagram.url if instagram else None,
        "instagram_embed": (instagram.meta.get("embed_html") if instagram else None),
        "thumbnail": primary.meta.get("thumbnail", ""),
        "regions": cluster.tags.get("regions", []),
        "tags": {k: v for k, v in cluster.tags.items()
                 if k not in {"evidence", "_itemset"}},
        "score_breakdown": {k: round(v, 3) for k, v in cluster.components.items()},
        "score_explanation": explain(cluster),
        "why_it_worked": cluster.why,
        "evidence": cluster.tags.get("evidence", []),
    }


def build_report(clusters: list[Cluster], rules: list[dict],
                 *, top_n: int = 10, benchmark: dict | None = None,
                 recommendations: list[dict] | None = None,
                 stats: dict | None = None) -> dict[str, Any]:
    top = clusters[:top_n]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "week": week_key(),
        "methodology": {
            "headline": ("Estimated ranking. Reels are discovered natively on "
                         "Instagram via Hashtag Search and Business Discovery, "
                         "and supplemented with YouTube Shorts cross-posts where "
                         "a view count is only available there. Rows are ranked "
                         "on whichever metric Instagram actually publishes for "
                         "them — views, or likes and comments — with each basis "
                         "standardised against its own cohort."),
            "not_claimed": ("This is not Meta's internal ranking and does not "
                            "reproduce it. No Instagram scraping is performed, "
                            "and no view count is ever estimated from likes."),
            "sources": ["Instagram Hashtag Search (Graph API)",
                        "Instagram Business Discovery (Graph API)",
                        "Instagram Graph API (own account)",
                        "Instagram oEmbed (tokenless)",
                        "YouTube Data API v3", "Reddit API",
                        "Wikimedia Pageviews API"],
        },
        "stats": stats or {},
        "top": [cluster_payload(c) for c in top],
        "patterns": [
            {
                "rule": summarise_rule(r),
                "antecedent": r["antecedent"],
                "lift": round(r["lift"], 3),
                # Probability a clip with these attributes outranks one without.
                # 0.5 = no effect. Reported alongside lift because lift measurably
                # under-states: a planted 3x effect came back as 1.87x.
                "superiority": r.get("superiority"),
                "support": round(r["support"], 3),
                "confidence_pct": round(r["confidence"] * 100, 1),
                "confidence_ci": r.get("confidence_ci"),
                "n": r["n"],
                # FDR-adjusted p-value, and how many hypotheses were tested to
                # get it. Without both, a reader cannot tell signal from the
                # multiple-testing artefacts this miner used to emit freely.
                "q_value": (round(r["q_value"], 4) if r.get("q_value") is not None
                            else None),
                "tested": r.get("tested"),
                "weeks_pooled": r.get("weeks_pooled", 1),
                "pool_size": r.get("pool_size"),
                "strength": confidence_label(r),
            }
            for r in rules[:20]
        ],
        "mining": {
            "clips_in_pool": (rules[0].get("pool_size") if rules else None),
            "weeks_pooled": (rules[0].get("weeks_pooled", 1) if rules else None),
            "hypotheses_tested": (rules[0].get("tested") if rules else None),
            "fdr_level": 0.10,
            "note": ("Rules are Fisher/rank tested and Benjamini-Hochberg "
                     "corrected at a 10% false discovery rate. Statistical power "
                     "depends on pool size: measured recall of a real 2x effect "
                     "was 16% at 200 clips, 76% at 800, and 96% at 1200. Few or "
                     "no rules usually means not enough history yet, not that "
                     "nothing is true."),
        },
        "recommendations": recommendations or [],
        "benchmark": benchmark or {"available": False},
    }


def write_report(report: dict, out_dir: str | Path = "data") -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    latest = out_dir / "latest.json"
    latest.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    archive = out_dir / "weeks"
    archive.mkdir(exist_ok=True)
    (archive / f"{report['week']}.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return latest


def render_dashboard(report: dict, template: str | Path,
                     out_path: str | Path = "docs/index.html") -> Path:
    """Inline the report into the HTML so the dashboard is a single file.

    No fetch(), no CORS, no web server — open it from disk, drop it on GitHub
    Pages, or email it. That is the whole reason it is one file.
    """
    template_text = Path(template).read_text(encoding="utf-8")
    payload = json.dumps(report, ensure_ascii=False).replace("</", "<\\/")
    html = template_text.replace("/*__REELPULSE_DATA__*/null", payload)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path
