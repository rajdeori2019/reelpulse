"""The Instagram setup doctor.

Meta reports an unlinked Facebook Page and a missing permission with nearly
identical error text, which is why getting a token right is the most
error-prone step in this project. These tests pin the distinctions.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reelpulse.setup_instagram as si


def _valid_token(scopes=None, expires_in=60 * 24 * 3600):
    return True, {"data": {"is_valid": True, "type": "USER",
                           "scopes": scopes if scopes is not None else list(si.NEEDED),
                           "expires_at": int(time.time()) + expires_in}}


def test_expired_token_is_named_as_such(monkeypatch):
    monkeypatch.setattr(si, "_get", lambda p, q, **kw: (
        False, {"error": {"message": "Session expired", "code": 190}}))
    result = si.check_token("t")
    assert result["ok"] is False
    assert "expired or revoked" in result["detail"]


def test_missing_scopes_are_listed(monkeypatch):
    monkeypatch.setattr(si, "_get", lambda p, q, **kw: _valid_token(
        ["instagram_basic", "pages_show_list"]))
    result = si.check_token("t")
    assert result["ok"] is True
    assert "instagram_manage_insights" in result["missing"]
    assert "pages_read_engagement" in result["missing"]


def test_never_expiring_token_is_flagged(monkeypatch):
    monkeypatch.setattr(si, "_get", lambda p, q, **kw: (
        True, {"data": {"is_valid": True, "scopes": [], "expires_at": 0}}))
    assert si.check_token("t")["never_expires"] is True


def test_unlinked_page_is_distinguished_from_no_pages(monkeypatch):
    """The failure mode that wastes the most time: Pages exist, but none has an
    Instagram professional account attached. Hashtag Search needs that link."""
    monkeypatch.setattr(si, "_get", lambda p, q, **kw: (
        True, {"data": [{"name": "My Page", "id": "1"}]}))
    result = si.find_accounts("t")
    assert result["ok"] is False
    assert "none with an Instagram" in result["detail"]
    assert result["accounts"] and not result["linked"]


def test_linked_account_yields_the_id(monkeypatch):
    monkeypatch.setattr(si, "_get", lambda p, q, **kw: (True, {"data": [
        {"name": "P", "id": "1", "instagram_business_account":
            {"id": "17841400000000", "username": "me", "followers_count": 10}}]}))
    result = si.find_accounts("t")
    assert result["ok"] is True
    assert result["linked"][0]["ig_user_id"] == "17841400000000"


def test_hashtag_search_failure_surfaces_the_reason(monkeypatch):
    monkeypatch.setattr(si, "_get", lambda p, q, **kw: (
        False, {"error": {"message": "Insufficient permission", "code": 200}}))
    result = si.check_hashtag_search("t", "1")
    assert result["ok"] is False
    assert "missing a permission" in result["detail"]


def test_hashtag_resolves_but_media_fails_is_reported_precisely(monkeypatch):
    """Two different calls; saying which one broke saves real debugging time."""
    def _get(path, params, **kw):
        if path == "ig_hashtag_search":
            return True, {"data": [{"id": "h1"}]}
        return False, {"error": {"message": "nope", "code": 10}}
    monkeypatch.setattr(si, "_get", _get)
    result = si.check_hashtag_search("t", "1")
    assert result["ok"] is False
    assert "top_media failed" in result["detail"]


def test_business_discovery_counts_real_view_counts(monkeypatch):
    monkeypatch.setattr(si, "_get", lambda p, q, **kw: (True, {"business_discovery": {
        "username": "natgeo", "followers_count": 1_000,
        "media": {"data": [{"id": "a", "view_count": 5}, {"id": "b"}]}}}))
    result = si.check_business_discovery("t", "1")
    assert result["ok"] is True
    assert "1 with view_count" in result["detail"]


def test_run_all_stops_when_the_token_is_dead(monkeypatch):
    monkeypatch.setattr(si, "_get", lambda p, q, **kw: (
        False, {"error": {"message": "bad", "code": 190}}))
    result = si.run_all("t")
    assert result["fatal"] == "token invalid or expired"
    assert "hashtag_search" not in result


def test_run_all_auto_discovers_the_account_id(monkeypatch):
    def _get(path, params, **kw):
        if path == "debug_token":
            return _valid_token()
        if path == "me/accounts":
            return True, {"data": [{"name": "P", "id": "1",
                "instagram_business_account": {"id": "IG42", "username": "u",
                                               "followers_count": 1}}]}
        if path == "ig_hashtag_search":
            return True, {"data": [{"id": "h"}]}
        return True, {"data": [], "business_discovery": {"media": {"data": []}}}
    monkeypatch.setattr(si, "_get", _get)
    result = si.run_all("t")
    assert result["ig_user_id"] == "IG42", "did not auto-discover the account id"


def test_network_failure_does_not_raise(monkeypatch):
    import requests
    def _boom(*a, **k):
        raise requests.RequestException("no route")
    monkeypatch.setattr(si.requests, "get", _boom)
    ok, body = si._get("debug_token", {})
    assert ok is False
    assert "network" in body["error"]["message"]


class _FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self.headers = {}
        self._body = body if body is not None else {"data": [{"id": "h1"}]}

    def json(self):
        return self._body


def _limiter():
    from reelpulse.limits import RateLimiter
    return RateLimiter(sleeper=lambda s: None)


def test_every_setup_call_is_metered(monkeypatch):
    """The doctor used to bypass the limiter entirely.

    It is only six calls, but a hole in the accounting is a hole regardless of
    size — and one of these six spends a hashtag slot the weekly run needs.
    """
    monkeypatch.setattr(si.requests, "get", lambda *a, **k: _FakeResponse())
    lim = _limiter()
    si._get("debug_token", {}, limiter=lim)
    assert lim.spent("instagram_graph") == 1.0


def test_the_hashtag_probe_books_a_hashtag_slot_not_a_graph_call(monkeypatch):
    """Hashtag Search is quota'd separately: 30 distinct tags per 7 days.

    Booking the probe against the hourly Graph budget instead would let the
    weekly run walk into a wall it had no record of approaching.
    """
    monkeypatch.setattr(si.requests, "get", lambda *a, **k: _FakeResponse())
    lim = _limiter()
    si._get("ig_hashtag_search", {}, limiter=lim,
            service="instagram_hashtags", key="reels")
    assert lim.spent("instagram_hashtags") == 1.0
    assert lim.spent("instagram_graph") == 0.0


def test_reprobing_the_same_hashtag_is_free(monkeypatch):
    """The window counts distinct hashtags, not calls.

    Someone re-running the doctor to check a fix should not burn a second slot
    for a tag already inside the window.
    """
    monkeypatch.setattr(si.requests, "get", lambda *a, **k: _FakeResponse())
    lim = _limiter()
    for _ in range(4):
        si._get("ig_hashtag_search", {}, limiter=lim,
                service="instagram_hashtags", key="reels")
    si._get("ig_hashtag_search", {}, limiter=lim,
            service="instagram_hashtags", key="funny")
    assert lim.spent("instagram_hashtags") == 2.0


def test_an_exhausted_budget_refuses_before_the_request_is_sent(monkeypatch):
    """Refusing beforehand is the point: a call that cannot succeed still
    counts against you, and it is what turns a throttle into a restriction."""
    sent = []
    monkeypatch.setattr(si.requests, "get",
                        lambda *a, **k: sent.append(1) or _FakeResponse())
    lim = _limiter()
    lim.record("instagram_graph", 200.0, "prior")
    ok, body = si._get("debug_token", {}, limiter=lim)
    assert ok is False
    assert body["error"]["code"] == "budget"
    assert not sent, "request was sent despite an exhausted budget"


def test_a_metered_call_is_booked_once_not_twice(monkeypatch):
    """observe() records; callers must not record again on top of it."""
    monkeypatch.setattr(si.requests, "get", lambda *a, **k: _FakeResponse())
    lim = _limiter()
    si._get("me/accounts", {}, limiter=lim, key="p1")
    assert lim.spent("instagram_graph") == 1.0


def test_a_network_failure_still_costs_its_slot(monkeypatch):
    """The request left the machine. Whether a reply came back is irrelevant to
    the platform's counter, so it must not be free in ours either."""
    import requests as _r
    def _boom(*a, **k):
        raise _r.RequestException("no route")
    monkeypatch.setattr(si.requests, "get", _boom)
    lim = _limiter()
    si._get("debug_token", {}, limiter=lim)
    assert lim.spent("instagram_graph") == 1.0


