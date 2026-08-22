# Data sources, and the lines this project does not cross

Short-form analytics is a category with a scraping problem. This document exists
so you can tell, without reading the code, exactly what ReelPulse touches.

## What it uses

| Source | Access | Cost | What it provides |
|---|---|---|---|
| Instagram Hashtag Search | Business/Creator token | Free | Other people's public reels by hashtag: likes, comments, caption, permalink. 30 unique hashtags / 7 days |
| Instagram Business Discovery | Business/Creator token | Free | A named professional account's reels, **including real view counts** |
| YouTube Data API v3 | Official API key | Free, 10,000 units/day | View counts at global scale for cross-posted clips |
| Instagram oEmbed | Tokenless since [15 Jun 2026](https://developers.facebook.com/blog/post/2026/06/15/tokenless-access-to-meta-oembed-apis/) | Free, no App Review | Confirms a reel is public and live; official embed markup |
| Instagram Graph API | Your own Business/Creator token | Free | Real metrics **for accounts you own only** |
| Reddit API | OAuth client credentials | Free (~100 QPM) | Off-platform share counts; public Instagram permalinks |
| Wikimedia Pageviews | None needed | Free | Topic momentum |
| Google Trends (pytrends) | Unofficial, **opt-in, off by default** | Free | Alternative topic momentum |

## What it does not do

- **No Instagram scraping.** No HTML parsing of instagram.com, no private API endpoints, no `?__a=1`, no headless browser pointed at a feed.
- **No logged-in sessions.** No account credentials, no session cookies, no rotating proxies. Nothing that could get an account banned.
- **No personal data.** Only public post metrics and public creator handles. No follower graphs, no DMs, no profile scraping. Hashtag Search cannot return usernames and ReelPulse does not try to recover them.
- **No invented numbers.** Reels without a published view count are ranked on engagement and labelled as such. A view count is never estimated from likes.
- **yt-dlp is YouTube-only.** The optional transcript feature is deliberately restricted to the public YouTube mirror. Pointing it at Instagram would mean pulling media Meta's terms do not permit, and no transcript is a smaller loss than a takedown.

## Quota behaviour

Every request passes through a rate limiter that refuses to send when the budget
cannot cover it. Quota spend and cooldowns are persisted to SQLite, so limits
that span runs are genuinely enforced rather than reset by each process.

Rate limits are detected from the response **body**, not just the status code,
because Meta signals throttling with HTTP 400 (error codes 4/17/32/613 and
80000-80014) and YouTube with HTTP 403 (`quotaExceeded`) — neither returns 429.
Meta's `X-App-Usage` percentages are read on every response and the client slows
itself down above 80% rather than waiting to be throttled.

The default config uses roughly 2,400 of YouTube's 10,000 daily units — under a
quarter — and each service reserves headroom (YouTube 20%, Instagram 25%) that
ad-hoc commands cannot spend, so scheduled runs are never starved by manual use.

Retries use full jitter and are attempted **only** for transient failures (429,
5xx, network). A 401 or a malformed 400 is never retried: repeating it wastes
quota and looks like abuse. Five consecutive hard failures open a cooldown, so a
bad token cannot generate thousands of rejected calls.

`reelpulse limits` shows current spend, pacing and any active cooldown.

## About TikTok

TikTok's Research API is free but restricted to approved academic and non-profit
researchers in specific regions; TikTok's own FAQ states creators, advertisers
and commercial users are not eligible. It is scaffolded in `config/sources.yaml`
and **disabled by default**. Enable it only if you hold an approved research
client.

## If you fork this

Two things stay true only as long as you keep them true:

1. **Don't add a scraper.** The moment one goes in, the project inherits every
   failure mode it was built to avoid — bans, breakage, and a dataset you can't
   publish.
2. **Don't drop the caveats.** The methodology note on the dashboard and the
   confidence labels on every rule are not decoration. Strip them and the tool
   starts making claims its data cannot support.
