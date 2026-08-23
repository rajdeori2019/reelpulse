"""Rate limiting, quota accounting and circuit breaking.

The naive version of this — retry on HTTP 429 — is worse than useless here,
because **neither platform this project depends on signals a rate limit with
429**:

  * **Meta** returns HTTP **400** with an error code in the body: 4 (app limit),
    17 (user limit), 32 (page limit), 613 (rate limit), or 80000-80014 (business
    use case limits). A 429-only handler treats these as permanent failures,
    retries them as if they were bugs, and keeps hammering a throttled endpoint —
    which is exactly how an app gets its access restricted.
  * **YouTube** returns HTTP **403** with `reason: quotaExceeded` or
    `rateLimitExceeded`. Also not 429, also easy to mistake for an auth failure.

So detection is body-aware, not status-code-aware.

Four mechanisms, in the order they engage:

1. **Quota ledger (persistent).** Spend is written to SQLite, so a limit that
   spans runs — YouTube's 10,000 units/day, Meta's rolling hour — is actually
   enforced. An in-memory counter resets every process and enforces nothing.
   Requests are refused *before* being sent when the budget cannot cover them.

2. **Token bucket (in-process).** Paces requests inside a run so a burst never
   arrives faster than the documented per-minute ceiling.

3. **Usage-header feedback.** Meta reports consumption as a percentage on every
   response (`X-App-Usage`, `X-Business-Use-Case-Usage`). Above a threshold the
   limiter slows down *before* being throttled, rather than discovering the wall
   by hitting it.

4. **Circuit breaker (persistent).** On a real throttle, the service is put in
   cooldown — honouring Meta's own `estimated_time_to_regain_access` when it is
   supplied — and the cooldown survives process exit, so the next cron run does
   not immediately re-trigger it.

Reserve headroom is the other half of not getting blocked: each service keeps a
fraction of its quota unspendable by default, so an ad-hoc `search` cannot eat
the budget the scheduled daily snapshot depends on.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

log = logging.getLogger("reelpulse")


class QuotaExhausted(RuntimeError):
    """Raised before sending, when the budget cannot cover a request."""


class ServiceCoolingDown(RuntimeError):
    """Raised before sending, when a service is in an enforced cooldown."""


# ---------------------------------------------------------------------------
# documented limits
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Limit:
    """One service's documented ceilings.

    `reserve` is the fraction of quota held back from opportunistic work. It is
    the difference between "the scheduled job still runs on Monday" and "someone
    ran three keyword searches on Sunday night and the cron job found an empty
    tank".
    """

    name: str
    per_minute: float               # in-process pacing
    quota: int | None = None        # ceiling over quota_window_s
    quota_window_s: int = 86_400
    unit: str = "calls"
    reserve: float = 0.15
    usage_header_ceiling: float = 80.0   # % at which to start slowing down
    default_cooldown_s: int = 900

    # Some quotas count DISTINCT THINGS rather than calls. Instagram's hashtag
    # window allows 30 unique hashtags per 7 days — querying the same tag twenty
    # times costs one slot, and twenty different tags costs twenty. Counting
    # calls would over-report by the repeat factor and refuse work that is
    # actually free.
    distinct: bool = False

    # Some quotas reset on a wall-clock boundary rather than rolling.
    # YouTube's 10,000 units reset at midnight Pacific: spend 8,000 at 23:00 PT
    # and a rolling 24h window would still count it at 00:30 PT, refusing runs
    # against a budget the platform has already refilled. Modelling the real
    # boundary is the difference between the tool working and mysteriously
    # declining to.
    reset_tz: str | None = None


LIMITS: dict[str, Limit] = {
    # Instagram Hashtag Search: 30 UNIQUE hashtags per rolling 7 days, enforced
    # by Meta across every call the token makes. This is the tightest limit in
    # the project and the only one that cannot be waited out in hours.
    "instagram_hashtags": Limit("instagram_hashtags", per_minute=30, quota=30,
                                quota_window_s=7 * 86_400, unit="hashtags",
                                reserve=0.0, distinct=True),

    # 10,000 units/day per project. search.list costs 100, videos.list costs 1.
    # Quota is counted in UNITS, not calls, so cost varies per endpoint.
    "youtube": Limit("youtube", per_minute=60, quota=10_000,
                     quota_window_s=86_400, unit="units", reserve=0.20,
                     reset_tz="America/Los_Angeles"),

    # Platform limit is 200 calls/hour x daily active users. A self-hosted
    # install has ~1 user, so 200/hour is the realistic ceiling. Meta's own
    # usage headers are the authoritative signal and override this.
    "instagram_graph": Limit("instagram_graph", per_minute=30, quota=200,
                             quota_window_s=3_600, unit="calls", reserve=0.25),

    # Tokenless oEmbed. Meta states limits "may differ" from token-based access
    # and publishes no number, so this is deliberately conservative — an
    # unpublished limit is a reason for more caution, not less.
    "instagram_oembed": Limit("instagram_oembed", per_minute=30, quota=500,
                              quota_window_s=3_600, unit="calls", reserve=0.10),

    # ~100 queries/minute with OAuth. Held at 60 to leave room for the retries
    # that a 100/min ceiling makes likely.
    "reddit": Limit("reddit", per_minute=60, quota=None, unit="calls",
                    reserve=0.0),

    # No hard published cap, but it is a donated public service. Politeness is
    # the operative constraint, not enforcement.
    "wikimedia": Limit("wikimedia", per_minute=100, quota=None, unit="calls",
                       reserve=0.0),
}


def limit_for(service: str) -> Limit:
    return LIMITS.get(service, Limit(service, per_minute=30))


def window_start(limit: Limit, now: datetime) -> datetime:
    """When the current quota window began.

    Rolling by default. For a wall-clock quota (`reset_tz`), the most recent
    local midnight — so the ledger refills when the platform's does.
    """
    if not limit.reset_tz:
        return now - timedelta(seconds=limit.quota_window_s)

    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415
        local = now.astimezone(ZoneInfo(limit.reset_tz))
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        # No tzdata (common in slim containers). Fall back to 08:00 UTC, which
        # is midnight PST. During PDT the real reset is an hour earlier, so this
        # errs toward counting *more* spend — conservative, never over-spending.
        midnight = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if midnight > now:
            midnight -= timedelta(days=1)
        return midnight


# ---------------------------------------------------------------------------
# rate-limit detection
# ---------------------------------------------------------------------------

META_RATE_LIMIT_CODES = {4, 17, 32, 613}
META_BUC_RANGE = range(80_000, 80_015)
YOUTUBE_RATE_REASONS = {"quotaExceeded", "rateLimitExceeded",
                        "userRateLimitExceeded", "dailyLimitExceeded"}

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def classify(status: int, body: dict | None) -> tuple[bool, bool, str]:
    """(is_rate_limited, is_retryable, reason).

    Status code alone is not enough — see the module docstring. A Meta throttle
    arrives as 400 and a YouTube quota exhaustion as 403, both of which look
    like permanent client errors from the outside.
    """
    body = body or {}
    error = body.get("error") or {}

    # --- Meta ---------------------------------------------------------
    code = error.get("code")
    if isinstance(code, int) and (code in META_RATE_LIMIT_CODES
                                  or code in META_BUC_RANGE):
        return True, True, f"meta error code {code}: {error.get('message', '')[:120]}"

    # --- YouTube ------------------------------------------------------
    for item in (error.get("errors") or []):
        if item.get("reason") in YOUTUBE_RATE_REASONS:
            return True, True, f"youtube {item['reason']}"

    if status == 429:
        return True, True, "http 429"
    if status in RETRYABLE_STATUS:
        return False, True, f"http {status}"

    # Everything else — 400 without a rate-limit code, 401, 403, 404 — is a
    # real error. Retrying it wastes quota and looks like abuse.
    return False, False, f"http {status}"


def parse_meta_usage(headers) -> float:
    """Highest usage percentage Meta reports, or 0.0 when absent."""
    worst = 0.0
    for header in ("x-app-usage", "x-business-use-case-usage"):
        raw = headers.get(header) or headers.get(header.title())
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue

        entries = []
        if isinstance(data, dict):
            # BUC nests per business id; app usage is flat.
            entries = ([data] if "call_count" in data
                       else [e for v in data.values()
                             if isinstance(v, list) for e in v])
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for field in ("call_count", "total_cputime", "total_time"):
                try:
                    worst = max(worst, float(entry.get(field, 0)))
                except (TypeError, ValueError):
                    continue
    return worst


def parse_cooldown(headers, body: dict | None, default_s: int) -> int:
    """Seconds to wait, preferring what the platform actually told us."""
    retry_after = headers.get("retry-after") or headers.get("Retry-After")
    if retry_after:
        try:
            return max(int(float(retry_after)), 1)
        except (TypeError, ValueError):
            pass

    # Meta reports this in MINUTES, inside the BUC header.
    for header in ("x-business-use-case-usage", "X-Business-Use-Case-Usage"):
        raw = headers.get(header)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for value in (data.values() if isinstance(data, dict) else []):
            for entry in (value if isinstance(value, list) else []):
                minutes = entry.get("estimated_time_to_regain_access")
                if minutes:
                    return int(float(minutes) * 60)

    # Reddit publishes seconds until its window resets.
    reset = headers.get("x-ratelimit-reset") or headers.get("X-Ratelimit-Reset")
    if reset:
        try:
            return max(int(float(reset)), 1)
        except (TypeError, ValueError):
            pass

    return default_s


# ---------------------------------------------------------------------------
# mechanisms
# ---------------------------------------------------------------------------

class TokenBucket:
    """Paces requests within a process. Thread-safe."""

    def __init__(self, per_minute: float, burst: int | None = None) -> None:
        self.rate = max(per_minute, 1.0) / 60.0
        self.capacity = float(burst if burst is not None else max(per_minute / 4, 1))
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def take(self, tokens: float = 1.0, *, sleeper=time.sleep) -> float:
        with self._lock:
            now = time.monotonic()
            self.tokens = min(self.capacity,
                              self.tokens + (now - self.updated) * self.rate)
            self.updated = now

            if self.tokens >= tokens:
                self.tokens -= tokens
                return 0.0

            deficit = tokens - self.tokens
            wait = deficit / self.rate
            self.tokens = 0.0
            self.updated = now + wait

        sleeper(wait)
        return wait


class RateLimiter:
    """Ties the ledger, buckets, usage feedback and breaker together.

    Constructed with a `Store` so quota spend and cooldowns persist across runs.
    Passing `store=None` gives an in-memory limiter, which is fine for tests but
    enforces nothing that spans a process.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS api_spend (
        service   TEXT NOT NULL,
        ts        TEXT NOT NULL,
        cost      REAL NOT NULL,
        endpoint  TEXT,
        key       TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_spend ON api_spend(service, ts);

    CREATE TABLE IF NOT EXISTS api_cooldown (
        service   TEXT PRIMARY KEY,
        until     TEXT NOT NULL,
        reason    TEXT,
        failures  INTEGER DEFAULT 0
    );
    """

    def __init__(self, store=None, *, sleeper=time.sleep,
                 clock=None) -> None:
        self.store = store
        self.sleeper = sleeper
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.buckets: dict[str, TokenBucket] = {}
        self._memory_spend: list[tuple[str, datetime, float, str | None]] = []
        self._memory_cooldown: dict[str, tuple[datetime, str]] = {}
        self._consecutive: dict[str, int] = {}
        self._slowdown: dict[str, float] = {}

        if store is not None:
            store.conn.executescript(self.SCHEMA)
            self._migrate(store)
            store.conn.commit()

    @staticmethod
    def _migrate(store) -> None:
        """Add columns a database created by an older version is missing.

        `CREATE TABLE IF NOT EXISTS` does nothing to a table that already
        exists, so a ledger restored from a cache written before distinct
        counting has no `key` column and every insert fails. That is a hard
        crash on the first real call of the run, in the one component whose
        whole job is to keep runs from failing.
        """
        have = {row[1] for row in
                store.conn.execute("PRAGMA table_info(api_spend)")}
        if "key" not in have:
            store.conn.execute("ALTER TABLE api_spend ADD COLUMN key TEXT")
            log.info("[limits] migrated api_spend: added the key column")

    # ---- ledger ------------------------------------------------------

    def _bucket(self, service: str) -> TokenBucket:
        if service not in self.buckets:
            self.buckets[service] = TokenBucket(limit_for(service).per_minute)
        return self.buckets[service]

    def spent(self, service: str, window_s: int | None = None) -> float:
        limit = limit_for(service)
        if window_s is not None:
            cutoff = self.clock() - timedelta(seconds=window_s)
        else:
            cutoff = window_start(limit, self.clock())

        if self.store is not None:
            if limit.distinct:
                cur = self.store.conn.execute(
                    "SELECT COUNT(DISTINCT key) FROM api_spend "
                    "WHERE service = ? AND ts >= ? AND key IS NOT NULL",
                    (service, cutoff.isoformat()))
            else:
                cur = self.store.conn.execute(
                    "SELECT COALESCE(SUM(cost), 0) FROM api_spend "
                    "WHERE service = ? AND ts >= ?", (service, cutoff.isoformat()))
            return float(cur.fetchone()[0] or 0.0)

        rows = [(svc, ts, cost, key) for svc, ts, cost, key in self._memory_spend
                if svc == service and ts >= cutoff]
        if limit.distinct:
            return float(len({k for _, _, _, k in rows if k is not None}))
        return sum(cost for _, _, cost, _ in rows)

    def spent_keys(self, service: str) -> set[str]:
        """Which distinct things are booked inside the window right now.

        The count alone is not enough for a distinct-counted limit: planning a
        sweep needs to know *which* hashtags are already inside the window,
        because those are free and everything else costs a slot.
        """
        cutoff = window_start(limit_for(service), self.clock())
        if self.store is not None:
            cur = self.store.conn.execute(
                "SELECT DISTINCT key FROM api_spend "
                "WHERE service = ? AND ts >= ? AND key IS NOT NULL",
                (service, cutoff.isoformat()))
            return {row[0] for row in cur.fetchall()}
        return {k for svc, ts, _, k in self._memory_spend
                if svc == service and ts >= cutoff and k is not None}

    def already_spent(self, service: str, key: str) -> bool:
        """Is this exact key already inside the window, and therefore free?"""
        limit = limit_for(service)
        cutoff = window_start(limit, self.clock())
        if self.store is not None:
            cur = self.store.conn.execute(
                "SELECT 1 FROM api_spend WHERE service = ? AND ts >= ? AND key = ? "
                "LIMIT 1", (service, cutoff.isoformat(), key))
            return cur.fetchone() is not None
        return any(svc == service and ts >= cutoff and k == key
                   for svc, ts, _, k in self._memory_spend)

    def remaining(self, service: str, *, respect_reserve: bool = True) -> float:
        limit = limit_for(service)
        if limit.quota is None:
            return float("inf")
        ceiling = limit.quota * (1 - limit.reserve if respect_reserve else 1.0)
        return max(ceiling - self.spent(service), 0.0)

    def record(self, service: str, cost: float, endpoint: str = "",
               key: str | None = None) -> None:
        """`key` identifies what was spent, for limits that count distinct
        things (a hashtag) rather than calls."""
        now = self.clock()
        if self.store is not None:
            self.store.conn.execute(
                "INSERT INTO api_spend (service, ts, cost, endpoint, key) "
                "VALUES (?,?,?,?,?)",
                (service, now.isoformat(), float(cost), endpoint, key))
            self.store.conn.commit()
        else:
            self._memory_spend.append((service, now, float(cost), key))

    def prune(self, older_than_days: int = 30) -> None:
        if self.store is None:
            return
        cutoff = (self.clock() - timedelta(days=older_than_days)).isoformat()
        self.store.conn.execute("DELETE FROM api_spend WHERE ts < ?", (cutoff,))
        self.store.conn.commit()

    # ---- circuit breaker ---------------------------------------------

    def cooldown_until(self, service: str) -> datetime | None:
        if self.store is not None:
            cur = self.store.conn.execute(
                "SELECT until FROM api_cooldown WHERE service = ?", (service,))
            row = cur.fetchone()
            if not row:
                return None
            until = datetime.fromisoformat(row[0])
        else:
            entry = self._memory_cooldown.get(service)
            if not entry:
                return None
            until = entry[0]

        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until if until > self.clock() else None

    def open_circuit(self, service: str, seconds: int, reason: str) -> None:
        until = self.clock() + timedelta(seconds=max(seconds, 1))
        log.warning("[%s] cooling down for %ds (%s). This is persisted, so the "
                    "next run will respect it too.", service, seconds, reason)
        if self.store is not None:
            self.store.conn.execute(
                "INSERT OR REPLACE INTO api_cooldown (service, until, reason, failures) "
                "VALUES (?,?,?,?)",
                (service, until.isoformat(), reason[:300],
                 self._consecutive.get(service, 0)))
            self.store.conn.commit()
        else:
            self._memory_cooldown[service] = (until, reason)

    def close_circuit(self, service: str) -> None:
        self._consecutive[service] = 0
        if self.store is not None:
            self.store.conn.execute("DELETE FROM api_cooldown WHERE service = ?",
                                    (service,))
            self.store.conn.commit()
        else:
            self._memory_cooldown.pop(service, None)

    # ---- the pre-flight gate -----------------------------------------

    def acquire(self, service: str, cost: float = 1.0, *,
                respect_reserve: bool = True, key: str | None = None) -> None:
        """Block until it is safe to send, or refuse outright.

        Refusing before sending is the entire point. A request that would breach
        a quota is far better never made: it cannot succeed, and making it is
        what escalates a throttle into a restriction.
        """
        until = self.cooldown_until(service)
        if until:
            wait = (until - self.clock()).total_seconds()
            raise ServiceCoolingDown(
                f"{service} is in cooldown for another {wait / 60:.1f} min")

        limit = limit_for(service)

        if limit.distinct and key is None:
            # Without a key there is nothing to count, so the spend would be
            # invisible: the ledger would read zero however many calls were
            # made, and the first sign of trouble would be Meta refusing the
            # 31st hashtag. Better to fail here, loudly, at the call site.
            raise ValueError(
                f"{service} counts distinct {limit.unit}, so every call must "
                f"pass key= naming the one being spent")

        # A distinct-counted key already inside the window costs nothing, so it
        # must not be refused when the quota looks full.
        if limit.distinct and self.already_spent(service, key):
            self._bucket(service).take(1.0, sleeper=self.sleeper)
            return

        if limit.quota is not None:
            left = self.remaining(service, respect_reserve=respect_reserve)
            if left < cost:
                total = self.spent(service)
                raise QuotaExhausted(
                    f"{service}: {cost:g} {limit.unit} needed, {left:g} available "
                    f"({total:g}/{limit.quota} spent in the last "
                    f"{limit.quota_window_s // 3600}h"
                    + (f", {limit.reserve:.0%} reserved for scheduled runs)"
                       if respect_reserve and limit.reserve else ")"))

        self._bucket(service).take(1.0, sleeper=self.sleeper)

        # Voluntary slowdown when the platform says we are getting close.
        extra = self._slowdown.get(service, 0.0)
        if extra:
            self.sleeper(extra)

    # ---- post-flight -------------------------------------------------

    def observe(self, service: str, status: int, headers, body: dict | None,
                cost: float, endpoint: str = "",
                key: str | None = None) -> tuple[bool, bool, str]:
        """Record the outcome and adapt. Returns classify()'s verdict.

        `key` is threaded through to the ledger so a distinct-counted limit
        books the thing that was spent, not just the call. Callers must not
        record separately as well: two rows for one request would be harmless
        for a distinct count but would double-charge a unit-counted one.
        """
        self.record(service, cost, endpoint, key=key)

        usage = parse_meta_usage(headers or {})
        limit = limit_for(service)
        if usage:
            if usage >= limit.usage_header_ceiling:
                # Meta is telling us we are close. Slowing down now is cheaper
                # than being throttled and cooled down later.
                self._slowdown[service] = min(
                    (usage - limit.usage_header_ceiling) / 5.0 + 1.0, 15.0)
                log.warning("[%s] platform reports %.0f%% of its hourly limit "
                            "used — throttling ourselves by %.1fs/request",
                            service, usage, self._slowdown[service])
            else:
                self._slowdown.pop(service, None)

        rate_limited, retryable, reason = classify(status, body)

        if rate_limited:
            self._consecutive[service] = self._consecutive.get(service, 0) + 1
            self.open_circuit(service,
                              parse_cooldown(headers or {}, body,
                                             limit.default_cooldown_s),
                              reason)
        elif 200 <= status < 300:
            self.close_circuit(service)
        elif not retryable:
            # Repeated hard failures usually mean a bad token. Backing off stops
            # a broken config from generating thousands of rejected calls.
            self._consecutive[service] = self._consecutive.get(service, 0) + 1
            if self._consecutive[service] >= 5:
                self.open_circuit(service, 600,
                                  f"5 consecutive non-retryable failures ({reason})")

        return rate_limited, retryable, reason

    # ---- backoff -----------------------------------------------------

    @staticmethod
    def backoff(attempt: int, base: float = 1.5, cap: float = 60.0,
                rng: random.Random | None = None) -> float:
        """Exponential backoff with full jitter.

        Full jitter rather than fixed increments: several collectors retrying in
        lockstep produce a synchronised burst that looks exactly like an attack.
        Randomising the whole interval spreads them out.
        """
        rng = rng or random
        return rng.uniform(0, min(cap, base * (2 ** attempt)))

    # ---- reporting ---------------------------------------------------

    def status(self) -> list[dict]:
        out = []
        for name, limit in LIMITS.items():
            spent = self.spent(name)
            until = self.cooldown_until(name)
            out.append({
                "service": name,
                "unit": limit.unit,
                "window_hours": limit.quota_window_s / 3600,
                "quota": limit.quota,
                "spent": round(spent, 1),
                "remaining": (None if limit.quota is None
                              else round(self.remaining(name), 1)),
                "reserved": (None if limit.quota is None
                             else round(limit.quota * limit.reserve, 1)),
                "pct_used": (None if not limit.quota
                             else round(spent / limit.quota * 100, 1)),
                "per_minute": limit.per_minute,
                "cooling_down_until": until.isoformat() if until else None,
                "self_throttle_s": round(self._slowdown.get(name, 0.0), 2),
            })
        return out