@pytest.mark.parametrize("code,fragment", [
    (190, "expired or revoked"), (200, "missing a permission"),
    (100, "not a professional account"), (4, "rate limit"),
])
def test_error_codes_carry_actionable_hints(code, fragment):
    assert fragment in si._err({"error": {"message": "x", "code": code}})


def test_the_unlinked_hint_does_not_send_people_to_accounts_centre():
    """Accounts Centre links Instagram to a personal *account*. The Graph API
    cannot see that link at all — only a connection to a specific Page produces
    `instagram_business_account`. An earlier version of this hint named
    Accounts Centre as the fix, which sends people to do the wrong thing and
    then wonder why nothing changed."""
    import re
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent
              / "reelpulse" / "cli.py").read_text(encoding="utf-8")
    block = source[source.index("No Instagram professional account linked"):]
    block = block[:block.index("if results.get(\"fatal\")")]

    assert "Linked accounts" in block, "the working path is not named"
    assert "Public business information" in block, "the app path is not named"
    # Accounts Centre may be named, but only as the thing NOT to do.
    for match in re.finditer(r"Accounts Centre", block):
        context = block[max(0, match.start() - 200):match.end() + 80]
        assert "WRONG" in context, "Accounts Centre is presented as a fix"


def test_the_newer_connected_field_is_accepted_as_a_link(monkeypatch):
    """Meta's newer in-app flow populates `connected_instagram_account` and
    leaves `instagram_business_account` empty. Asking for only the documented
    field makes a Page that IS linked report as unlinked, which sends people
    off to re-do a connection that already exists."""
    monkeypatch.setattr(si, "_get", lambda p, q, **kw: (True, {"data": [
        {"name": "Prafulla Deori", "id": "1",
         "connected_instagram_account": {"id": "IG99", "username": "iamprafz",
                                         "followers_count": 4_200}}]}))
    result = si.find_accounts("t")
    assert result["ok"] is True
    assert result["linked"][0]["ig_user_id"] == "IG99"
    assert result["linked"][0]["via"] == "connected_instagram_account"


def test_the_documented_field_wins_when_both_are_present(monkeypatch):
    monkeypatch.setattr(si, "_get", lambda p, q, **kw: (True, {"data": [
        {"name": "P", "id": "1",
         "instagram_business_account": {"id": "BIZ", "username": "a"},
         "connected_instagram_account": {"id": "CON", "username": "a"}}]}))
    result = si.find_accounts("t")
    assert result["linked"][0]["ig_user_id"] == "BIZ"
    assert result["linked"][0]["via"] == "instagram_business_account"


def test_an_unlinked_page_reports_which_fields_were_empty(monkeypatch):
    """'3 Pages, none linked' is not a diagnosis. Which field Meta left empty
    is the diagnosis."""
    monkeypatch.setattr(si, "_get", lambda p, q, **kw: (True, {"data": [
        {"name": "Page A", "id": "1"}]}))
    result = si.find_accounts("t")
    acct = result["accounts"][0]
    assert acct["has_business_field"] is False
    assert acct["has_connected_field"] is False
    assert acct["via"] is None
