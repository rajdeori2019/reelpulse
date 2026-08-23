"""Rate limiting, quota accounting and circuit breaking.

The load-bearing tests are the ones covering **non-429 throttle signals**.
Neither platform this project depends on returns 429: Meta throttles with HTTP
400 plus an error code, YouTube with HTTP 403 plus a reason string. A 429-only
handler treats both as permanent bugs and retries them — which is exactly the
behaviour that turns a temporary throttle into a restricted app.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reelpulse.db import Store
from reelpulse.limits import (LIMITS, QuotaExhausted, RateLimiter,
                              ServiceCoolingDown, TokenBucket, classify,
                              parse_cooldown, parse_meta_usage)


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store(Path(tmp) / "t.db")
        yield s
        s.close()


@pytest.fixture
def limiter(store):
    slept: list[float] = []
    lim = RateLimiter(store, sleeper=slept.append)
    lim.slept = slept
    return lim


# ---- detecting throttles that are not 429 ---------------------------------

@pytest.mark.parametrize("code", [4, 17, 32, 613, 80000, 80014])
def test_meta_throttle_arrives_as_http_400(code):
    """Meta signals rate limits with 400, not 429. Missing this means retrying
    a throttled endpoint until access is restricted."""
    rate_limited, retryable, _ = classify(400, {"error": {"code": code,
                                                          "message": "limit"}})
    assert rate_limited is True
    assert retryable is True


@pytest.mark.parametrize("reason", ["quotaExceeded", "rateLimitExceeded",
                                    "dailyLimitExceeded"])
def test_youtube_quota_arrives_as_http_403(reason):
    """YouTube signals quota exhaustion with 403, which looks like an auth
    failure if you only inspect the status code."""
    rate_limited, retryable, _ = classify(
        403, {"error": {"errors": [{"reason": reason}]}})
    assert rate_limited is True
    assert retryable is True


def test_ordinary_400_is_not_treated_as_a_throttle():
    """A malformed request must not open a cooldown — that would mask a bug as
    a rate limit and silently disable the collector."""
    rate_limited, retryable, _ = classify(400, {"error": {"code": 100,
                                                          "message": "bad field"}})
    assert rate_limited is False
    assert retryable is False


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_client_errors_are_not_retried(status):
    """Retrying a 401 wastes quota and looks like credential stuffing."""
    _, retryable, _ = classify(status, {})
    assert retryable is False


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_failures_are_retried(status):
    _, retryable, _ = classify(status, {})
    assert retryable is True


# ---- reading what the platform tells us -----------------------------------

def test_meta_app_usage_header_is_parsed():
    headers = {"x-app-usage": '{"call_count":73,"total_cputime":25,"total_time":40}'}
    assert parse_meta_usage(headers) == 73.0


def test_meta_business_use_case_header_is_parsed():
    headers = {"x-business-use-case-usage":
               '{"17841400000":[{"type":"instagram","call_count":91,'
               '"total_cputime":12,"total_time":8}]}'}
    assert parse_meta_usage(headers) == 91.0


def test_malformed_usage_header_does_not_explode():
    assert parse_meta_usage({"x-app-usage": "not json"}) == 0.0
    assert parse_meta_usage({}) == 0.0


def test_retry_after_header_wins():
    assert parse_cooldown({"retry-after": "120"}, None, 900) == 120


def test_meta_regain_access_is_minutes_not_seconds():
    """estimated_time_to_regain_access is in MINUTES. Treating it as seconds
    would resume 60x too early and re-trigger the throttle immediately."""
    headers = {"x-business-use-case-usage":
               '{"1784":[{"call_count":100,"estimated_time_to_regain_access":7}]}'}
    assert parse_cooldown(headers, None, 900) == 420      # 7 min


def test_cooldown_falls_back_to_default():
    assert parse_cooldown({}, None, 900) == 900


# ---- quota ledger ---------------------------------------------------------

def test_youtube_cost_is_counted_in_units_not_calls(limiter):
    """search.list costs 100 units. Counting it as 1 call under-reports spend
    by 100x on the exact endpoint that drains the budget."""
    for _ in range(10):
        limiter.record("youtube", 100)
    assert limiter.spent("youtube") == 1000


def test_quota_refuses_before_sending(limiter):
    """The request that would breach the quota is never made. It cannot succeed,
    and making it is what escalates a throttle into a block."""
    limiter.record("youtube", 7_900)          # ceiling is 10000 * (1 - 0.20)
    with pytest.raises(QuotaExhausted) as exc:
        limiter.acquire("youtube", 200)
    assert "youtube" in str(exc.value)


def test_reserve_protects_scheduled_runs(limiter):
    """Ad-hoc work cannot spend the reserve, but the reserve is still there."""
    limiter.record("youtube", 8_000)
    assert limiter.remaining("youtube", respect_reserve=True) == 0
    assert limiter.remaining("youtube", respect_reserve=False) == 2_000


def test_spend_outside_the_window_is_forgotten(limiter, store):
    """YouTube's quota is daily. Yesterday's spend must not count today."""
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    store.conn.execute("INSERT INTO api_spend (service, ts, cost) VALUES (?,?,?)",
                       ("youtube", old, 9_000))
    store.conn.commit()
    assert limiter.spent("youtube") == 0


def test_quota_survives_a_new_process(store):
    """The whole point of persisting: a fresh run must see yesterday's spend."""
    RateLimiter(store).record("youtube", 5_000)
    assert RateLimiter(store).spent("youtube") == 5_000


