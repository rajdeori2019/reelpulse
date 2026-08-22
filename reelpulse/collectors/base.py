"""Shared HTTP plumbing for collectors.

Every request goes through `RateLimiter`, which enforces quota *before* sending
and reacts to what the platform reports afterwards. Collectors do not implement
their own throttling — a per-collector sleep cannot see a budget that spans
processes, and that is where blocks actually come from.

Every collector is also degradable: if its credential is missing, its quota is
spent, or its API is down, it logs one line and returns an empty list. A missing
source lowers coverage; it never crashes a run. That matters because this runs
unattended in CI, and a run that dies at 06:00 on a Monday is a run nobody sees.
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from ..limits import (QuotaExhausted, RateLimiter, ServiceCoolingDown,
                      limit_for)

log = logging.getLogger("reelpulse")

USER_AGENT = ("ReelPulse/1.1 (open-source short-form trend research; "
              "+https://github.com/) contact-via-repo-issues")


class Collector:
    name = "base"
    service = "base"              # key into limits.LIMITS
    requires: list[str] = []      # env var names this collector needs

    def __init__(self, config: dict[str, Any] | None = None,
                 limiter: RateLimiter | None = None) -> None:
        self.config = config or {}
        self.limiter = limiter or RateLimiter()
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def collect(self) -> list:
        raise NotImplementedError

    def safe_collect(self) -> list:
        if not self.enabled:
            log.info("[%s] disabled in sources.yaml — skipping", self.name)
            return []
        try:
            items = self.collect()
            log.info("[%s] collected %d candidates", self.name, len(items))
            return items
        except (QuotaExhausted, ServiceCoolingDown) as exc:
            # Not an error. The limiter did its job; say so plainly rather than
            # burying it in a stack trace, because the fix is "wait" or "raise
            # the budget", not "debug the collector".
            log.warning("[%s] skipped: %s", self.name, exc)
            return []
        except Exception as exc:  # noqa: BLE001 — a dead source must not kill the run
            log.warning("[%s] failed (%s: %s) — continuing without it",
                        self.name, type(exc).__name__, exc)
            return []

    # ---- helpers -------------------------------------------------------

    def get_json(self, url: str, params: dict | None = None, *,
                 headers: dict | None = None, retries: int = 3,
                 timeout: int = 20, cost: float = 1.0,
                 service: str | None = None,
                 respect_reserve: bool = True) -> dict:
        """GET with quota accounting, adaptive throttling and honest retries.

        `cost` matters for YouTube, where quota is denominated in units rather
        than calls: a search.list costs 100 and a videos.list costs 1. Charging
        every call as 1 would under-count spend by two orders of magnitude on
        exactly the endpoint most likely to exhaust the budget.
        """
        service = service or self.service
        limit = limit_for(service)
        last_reason = "unknown"

        for attempt in range(retries):
            # Pre-flight. Raises rather than sending when it cannot be afforded;
            # both exceptions propagate to safe_collect().
            self.limiter.acquire(service, cost, respect_reserve=respect_reserve)

            try:
                resp = self.session.get(url, params=params, headers=headers,
                                        timeout=timeout)
            except requests.RequestException as exc:
                last_reason = f"{type(exc).__name__}: {exc}"
                self.limiter.record(service, cost, url)
                if attempt == retries - 1:
                    break
                self._sleep_backoff(attempt, service)
                continue

            body: dict | None = None
            if resp.content:
                try:
                    body = resp.json()
                except ValueError:
                    body = None

            rate_limited, retryable, reason = self.limiter.observe(
                service, resp.status_code, resp.headers, body, cost, url)
            last_reason = reason

            if 200 <= resp.status_code < 300:
                return body if body is not None else {}

            if rate_limited:
                # observe() has already opened a persisted cooldown. Retrying
                # inside this run would only deepen it.
                raise ServiceCoolingDown(
                    f"{service} rate limited ({reason}); cooling down. "
                    f"Run `reelpulse limits` to see when it clears.")

            if not retryable:
                raise RuntimeError(
                    f"{service} {reason} for {url.split('?')[0]} — not retryable")

            if attempt == retries - 1:
                break
            self._sleep_backoff(attempt, service)

        raise RuntimeError(f"GET {url.split('?')[0]} failed after {retries} "
                           f"attempts ({last_reason})")

    def _sleep_backoff(self, attempt: int, service: str) -> None:
        wait = self.limiter.backoff(attempt)
        log.info("[%s] retrying in %.1fs", service, wait)
        self.limiter.sleeper(wait)
