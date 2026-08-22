"""Synthetic data so the whole pipeline can be exercised with zero API keys.

The generator is not uniform noise: it bakes in a few real relationships (short
runtimes and POV/wait-for-it hooks get a view multiplier; heavy hashtag use gets
a small penalty) precisely so the pattern miner has something true to find. If
`reelpulse demo` surfaces those planted relationships, the mining stage works.
That makes this file a fixture and a smoke test at once.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from reelpulse.models import Candidate

TOPICS = {
    "food": ["recipe", "cooking", "chef", "kitchen"],
    "comedy": ["funny", "prank", "skit", "fail"],
    "fitness": ["workout", "gym", "abs", "training"],
    "animals": ["dog", "cat", "puppy", "wildlife"],
    "satisfying": ["satisfying", "asmr", "restoration", "pressure wash"],
    "music_dance": ["dance", "song", "choreography", "remix"],
    "tech": ["ai", "gadget", "iphone", "coding"],
    "education": ["explained", "history", "psychology", "science"],
}

HOOK_PHRASES = {
    "pov": "POV: you {verb} for the first time",
    "wait_for_it": "Wait for it... this {noun} changes everything",
    "question": "Why does every {noun} do this?",
    "listicle": "Top 5 {noun} mistakes nobody talks about",
    "shock_claim": "Nobody told you this about {noun}",
    "transformation": "Before and after: {noun} transformation",
    "tutorial": "How to fix your {noun} in 10 seconds",
    "contrarian": "Unpopular opinion: your {noun} is wrong",
    "stakes": "I tried {noun} for 30 days",
    "confession": "Honestly, I failed at {noun} three times",
    "none_detected": "{noun} clip",
}

VERBS = ["see it", "try it", "cook it", "lift it", "meet him", "hear this"]

# Planted ground truth. If mining works, these come back out as high-lift rules.
HOOK_MULTIPLIER = {
    "pov": 2.4, "wait_for_it": 2.1, "shock_claim": 1.8, "listicle": 1.5,
    "question": 1.3, "transformation": 1.4, "tutorial": 0.9,
    "contrarian": 1.1, "stakes": 1.2, "confession": 0.8, "none_detected": 0.6,
}


def synthesize(n: int = 140, seed: int = 20260822) -> list[Candidate]:
    rng = random.Random(seed)
    now = datetime.now(timezone.utc)
    out: list[Candidate] = []

    for i in range(n):
        topic = rng.choice(list(TOPICS))
        noun = rng.choice(TOPICS[topic])
        hook = rng.choices(
            list(HOOK_PHRASES),
            weights=[3, 3, 3, 2, 2, 2, 3, 2, 2, 1, 4],
        )[0]
        title = HOOK_PHRASES[hook].format(noun=noun, verb=rng.choice(VERBS))

        duration = rng.choice([5, 6, 9, 12, 14, 18, 25, 34, 48, 62])
        hashtags = rng.choice([0, 1, 2, 3, 5, 8, 12, 15])
        caption_words = rng.choice([0, 4, 7, 12, 22, 35])

        age_hours = rng.uniform(3, 24 * 7)

        # base x hook x duration x hashtag effects x noise
        base = rng.lognormvariate(12.2, 1.15)
        duration_mult = 1.9 if duration <= 10 else 1.35 if duration <= 20 else 0.75
        hashtag_mult = 0.7 if hashtags >= 11 else 1.15 if 1 <= hashtags <= 3 else 1.0
        views = int(base * HOOK_MULTIPLIER[hook] * duration_mult * hashtag_mult
                    * rng.uniform(0.55, 1.8))

        likes = int(views * rng.uniform(0.03, 0.11))
        comments = int(likes * rng.uniform(0.02, 0.14))

        caption = " ".join([f"word{j}" for j in range(caption_words)])
        caption += " " + " ".join(f"#{noun}{j}" for j in range(hashtags))
        if rng.random() < 0.3:
            caption += " what do you think?"
        if rng.random() < 0.25:
            caption += " follow for more"

        published = now - timedelta(hours=age_hours)
        creator = f"creator_{rng.randint(1, 45)}"
        region = rng.choice(["US", "IN", "BR", "GB", "ID", "MX", "DE", "PH"])

        out.append(Candidate(
            platform="youtube", platform_id=f"demo_yt_{i}",
            url=f"https://www.youtube.com/shorts/demo_yt_{i}",
            title=title, caption=caption, creator=creator,
            creator_id=f"UC_demo_{creator}", published_at=published,
            duration_s=float(duration), views=views, likes=likes, comments=comments,
            meta={"region": region, "hashtags": [f"{noun}{j}" for j in range(hashtags)],
                  "thumbnail": "", "demo": True},
            source="demo",
        ))

        # ~35% of clips get an Instagram mirror, and ~18% a Reddit repost, so
        # cross-platform breadth has something realistic to measure.
        if rng.random() < 0.35:
            out.append(Candidate(
                platform="instagram", platform_id=f"DEMO{i:05d}",
                url=f"https://www.instagram.com/reel/DEMO{i:05d}/",
                title=title, caption=caption, creator=creator,
                published_at=published + timedelta(hours=rng.uniform(-6, 6)),
                duration_s=float(duration),
                shares=rng.randint(0, 400),
                meta={"oembed_status": "ok", "embed_html": "", "demo": True},
                source="demo",
            ))
        if rng.random() < 0.18:
            out.append(Candidate(
                platform="reddit", platform_id=f"demo_rd_{i}",
                url=f"https://reddit.com/r/demo/comments/demo_rd_{i}",
                title=title, published_at=published + timedelta(hours=rng.uniform(1, 40)),
                duration_s=float(duration),
                shares=rng.randint(50, 9000), comments=rng.randint(5, 900),
                meta={"youtube_id": f"demo_yt_{i}", "subreddit": "demo", "demo": True},
                source="demo",
            ))

    return out


if __name__ == "__main__":
    items = synthesize()
    print(f"generated {len(items)} synthetic candidates")
