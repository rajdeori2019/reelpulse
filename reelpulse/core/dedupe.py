"""Cluster the same clip across platforms.

The same 11-second video shows up as a Reel, a Short, a Reddit post and three
aggregator re-uploads. Counted separately it fragments the leaderboard; counted
together, the *number of places it appears* becomes one of the strongest free
signals of genuine global reach. So dedupe is not housekeeping here — it
produces the `breadth` feature.

Matching is deliberately conservative: it is much worse to merge two different
clips (which fabricates breadth) than to miss a merge (which merely undercounts).
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict

from rapidfuzz import fuzz

from ..models import Candidate, Cluster

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")

BOILERPLATE = {
    "shorts", "short", "reels", "reel", "viral", "trending", "youtubeshorts",
    "fyp", "foryou", "foryoupage", "explore", "instagram", "tiktok", "video",
    "funny", "subscribe", "follow", "like", "share", "comment", "new", "best",
}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _PUNCT.sub(" ", text.lower())
    return _WS.sub(" ", text).strip()


def content_tokens(cand: Candidate) -> set[str]:
    tokens = {t for t in normalize(cand.text).split() if len(t) > 2}
    return tokens - BOILERPLATE


def _duration_compatible(a: Candidate, b: Candidate, tol: float = 2.0) -> bool:
    """Same clip => near-identical runtime. Unknown duration is not disqualifying."""
    if a.duration_s is None or b.duration_s is None:
        return True
    return abs(a.duration_s - b.duration_s) <= tol


def build_idf(candidates: list[Candidate]) -> dict[str, float]:
    """Inverse document frequency over the candidate pool.

    This is what stops templated titles from collapsing into one clip. Plain
    Jaccard treats "how", "fix", "your", "seconds" as equal evidence to
    "training" and "coding" — so "How to fix your training in 10 seconds" and
    "How to fix your coding in 10 seconds" score 0.78 similar and merge, even
    though they are unrelated videos. IDF makes the shared template words nearly
    weightless and puts the decision on the words that actually name the subject.
    """
    from math import log

    n = max(len(candidates), 1)
    df: dict[str, int] = defaultdict(int)
    for cand in candidates:
        for token in content_tokens(cand):
            df[token] += 1
    return {token: log(n / (1 + count)) + 1.0 for token, count in df.items()}


def _weighted_overlap(tok_a: set[str], tok_b: set[str],
                      idf: dict[str, float]) -> float:
    union = tok_a | tok_b
    if not union:
        return 0.0
    weight = lambda t: idf.get(t, 1.0)  # noqa: E731
    denom = sum(weight(t) for t in union)
    if denom <= 0:
        return 0.0
    return sum(weight(t) for t in (tok_a & tok_b)) / denom


def distinctive_cutoff(idf: dict[str, float], percentile: float = 60.0) -> float:
    """IDF above which a token is treated as naming the subject, not the template."""
    if not idf:
        return float("inf")
    import numpy as np
    return float(np.percentile(list(idf.values()), percentile))


def similarity(a: Candidate, b: Candidate,
               idf: dict[str, float] | None = None,
               cutoff: float | None = None) -> float:
    """0..1. Biased hard toward false negatives: a missed merge undercounts
    breadth, a wrong merge fabricates it."""
    if not _duration_compatible(a, b):
        return 0.0

    text_a, text_b = normalize(a.text)[:300], normalize(b.text)[:300]
    if not text_a or not text_b:
        return 0.0

    tok_a, tok_b = content_tokens(a), content_tokens(b)
    idf = idf or {}
    same_creator = bool(a.creator and b.creator
                        and normalize(a.creator) == normalize(b.creator))

    # --- the decisive gate ---------------------------------------------
    # If each side names a subject the other never mentions, they are different
    # clips — however much template they share. "How to fix your TRAINING in 10
    # seconds" and "How to fix your CODING in 10 seconds" fail here immediately,
    # and they fail on a pool of five candidates as readily as on five thousand,
    # which a purely IDF-mass threshold does not.
    if cutoff is not None and not same_creator:
        only_a = {t for t in tok_a - tok_b if idf.get(t, 1.0) >= cutoff}
        only_b = {t for t in tok_b - tok_a if idf.get(t, 1.0) >= cutoff}
        if only_a and only_b:
            return 0.0

    overlap = _weighted_overlap(tok_a, tok_b, idf)
    if overlap < 0.40 and not same_creator:
        return 0.0

    fuzzy = fuzz.token_set_ratio(text_a, text_b) / 100.0
    score = 0.35 * fuzzy + 0.65 * overlap

    if same_creator:
        score = min(score + 0.10, 1.0)
    return score


def cluster_candidates(candidates: list[Candidate],
                       threshold: float = 0.62) -> list[Cluster]:
    """Single-link agglomeration with a blocking key to keep it near-linear.

    Blocking: only compare candidates that share at least one rare-ish content
    token. Without it this is O(n^2) on every pair; with it, typical runs of a
    few thousand candidates finish in well under a second.
    """
    # ---- explicit cross-links first ------------------------------------
    # Reddit posts carry the YouTube id they link to; that is a certain match,
    # so wire it up before any fuzzy work.
    by_platform_id: dict[str, Candidate] = {}
    for cand in candidates:
        by_platform_id.setdefault(f"{cand.platform}:{cand.platform_id}", cand)

    parent: dict[int, int] = {i: i for i in range(len(candidates))}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    index_of = {id(c): i for i, c in enumerate(candidates)}
    for i, cand in enumerate(candidates):
        yt_id = cand.meta.get("youtube_id")
        if yt_id:
            target = by_platform_id.get(f"youtube:{yt_id}")
            if target is not None:
                union(i, index_of[id(target)])

    # ---- blocked fuzzy pass --------------------------------------------
    idf = build_idf(candidates)
    cutoff = distinctive_cutoff(idf)

    # Block on each candidate's RAREST tokens, not its first eight alphabetically.
    # Blocking on common words puts hundreds of unrelated clips in one bucket and
    # buys nothing; blocking on rare words puts genuine duplicates together.
    blocks: dict[str, list[int]] = defaultdict(list)
    for i, cand in enumerate(candidates):
        tokens = sorted(content_tokens(cand), key=lambda t: -idf.get(t, 1.0))
        for token in tokens[:8]:
            blocks[token].append(i)

    for members in blocks.values():
        if len(members) < 2 or len(members) > 300:
            continue                      # >300 means the token is boilerplate
        for pos, i in enumerate(members):
            for j in members[pos + 1:]:
                if find(i) == find(j):
                    continue
                if similarity(candidates[i], candidates[j], idf, cutoff) >= threshold:
                    union(i, j)

    grouped: dict[int, list[Candidate]] = defaultdict(list)
    for i, cand in enumerate(candidates):
        grouped[find(i)].append(cand)

    clusters: list[Cluster] = []
    for root, members in grouped.items():
        anchor = max(members, key=lambda c: (c.views or 0, len(c.text)))
        clusters.append(Cluster(cluster_id=anchor.fingerprint, members=members))
    return clusters
