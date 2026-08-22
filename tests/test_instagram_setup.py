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
    monkeypatch.setattr(si, "_get", lambda p, q: (
        False, {"error": {"message": "Session expired", "code": 190}}))
    result = si.check_token("t")
    assert result["ok"] is False
    assert "expired or revoked" in result["detail"]


def test_missing_scopes_are_listed(monkeypatch):
    monkeypatch.setattr(si, "_get", lambda p, q: _valid_token(
        ["instagram_basic", "pages_show_list"]))
    result = si.check_token("t")
    assert result["ok"] is True
    assert "instagram_manage_insights" in result["missing"]
    assert "pages_read_engagement" in result["missing"]


def test_never_expiring_token_is_flagged(monkeypatch):
    monkeypatch.setattr(si, "_get", lambda p, q: (
        True, {"data": {"is_valid": True, "scopes": [], "expires_at": 0}}))
    assert si.check_token("t")["never_expires"] is True


def test_unlinked_page_is_distinguished_from_no_pages(monkeypatch):
    """The failure mode that wastes the most time: Pages exist, but none has an
    Instagram professional account attached. Hashtag Search needs that link."""
    monkeypatch.setattr(si, "_get", lambda p, q: (
        True, {"data": [{"name": "My Page", "id": "1"}]}))
    result = si.find_accounts("t")
    assert result["ok"] is False
    assert "none with an Instagram" in result["detail"]
    assert result["accounts"] and not result["linked"]


def test_linked_account_yields_the_id(monkeypatch):
    monkeypatch.setattr(si, "_get", lambda p, q: (True, {"data": [
        {"name": "P", "id": "1", "instagram_business_account":
            {"id": "17841400000000", "username": "me", "followers_count": 10}}]}))
    result = si.find_accounts("t")
    assert result["ok"] is True
    assert result["linked"][0]["ig_user_id"] == "17841400000000"


def test_hashtag_search_failure_surfaces_the_reason(monkeypatch):
    monkeypatch.setattr(si, "_get", lambda p, q: (
        False, {"error": {"message": "Insufficient permission", "code": 200}}))
    result = si.check_hashtag_search("t", "1")
    assert result["ok"] is False
    assert "missing a permission" in result["detail"]


def test_hashtag_resolves_but_media_fails_is_reported_precisely(monkeypatch):
    """Two different calls; saying which one broke saves real debugging time."""
    def _get(path, params):
        if path == "ig_hashtag_search":
            return True, {"data": [{"id": "h1"}]}
        return False, {"error": {"message": "nope", "code": 10}}
    monkeypatch.setattr(si, "_get", _get)
    result = si.check_hashtag_search("t", "1")
    assert result["ok"] is False
    assert "top_media failed" in result["detail"]


def test_business_discovery_counts_real_view_counts(monkeypatch):
    monkeypatch.setattr(si, "_get", lambda p, q: (True, {"business_discovery": {
        "username": "natgeo", "followers_count": 1_000,
        "media": {"data": [{"id": "a", "view_count": 5}, {"id": "b"}]}}}))
    result = si.check_business_discovery("t", "1")
    assert result["ok"] is True
    assert "1 with view_count" in result["detail"]


def test_run_all_stops_when_the_token_is_dead(monkeypatch):
    monkeypatch.setattr(si, "_get", lambda p, q: (
        False, {"error": {"message": "bad", "code": 190}}))
    result = si.run_all("t")
    assert result["fatal"] == "token invalid or expired"
    assert "hashtag_search" not in result


def test_run_all_auto_discovers_the_account_id(monkeypatch):
    def _get(path, params):
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


@pytest.mark.parametrize("code,fragment", [
    (190, "expired or revoked"), (200, "missing a permission"),
    (100, "not a professional account"), (4, "rate limit"),
])
def test_error_codes_carry_actionable_hints(code, fragment):
    assert fragment in si._err({"error": {"message": "x", "code": code}})
