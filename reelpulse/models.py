"""Core data structures shared by collectors, scorer and analyzer.

Everything in ReelPulse flows through `Candidate`. A Candidate is one short-form
video *observation* on one platform. Several Candidates that are the same
underlying clip get merged into a `Cluster` by core.dedupe.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _norm_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


@dataclass
class Candidate:
    """One short-form video as seen on one platform at one point in time."""

    platform: str                 # youtube | instagram | reddit | tiktok
    platform_id: str              # native id on that platform
    url: str
    title: str = ""
    caption: str = ""
    creator: str = ""
    creator_id: str = ""
    published_at: datetime | None = None
    duration_s: float | None = None

    # Raw metrics. `views` is None when the platform does not expose it.
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    saves: int | None = None

    # Free-form extras (audio title, region, hashtags, thumbnail, embed html...)
    meta: dict[str, Any] = field(default_factory=dict)

    collected_at: datetime = field(default_factory=utcnow)
    source: str = ""              # which collector produced this

    def __post_init__(self) -> None:
        self.published_at = _norm_dt(self.published_at)
        self.collected_at = _norm_dt(self.collected_at) or utcnow()

    # ---- derived -------------------------------------------------------

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.platform_id}"

    @property
    def fingerprint(self) -> str:
        """Stable hash used as the primary key in SQLite."""
        return hashlib.sha1(self.key.encode("utf-8")).hexdigest()[:16]

    @property
    def age_hours(self) -> float:
        if not self.published_at:
            return float("nan")
        delta = (self.collected_at - self.published_at).total_seconds() / 3600.0
        return max(delta, 0.5)  # floor at 30 min so velocity never explodes

    @property
    def views_per_hour(self) -> float:
        if not self.views or math.isnan(self.age_hours):
            return 0.0
        return self.views / self.age_hours

    @property
    def text(self) -> str:
        """Everything textual, for dedupe and feature extraction."""
        tags = " ".join(self.meta.get("hashtags", []) or [])
        return " ".join(x for x in (self.title, self.caption, tags) if x).strip()

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["fingerprint"] = self.fingerprint
        for key in ("published_at", "collected_at"):
            value = row.get(key)
            row[key] = value.isoformat() if isinstance(value, datetime) else None
        return row


@dataclass
class Cluster:
    """One underlying clip, seen across one or more platforms."""

    cluster_id: str
    members: list[Candidate]

    # Filled in by core.score
    features: dict[str, float] = field(default_factory=dict)
    components: dict[str, float] = field(default_factory=dict)
    vvs: float = 0.0
    rank: int | None = None

    # Filled in by core.analyze
    tags: dict[str, Any] = field(default_factory=dict)
    why: list[str] = field(default_factory=list)

    @property
    def primary(self) -> Candidate:
        """The member with the most trustworthy metrics (highest views wins)."""
        return max(self.members, key=lambda c: (c.views or 0, c.likes or 0))

    @property
    def instagram(self) -> Candidate | None:
        for member in self.members:
            if member.platform == "instagram":
                return member
        return None

    @property
    def platforms(self) -> set[str]:
        return {member.platform for member in self.members}

    @property
    def breadth(self) -> int:
        return len(self.platforms)

    @property
    def title(self) -> str:
        return self.primary.title or self.primary.caption[:120] or "(untitled)"

    @property
    def best_views(self) -> int:
        return max((member.views or 0) for member in self.members)
