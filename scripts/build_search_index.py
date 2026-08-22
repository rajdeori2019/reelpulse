"""Build docs/search/index.html — a list of every keyword search run so far.

Without this, each search writes an HTML file nobody can find unless they
remember its exact slug. The index is regenerated from whatever is on disk, so
it never drifts out of sync with the actual files.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

SEARCH_DIR = Path("docs/search")


def describe(path: Path) -> dict:
    """Pull the query and headline numbers back out of a rendered dashboard."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    info = {
        "slug": path.stem,
        "query": path.stem.replace("-", " "),
        "results": None,
        "generated": None,
        "top": None,
    }

    # The report is inlined as JSON in a <script>; recover it rather than
    # guessing from the filename.
    match = re.search(r"const DATA = (\{.*?\});\s*\n", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            stats = data.get("stats", {})
            info["query"] = stats.get("query", info["query"])
            info["results"] = stats.get("relevant")
            info["generated"] = data.get("generated_at", "")[:16].replace("T", " ")
            top = data.get("top") or []
            if top:
                info["top"] = top[0].get("title", "")[:80]
        except (ValueError, KeyError):
            pass

    if not info["generated"]:
        info["generated"] = datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc).isoformat()[:16].replace("T", " ")
    return info


def main() -> None:
    SEARCH_DIR.mkdir(parents=True, exist_ok=True)
    pages = sorted((p for p in SEARCH_DIR.glob("*.html") if p.stem != "index"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    rows = [describe(p) for p in pages]

    items = "\n".join(
        f'''<li><a href="{html.escape(r["slug"])}.html">{html.escape(r["query"])}</a>
        <span class="meta">{r["results"] if r["results"] is not None else "?"} results
        &middot; {html.escape(str(r["generated"]))}</span>
        {f'<div class="top">top: {html.escape(str(r["top"]))}</div>' if r["top"] else ""}
        </li>''' for r in rows) or '<li class="empty">No searches run yet.</li>'

    SEARCH_DIR.joinpath("index.html").write_text(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ReelPulse — searches</title>
<style>
:root{{--bg:#f7f7f5;--panel:#fff;--ink:#1a1a17;--muted:#6b6b63;--line:#e3e3dd;--accent:#c65d3b}}
:root:not([data-theme="light"]){{@media(prefers-color-scheme:dark){{
--bg:#14140f;--panel:#1c1c17;--ink:#ece9df;--muted:#9a978c;--line:#2e2e26;--accent:#e08157}}}}
:root[data-theme="dark"]{{--bg:#14140f;--panel:#1c1c17;--ink:#ece9df;--muted:#9a978c;
--line:#2e2e26;--accent:#e08157}}
body{{margin:0;background:var(--bg);color:var(--ink);
font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;line-height:1.55}}
.wrap{{max-width:760px;margin:0 auto;padding:32px 20px 80px}}
h1{{font-size:30px;margin:0 0 4px;letter-spacing:-.02em}} h1 span{{color:var(--accent)}}
p.sub{{color:var(--muted);margin:0 0 24px;font-size:15px}}
ul{{list-style:none;padding:0;margin:0}}
li{{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:14px 16px;margin-bottom:10px}}
li a{{color:var(--accent);font-weight:600;font-size:16px;text-decoration:none}}
li a:hover{{text-decoration:underline}}
.meta{{color:var(--muted);font-size:13px;margin-left:8px}}
.top{{color:var(--muted);font-size:13px;margin-top:4px}}
.empty{{color:var(--muted);font-style:italic}}
a.back{{color:var(--accent);font-size:14px;display:inline-block;margin-top:26px}}
</style></head><body><div class="wrap">
<h1>Searches<span>.</span></h1>
<p class="sub">Keyword searches run so far. Trigger a new one from the Actions tab.</p>
<ul>{items}</ul>
<a class="back" href="../">&larr; back to the weekly leaderboard</a>
</div></body></html>""", encoding="utf-8")

    print(f"search index: {len(rows)} search(es)")


if __name__ == "__main__":
    main()