def test_services_without_a_published_cap_are_unlimited(limiter):
    limiter.record("reddit", 10_000)
    assert limiter.remaining("reddit") == float("inf")
    limiter.acquire("reddit")          # must not raise


# ---- circuit breaker ------------------------------------------------------

def test_throttle_opens_a_cooldown_and_blocks_further_calls(limiter):
    limiter.observe("instagram_graph", 400, {}, {"error": {"code": 4}}, 1)
    with pytest.raises(ServiceCoolingDown):
        limiter.acquire("instagram_graph")


def test_cooldown_persists_across_processes(store):
    RateLimiter(store).observe("youtube", 403,
                               {}, {"error": {"errors": [{"reason": "quotaExceeded"}]}}, 100)
    # A new run must not immediately re-trigger the same throttle.
    with pytest.raises(ServiceCoolingDown):
        RateLimiter(store).acquire("youtube", 100)


def test_cooldown_expires(store):
    limiter = RateLimiter(store)
    limiter.open_circuit("reddit", 60, "test")
    assert limiter.cooldown_until("reddit") is not None

    future = RateLimiter(store,
                         clock=lambda: datetime.now(timezone.utc) + timedelta(hours=2))
    assert future.cooldown_until("reddit") is None
    future.acquire("reddit")           # must not raise


def test_success_clears_the_breaker(limiter):
    limiter.open_circuit("reddit", 600, "earlier failure")
    limiter.observe("reddit", 200, {}, {}, 1)
    assert limiter.cooldown_until("reddit") is None


def test_repeated_hard_failures_open_the_breaker(limiter):
    """A bad token would otherwise generate thousands of rejected calls."""
    for _ in range(5):
        limiter.observe("reddit", 401, {}, {"error": {"code": 100}}, 1)
    assert limiter.cooldown_until("reddit") is not None


# ---- proactive self-throttling --------------------------------------------

def test_high_usage_triggers_voluntary_slowdown(limiter):
    """Slowing down at 90% is cheaper than being throttled at 100%."""
    limiter.observe("instagram_graph", 200,
                    {"x-app-usage": '{"call_count":95}'}, {}, 1)
    assert limiter._slowdown.get("instagram_graph", 0) > 0

    limiter.acquire("instagram_graph")
    assert any(s > 0 for s in limiter.slept), "no extra delay was applied"


def test_usage_returning_to_normal_clears_the_slowdown(limiter):
    limiter.observe("instagram_graph", 200, {"x-app-usage": '{"call_count":95}'}, {}, 1)
    limiter.observe("instagram_graph", 200, {"x-app-usage": '{"call_count":10}'}, {}, 1)
    assert limiter._slowdown.get("instagram_graph", 0) == 0


# ---- pacing ---------------------------------------------------------------

def test_token_bucket_paces_a_burst():
    slept: list[float] = []
    bucket = TokenBucket(per_minute=60, burst=2)
    for _ in range(6):
        bucket.take(sleeper=slept.append)
    assert sum(slept) > 0, "a 6-request burst was not paced at all"


def test_backoff_is_jittered():
    """Fixed backoff makes several collectors retry in lockstep, producing a
    synchronised burst that looks like an attack."""
    limiter = RateLimiter(None)
    waits = {round(limiter.backoff(3), 6) for _ in range(40)}
    assert len(waits) > 30, "backoff appears deterministic"
    assert all(w <= 60.0 for w in waits)


def test_shared_limiter_pools_instagram_collectors(store):
    """Hashtag Search and Business Discovery draw on the same app-level hourly
    budget. Separate ledgers would each think they had the full allowance."""
    limiter = RateLimiter(store)
    for _ in range(100):
        limiter.record("instagram_graph", 1)          # as if from hashtag search
    for _ in range(50):
        limiter.record("instagram_graph", 1)          # as if from discovery
    assert limiter.spent("instagram_graph") == 150
    with pytest.raises(QuotaExhausted):
        limiter.acquire("instagram_graph", 5)          # ceiling is 200*0.75 = 150


def test_every_configured_service_has_sane_limits():
    for name, limit in LIMITS.items():
        assert limit.per_minute > 0, name
        assert 0 <= limit.reserve < 1, name
        if limit.quota is not None:
            assert limit.quota > 0 and limit.quota_window_s > 0, name


# ---- wall-clock quota reset ----------------------------------------------

