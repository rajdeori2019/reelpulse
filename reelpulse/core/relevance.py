"""Keyword matching for ad-hoc search.

Search introduces a failure mode the weekly leaderboard does not have. The
weekly board asks "what went big" and any answer is on-topic by construction.
A keyword search asks "what went big *about X*", and now a clip can be very
viral and completely irrelevant — YouTube's `search.list` is generous, and a
query for "sourdough" will happily return popular cooking clips that never
mention it.

So relevance is a hard gate applied before scoring, not a scoring component.
Mixing them would let a 40M-view clip with a weak keyword match outrank a
perfectly on-topic one, which is exactly the behaviour that makes social
listening tools untrustworthy.

Matching is transparent and tiered — every result carries the tier it matched
on, so a thin result set is visibly thin rather than quietly padded.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from ..models import Candidate

_PUNCT = re.compile(r"[^\w\s#]", re.UNICODE)
_WS = re.compile(r"\s+")

# Tokens too common to carry topical meaning in a short-form context.
NOISE = {
    "the", "a", "an", "and", "or", "of", "in", "on", "for", "to", "with",
    "video", "reel", "reels", "short", "shorts", "viral", "trending", "best",
    "top", "new", "how", "why", "what", "this", "that",
}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _PUNCT.sub(" ", text.lower())
    return _WS.sub(" ", text).strip()


@dataclass
class Query:
    """A parsed search query.

    Supports:
      sourdough starter        all terms must appear (AND)
      "sourdough starter"      exact phrase
      sourdough OR levain      any term (OR)
      sourdough -discard       exclude
    """

    raw: str
    phrases: list[str]
    required: list[str]
    optional: list[str]
    excluded: list[str]

    @property
    def terms(self) -> list[str]:
        return self.phrases + self.required + self.optional

    @property
    def is_or(self) -> bool:
        return bool(self.optional) and not self.required


def parse_query(raw: str) -> Query:
    text = raw.strip()
    phrases = [normalize(p) for p in re.findall(r'"([^"]+)"', text)]
    remainder = re.sub(r'"[^"]+"', " ", text)

    excluded, required, optional = [], [], []
    or_mode = " OR " in f" {remainder} "

    for token in remainder.split():
        if token.upper() == "OR":
            continue
        if token.startswith("-") and len(token) > 1:
            excluded.append(normalize(token[1:]))
            continue
        word = normalize(token)
        if not word or word in NOISE:
            continue
        (optional if or_mode else required).append(word)

    return Query(raw=text, phrases=phrases, required=required,
                 optional=optional, excluded=[e for e in excluded if e])


# Match tiers, strongest first. The tier is reported alongside each result.
TIER_TITLE_PHRASE = "title_phrase"
TIER_TITLE_ALL = "title_all_terms"
TIER_TEXT_PHRASE = "caption_phrase"
TIER_TEXT_ALL = "caption_all_terms"
TIER_HASHTAG = "hashtag"
TIER_PARTIAL = "partial"
TIER_NONE = "none"

# Tier scores are chosen so the documented --min-relevance thresholds actually
# mean something:
#   0.4  keep partial matches and up
#   0.7  require a full term match (drops hashtag-only and partials)
#   0.9  demand the term in the title
# An earlier arrangement scored a 1-of-2 partial at 0.20, which meant partials
# could never clear the default 0.4 gate — the tier existed but was unreachable,
# and the CLI help said otherwise.
TIER_RANK = {
    TIER_TITLE_PHRASE: 1.00, TIER_TITLE_ALL: 0.92, TIER_TEXT_PHRASE: 0.80,
    TIER_TEXT_ALL: 0.72, TIER_HASHTAG: 0.62, TIER_PARTIAL: 0.40,
    TIER_NONE: 0.0,
}
PARTIAL_FLOOR, PARTIAL_CEIL = 0.40, 0.65


def _contains_word(haystack: str, needle: str) -> bool:
    """Word-boundary match.

    Substring matching classified 'nothing matches here' as sports elsewhere in
    this codebase because 'match' is a sports term. Same trap, same fix.
    """
    return bool(re.search(rf"\b{re.escape(needle)}\b", haystack))


def match(cand: Candidate, query: Query) -> tuple[str, float]:
    """Return (tier, relevance 0..1) for one candidate.

    Checked strongest-first across three text zones that carry genuinely
    different amounts of signal:
      title  — the creator chose to lead with it
      prose  — title plus caption, what a human actually reads
      tags   — hashtags and keywords, which are cheap to spray and mean less
    A term appearing only in hashtags is a real but weak match, and is labelled
    as such rather than being passed off as a caption mention.
    """
    title = normalize(cand.title)
    caption = normalize(cand.caption)
    tagtext = " ".join(
        [normalize(h) for h in (cand.meta.get("hashtags") or [])]
        + [normalize(t) for t in (cand.meta.get("tags") or [])])

    prose = f"{title} {caption}".strip()
    body = f"{prose} {tagtext}".strip()

    # Exclusions win over everything, anywhere in the text.
    for term in query.excluded:
        if _contains_word(body, term):
            return TIER_NONE, 0.0

    for phrase in query.phrases:
        if phrase and phrase in title:
            return TIER_TITLE_PHRASE, TIER_RANK[TIER_TITLE_PHRASE]

    words = query.required or query.optional
    if not words and not query.phrases:
        return TIER_NONE, 0.0

    if words:
        in_title = [w for w in words if _contains_word(title, w)]
        in_prose = [w for w in words if _contains_word(prose, w)]
        in_body = [w for w in words if _contains_word(body, w)]

        if query.is_or:
            # Any single hit qualifies; more hits score higher within the tier.
            bonus = lambda hits, cap: min(cap, cap - 0.20 + 0.06 * len(hits))  # noqa: E731
            if in_title:
                return TIER_TITLE_ALL, bonus(in_title, TIER_RANK[TIER_TITLE_ALL])
            if in_prose:
                return TIER_TEXT_ALL, bonus(in_prose, TIER_RANK[TIER_TEXT_ALL])
            if in_body:
                return TIER_HASHTAG, TIER_RANK[TIER_HASHTAG]
        else:
            total = len(words)
            if len(in_title) == total:
                return TIER_TITLE_ALL, TIER_RANK[TIER_TITLE_ALL]
            if len(in_prose) == total:
                return TIER_TEXT_ALL, TIER_RANK[TIER_TEXT_ALL]
            if len(in_body) == total:
                # Complete only once hashtags are counted — weaker on purpose.
                return TIER_HASHTAG, TIER_RANK[TIER_HASHTAG]
            if in_body:
                # Partial AND match: real but incomplete. Scored on a floor so
                # it clears the documented 0.4 gate, and capped below a full
                # match so it can never outrank one.
                fraction = len(in_body) / total
                span = PARTIAL_CEIL - PARTIAL_FLOOR
                return TIER_PARTIAL, round(PARTIAL_FLOOR + span * fraction, 3)

    for phrase in query.phrases:
        if phrase and phrase in prose:
            return TIER_TEXT_PHRASE, TIER_RANK[TIER_TEXT_PHRASE]
        if phrase and phrase in body:
            return TIER_HASHTAG, TIER_RANK[TIER_HASHTAG]

    return TIER_NONE, 0.0


def filter_candidates(candidates: list[Candidate], query: Query,
                      min_relevance: float = 0.4
                      ) -> tuple[list[Candidate], dict[str, int]]:
    """Gate the pool by relevance, and report what each tier contributed."""
    kept: list[Candidate] = []
    tally: dict[str, int] = {}

    for cand in candidates:
        tier, score = match(cand, query)
        tally[tier] = tally.get(tier, 0) + 1
        if score >= min_relevance:
            cand.meta["relevance"] = round(score, 3)
            cand.meta["match_tier"] = tier
            kept.append(cand)

    return kept, tally


def expand(query: Query, extra: int = 3) -> list[str]:
    """Search strings to send upstream.

    Each ALTERNATIVE gets its own search. This used to join every term into one
    string, which quietly destroyed OR queries: `"career advice" OR "career
    guidance"` became the single search `career advice career guidance` — a
    phrase nobody has ever written, matching nothing, so the run died with "no
    results" on a query that should have returned plenty.

    Only after alternatives are covered do format qualifiers get added, and
    still no synonyms: guessing synonyms silently changes what was asked for.
    """
    alternatives: list[str] = []

    # Quoted phrases are alternatives in their own right.
    alternatives.extend(query.phrases)

    # OR terms likewise: each is a separate way to satisfy the query.
    alternatives.extend(query.optional)

    # AND terms describe ONE search, so they join into a single string.
    if query.required:
        alternatives.append(" ".join(query.required))

    if not alternatives:
        alternatives = [query.raw]

    # De-duplicate, preserving order.
    seen: set[str] = set()
    ordered = [a for a in alternatives if a and not (a in seen or seen.add(a))]

    # Spend any remaining budget widening the FIRST alternative, since a single
    # alternative is the common case and benefits most from the extra angles.
    out = list(ordered)
    budget = 1 + extra
    qualifiers = ["shorts", "reel"]
    i = 0
    while len(out) < budget and i < len(qualifiers):
        out.append(f"{ordered[0]} {qualifiers[i]}")
        i += 1
    return out[:max(budget, len(ordered))]
