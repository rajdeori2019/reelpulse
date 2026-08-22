"""Diagnose an Instagram token and find the account id automatically.

Getting a Meta token right is the single most error-prone step in this project,
and the failures are all silent in the same way: the token is valid, the call
returns 400, and the message names a permission rather than the thing you got
wrong. This module turns that into a checklist.

It also removes a manual step. `IG_USER_ID` is not shown anywhere obvious in the
Meta UI — it has to be read off the Facebook Page the account is linked to. So
rather than asking for it, this walks Pages -> linked Instagram account and
prints the id it found.

Every check is cheap and read-only. Nothing here posts, and nothing here spends
meaningful quota.
"""
from __future__ import annotations

from typing import Any

import requests

API = "https://graph.facebook.com/v23.0"
TIMEOUT = 20

# Permissions the three collectors actually need. Hashtag Search is the reason
# this project is on the Facebook-Login path at all.
NEEDED = {
    "instagram_basic": "read Instagram account and media",
    "pages_read_engagement": "read the linked Facebook Page",
    "pages_show_list": "list which Pages you manage",
    "instagram_manage_insights": "read insights, and Hashtag Search",
}


def _get(path: str, params: dict) -> tuple[bool, Any]:
    try:
        resp = requests.get(f"{API}/{path}", params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return False, {"error": {"message": f"network: {exc}"}}
    try:
        body = resp.json()
    except ValueError:
        body = {"error": {"message": f"non-JSON response ({resp.status_code})"}}
    return resp.ok and "error" not in body, body


def _err(body: Any) -> str:
    """Meta's error message, plus the hint its code carries.

    The codes matter more than the prose here: Meta reports a missing Page link
    and a missing permission with near-identical wording, and the code is what
    separates them.
    """
    error = (body or {}).get("error", {}) if isinstance(body, dict) else {}
    message = error.get("message") or str(body)[:160]
    code = error.get("code")

    hints = {
        190: "token expired or revoked — generate a new one",
        200: "missing a permission; check the scopes list above",
        100: "usually a wrong id, or the account is not a professional account",
        10: "permission not granted for this call",
        4: "app-level rate limit reached — wait and retry",
        803: "that id is not visible to this token",
    }
    parts = [message]
    if code is not None:
        parts.append(f"[code {code}]")
        if code in hints:
            parts.append(f"- {hints[code]}")
    return " ".join(parts)


def check_token(token: str) -> dict[str, Any]:
    """Validity, expiry and granted scopes, via Meta's own debug endpoint."""
    ok, body = _get("debug_token", {"input_token": token, "access_token": token})
    if not ok:
        return {"ok": False, "detail": _err(body)}

    data = body.get("data", {})
    scopes = set(data.get("scopes") or [])
    expires = data.get("expires_at", 0)
    return {
        "ok": bool(data.get("is_valid")),
        "type": data.get("type"),
        "app": data.get("application"),
        "expires_at": expires,
        "never_expires": expires == 0,
        "scopes": sorted(scopes),
        "missing": sorted(k for k in NEEDED if k not in scopes),
        "detail": "" if data.get("is_valid") else "token reported invalid",
    }


def find_accounts(token: str) -> dict[str, Any]:
    """Walk Pages -> linked Instagram professional account.

    This is where the Facebook-Login requirement bites: an Instagram account
    with no Page attached returns an empty list here, and every downstream call
    then fails with a permissions-shaped error that never mentions the Page.
    """
    ok, body = _get("me/accounts", {
        "fields": "name,id,instagram_business_account{id,username,followers_count}",
        "access_token": token, "limit": 50})
    if not ok:
        return {"ok": False, "detail": _err(body), "accounts": []}

    accounts = []
    for page in body.get("data", []):
        ig = page.get("instagram_business_account")
        accounts.append({
            "page": page.get("name"),
            "page_id": page.get("id"),
            "ig_user_id": (ig or {}).get("id"),
            "ig_username": (ig or {}).get("username"),
            "followers": (ig or {}).get("followers_count"),
        })
    linked = [a for a in accounts if a["ig_user_id"]]
    return {
        "ok": bool(linked),
        "accounts": accounts,
        "linked": linked,
        "detail": ("" if linked else
                   f"{len(accounts)} Page(s) found, none with an Instagram "
                   "professional account linked."),
    }


def check_hashtag_search(token: str, ig_user_id: str,
                         probe: str = "reels") -> dict[str, Any]:
    """The capability the whole project depends on.

    Spends one of the 30-per-7-days hashtag slots, which is why the probe uses a
    hashtag the default config already queries — so verification costs a slot
    the weekly run was going to spend anyway.
    """
    ok, body = _get("ig_hashtag_search",
                    {"user_id": ig_user_id, "q": probe, "access_token": token})
    if not ok:
        return {"ok": False, "detail": _err(body)}
    items = body.get("data") or []
    if not items:
        return {"ok": False, "detail": f"#{probe} resolved to nothing"}

    hashtag_id = items[0]["id"]
    ok2, body2 = _get(f"{hashtag_id}/top_media", {
        "user_id": ig_user_id, "fields": "id,like_count,media_type",
        "limit": 3, "access_token": token})
    if not ok2:
        return {"ok": False, "detail": f"resolved #{probe} but top_media failed: "
                                       f"{_err(body2)}"}
    return {"ok": True, "sample": len(body2.get("data", [])),
            "detail": f"#{probe} returned {len(body2.get('data', []))} media"}


def check_business_discovery(token: str, ig_user_id: str,
                             probe: str = "natgeo") -> dict[str, Any]:
    """The only free route to view counts on other people's reels."""
    field = (f"business_discovery.username({probe})"
             "{username,followers_count,media.limit(2){id,media_type,view_count}}")
    ok, body = _get(ig_user_id, {"fields": field, "access_token": token})
    if not ok:
        return {"ok": False, "detail": _err(body)}

    account = body.get("business_discovery") or {}
    media = (account.get("media") or {}).get("data", [])
    with_views = sum(1 for m in media if m.get("view_count") is not None)
    return {"ok": True, "probe": probe,
            "detail": f"@{probe}: {account.get('followers_count', 0):,} followers, "
                      f"{len(media)} media, {with_views} with view_count"}


def check_own_insights(token: str, ig_user_id: str) -> dict[str, Any]:
    ok, body = _get(f"{ig_user_id}/media",
                    {"fields": "id,media_product_type", "limit": 5,
                     "access_token": token})
    if not ok:
        return {"ok": False, "detail": _err(body)}
    media = body.get("data", [])
    reels = sum(1 for m in media if m.get("media_product_type") == "REELS")
    return {"ok": True,
            "detail": f"{len(media)} recent media, {reels} reels "
                      f"(calibration needs 12+ reels)"}


def run_all(token: str, ig_user_id: str | None = None,
            hashtag_probe: str = "reels") -> dict[str, Any]:
    """Full diagnosis. Stops early only when continuing cannot work."""
    results: dict[str, Any] = {"token": check_token(token)}
    if not results["token"]["ok"]:
        results["fatal"] = "token invalid or expired"
        return results

    results["accounts"] = find_accounts(token)
    if not ig_user_id:
        linked = results["accounts"].get("linked") or []
        ig_user_id = linked[0]["ig_user_id"] if linked else None
    results["ig_user_id"] = ig_user_id

    if not ig_user_id:
        results["fatal"] = ("no Instagram professional account is linked to any "
                            "Facebook Page this token can see")
        return results

    results["hashtag_search"] = check_hashtag_search(token, ig_user_id, hashtag_probe)
    results["business_discovery"] = check_business_discovery(token, ig_user_id)
    results["own_insights"] = check_own_insights(token, ig_user_id)
    return results