def test_youtube_quota_refills_at_pacific_midnight(store):
    """A rolling 24h window would keep counting spend the platform has already
    forgiven, refusing runs against a budget that is actually full."""
    from zoneinfo import ZoneInfo

    pacific = ZoneInfo("America/Los_Angeles")
    # 23:00 Pacific — one hour before YouTube's reset.
    late = datetime(2026, 8, 20, 23, 0, tzinfo=pacific).astimezone(timezone.utc)
    store.conn.execute("INSERT INTO api_spend (service, ts, cost) VALUES (?,?,?)",
                       ("youtube", late.isoformat(), 9_000))
    store.conn.commit()

    before = RateLimiter(store, clock=lambda: late + timedelta(minutes=30))
    assert before.spent("youtube") == 9_000, "spend before reset must still count"

    # 00:30 Pacific the next day — after the reset.
    after = RateLimiter(store, clock=lambda: late + timedelta(hours=1, minutes=30))
    assert after.spent("youtube") == 0, "quota did not refill at the Pacific reset"
    after.acquire("youtube", 100)          # must not raise


def test_rolling_window_services_are_unaffected(store):
    """Instagram's hourly limit is genuinely rolling — it must not snap to a
    wall-clock boundary."""
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    store.conn.execute("INSERT INTO api_spend (service, ts, cost) VALUES (?,?,?)",
                       ("instagram_graph", (now - timedelta(minutes=30)).isoformat(), 50))
    store.conn.commit()

    limiter = RateLimiter(store, clock=lambda: now)
    assert limiter.spent("instagram_graph") == 50

    later = RateLimiter(store, clock=lambda: now + timedelta(hours=1, minutes=1))
    assert later.spent("instagram_graph") == 0


def test_window_start_survives_missing_tzdata():
    """Slim containers often lack tzdata. The fallback must be conservative,
    never optimistic — counting extra spend is safe, missing spend is not."""
    import reelpulse.limits as L

    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    broken = L.Limit("x", per_minute=10, quota=100, reset_tz="Not/AZone")
    start = L.window_start(broken, now)
    assert start <= now
    assert (now - start) <= timedelta(days=1)


# ---- distinct-counted limits ---------------------------------------------

def test_a_distinct_limit_refuses_a_call_with_nothing_to_count():
    """Instagram's window allows 30 unique hashtags, not 30 calls. A call that
    does not say which hashtag it is spending cannot be counted at all — the
    ledger would read zero forever and the first symptom would be Meta
    rejecting the 31st tag a week later."""
    lim = RateLimiter(sleeper=lambda s: None)
    with pytest.raises(ValueError, match="key="):
        lim.acquire("instagram_hashtags", 1.0)


def test_a_repeat_of_a_spent_key_is_allowed_even_at_the_ceiling():
    """Re-querying a hashtag already inside the window is free at Meta's end,
    so refusing it would strand budget we have already paid for."""
    lim = RateLimiter(sleeper=lambda s: None)
    for i in range(30):
        lim.record("instagram_hashtags", 1.0, "x", key=f"tag{i}")
    assert lim.remaining("instagram_hashtags") == 0

    lim.acquire("instagram_hashtags", 1.0, key="tag0")   # free, must not raise
    with pytest.raises(QuotaExhausted):
        lim.acquire("instagram_hashtags", 1.0, key="brandnew")


def test_distinct_spend_counts_things_not_calls():
    lim = RateLimiter(sleeper=lambda s: None)
    for tag in ["reels", "funny", "reels", "reels"]:
        lim.record("instagram_hashtags", 1.0, "x", key=tag)
    assert lim.spent("instagram_hashtags") == 2.0
    assert lim.spent_keys("instagram_hashtags") == {"reels", "funny"}


def test_distinct_keys_age_out_of_the_window():
    """The window rolls. A tag queried eight days ago has freed its slot."""
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    clock = {"t": now - timedelta(days=8)}
    lim = RateLimiter(sleeper=lambda s: None, clock=lambda: clock["t"])
    lim.record("instagram_hashtags", 1.0, "x", key="old")
    clock["t"] = now
    lim.record("instagram_hashtags", 1.0, "x", key="fresh")
    assert lim.spent_keys("instagram_hashtags") == {"fresh"}


def test_a_database_from_an_older_version_is_migrated_not_crashed():
    """The cached ledger on CI predates distinct counting, so it has no `key`
    column and every insert against it fails. The migration has to run when the
    store is *opened*, because `ledger import` — the first command every
    workflow runs — opens the store and constructs nothing else."""
    import sqlite3
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"

        raw = sqlite3.connect(path)        # the pre-distinct schema, verbatim
        raw.execute("CREATE TABLE api_spend (service TEXT NOT NULL, ts TEXT "
                    "NOT NULL, cost REAL NOT NULL, endpoint TEXT)")
        raw.execute("INSERT INTO api_spend VALUES ('youtube', ?, 100.0, 'x')",
                    (datetime.now(timezone.utc).isoformat(),))
        raw.commit()
        raw.close()

        store = Store(path)                # migrates on open
        lim = RateLimiter(store)
        lim.record("instagram_hashtags", 1.0, "x", key="reels")

        assert lim.spent("youtube") == 100.0, "existing spend was lost"
        assert lim.spent_keys("instagram_hashtags") == {"reels"}
        store.close()
