"""The ledger has to outlive the runner that wrote it.

Every budget here is enforced by the platform across all your calls. On CI the
database that records them lives in a GitHub Actions cache, which is evicted
after seven days without a hit — the exact length of Instagram's hashtag window.
An eviction therefore makes every budget read as full, and the first symptom is
Meta rejecting a sweep with an error that says nothing about why.

These tests pin the property that stops that: a committed file the runs merge
from, which loses nothing to a wiped cache and cannot double-count on a warm one.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reelpulse.db import Store
from reelpulse.ledger import export_ledger, import_ledger, staleness
from reelpulse.limits import RateLimiter


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _seeded(path: Path, tags=("reels", "funny", "sourdough")):
    store = Store(path)
    lim = RateLimiter(store)
    for tag in tags:
        lim.record("instagram_hashtags", 1.0, "ig_hashtag_search", key=tag)
    lim.record("youtube", 100.0, "search.list")
    return store, lim


def test_spend_survives_a_wiped_database(workspace):
    """The scenario this file exists for: cache evicted, database gone, and the
    thirty-hashtag window still has to know what it already spent."""
    db, ledger = workspace / "t.db", workspace / "led.json"
    store, _ = _seeded(db)
    export_ledger(store, ledger)
    store.close()
    db.unlink()

    fresh = Store(db)
    import_ledger(fresh, ledger)
    lim = RateLimiter(fresh)
    assert lim.spent_keys("instagram_hashtags") == {"reels", "funny", "sourdough"}
    assert lim.spent("youtube") == 100.0
    fresh.close()


def test_importing_twice_does_not_double_count(workspace):
    """A warm cache already holds the rows. Importing on top of it must be a
    no-op, or every successful run would inflate its own recorded spend."""
    db, ledger = workspace / "t.db", workspace / "led.json"
    store, _ = _seeded(db)
    export_ledger(store, ledger)

    for _ in range(3):
        import_ledger(store, ledger)

    lim = RateLimiter(store)
    assert lim.spent("instagram_hashtags") == 3.0
    assert lim.spent("youtube") == 100.0
    store.close()


def test_import_merges_rather_than_replaces(workspace):
    """A run that died before exporting leaves spend in the database and not in
    the file. Replacing would hand that budget back and let it be spent twice."""
    db, ledger = workspace / "t.db", workspace / "led.json"
    store, lim = _seeded(db, tags=("reels",))
    export_ledger(store, ledger)
    lim.record("instagram_hashtags", 1.0, "x", key="unexported")

    import_ledger(store, ledger)
    assert RateLimiter(store).spent_keys("instagram_hashtags") == {
        "reels", "unexported"}
    store.close()


def test_a_live_cooldown_travels_with_the_ledger(workspace):
    """Being told to back off is the most important thing to carry forward.
    Forgetting it means the next run walks straight back into the throttle,
    which is what escalates one into a restriction."""
    db, ledger = workspace / "t.db", workspace / "led.json"
    store = Store(db)
    RateLimiter(store).open_circuit("youtube", 3_600, "quotaExceeded")
    export_ledger(store, ledger)
    store.close()
    db.unlink()

    fresh = Store(db)
    import_ledger(fresh, ledger)
    assert RateLimiter(fresh).cooldown_until("youtube") is not None
    fresh.close()


def test_import_never_shortens_a_cooldown(workspace):
    """A cooldown is a floor. An older file must not talk a live one down."""
    db, ledger = workspace / "t.db", workspace / "led.json"
    store = Store(db)
    lim = RateLimiter(store)
    lim.open_circuit("youtube", 600, "short")
    export_ledger(store, ledger)
    lim.open_circuit("youtube", 7_200, "long")
    longer = lim.cooldown_until("youtube")

    import_ledger(store, ledger)
    assert RateLimiter(store).cooldown_until("youtube") == longer
    store.close()


def test_an_expired_cooldown_is_not_exported(workspace):
    """Carrying a stale cooldown forward would idle a budget that is free."""
    db, ledger = workspace / "t.db", workspace / "led.json"
    store = Store(db)
    store.conn.execute(
        "CREATE TABLE IF NOT EXISTS api_cooldown (service TEXT PRIMARY KEY, "
        "until TEXT NOT NULL, reason TEXT, failures INTEGER DEFAULT 0)")
    RateLimiter(store)  # ensure schema
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    store.conn.execute("INSERT OR REPLACE INTO api_cooldown VALUES (?,?,?,?)",
                       ("youtube", past, "old", 1))
    store.conn.commit()

    export_ledger(store, ledger)
    assert json.loads(ledger.read_text())["cooldowns"] == []
    store.close()


def test_a_missing_ledger_is_not_an_error(workspace):
    """First run in a fresh repo. It should start empty and say so, not crash
    the whole weekly job."""
    store = Store(workspace / "t.db")
    result = import_ledger(store, workspace / "absent.json")
    assert result["imported"] == 0 and "no ledger" in result["reason"]
    store.close()


def test_a_corrupt_ledger_degrades_instead_of_crashing(workspace):
    """A half-written file from a killed job must not stop the next run. It
    under-counts, which the staleness warning is there to surface."""
    db, ledger = workspace / "t.db", workspace / "led.json"
    ledger.write_text("{not json")
    store = Store(db)
    result = import_ledger(store, ledger)
    assert result["imported"] == 0 and "unreadable" in result["reason"]
    store.close()


def test_an_export_is_byte_stable_when_nothing_changed(workspace):
    """An unstable file would churn a commit on every run, and a repo full of
    no-op commits is a repo nobody reads the history of."""
    db, ledger = workspace / "t.db", workspace / "led.json"
    store, _ = _seeded(db)
    export_ledger(store, ledger)
    first = ledger.read_text()
    payload = json.loads(first)
    export_ledger(store, ledger)
    second = json.loads(ledger.read_text())
    assert payload["spend"] == second["spend"]
    store.close()


def test_staleness_speaks_up_when_the_file_predates_every_window(workspace):
    ledger = workspace / "led.json"
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    ledger.write_text(json.dumps({"format": 1, "written_at": old,
                                  "spend": [], "cooldowns": []}))
    assert "older than every window" in staleness(ledger)


def test_staleness_is_quiet_about_a_fresh_file(workspace):
    db, ledger = workspace / "t.db", workspace / "led.json"
    store, _ = _seeded(db)
    export_ledger(store, ledger)
    assert staleness(ledger) is None
    store.close()


def test_rows_older_than_every_window_are_dropped(workspace):
    """Otherwise the committed file grows without bound for no benefit — no
    decision anywhere reads a row that old."""
    db, ledger = workspace / "t.db", workspace / "led.json"
    store = Store(db)
    RateLimiter(store)
    ancient = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    store.conn.execute(
        "INSERT INTO api_spend (service, ts, cost, endpoint, key) "
        "VALUES (?,?,?,?,?)", ("instagram_hashtags", ancient, 1.0, "x", "old"))
    store.conn.commit()

    export_ledger(store, ledger)
    keys = {r.get("key") for r in json.loads(ledger.read_text())["spend"]}
    assert "old" not in keys
    store.close()
