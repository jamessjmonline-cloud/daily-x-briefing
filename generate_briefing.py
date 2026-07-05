#!/usr/bin/env python3
"""Generate a clean daily viral X briefing using Agent-Reach/OpenCLI.

This script is intentionally dependency-light so Hermes cron can run it every
morning without setup. It queries X via `opencli twitter search`, writes a
self-contained `index.html`, archives the same file by date, and leaves git
publish to the caller.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import quote_plus

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo(os.environ.get("BRIEFING_TZ", "America/Los_Angeles"))
NOW = datetime.now(TZ)
SINCE = (NOW.date() - timedelta(days=1)).isoformat()
TODAY = NOW.strftime("%B %-d, %Y") if sys.platform != "win32" else NOW.strftime("%B %#d, %Y")
STAMP = NOW.strftime("%Y-%m-%d")

SECTIONS = [
    {
        "key": "finance",
        "title": "Finance",
        "keywords": ["finance", "market", "stock", "invest", "wall street", "crypto", "bitcoin", "fed", "rate", "yield", "nasdaq", "s&p"],
        "why": "Markets move on attention before they move on consensus. These posts show what finance people are reacting to before it becomes tomorrow's talking point.",
    },
    {
        "key": "business",
        "title": "Business",
        "keywords": ["business", "startup", "ceo", "company", "earnings", "retail", "saas", "founder", "acquisition", "IPO", "revenue"],
        "why": "Business virality is often an early signal for customer sentiment, founder narratives, and brand moments worth tracking.",
    },
    {
        "key": "entertainment",
        "title": "Entertainment",
        "keywords": ["movie", "music", "celebrity", "hollywood", "netflix", "streaming", "box office", "trailer", "actor", "album", "taylor", "film"],
        "why": "Entertainment trends travel fast because they blend fandom, identity, and shareable moments — useful for reading the broader culture.",
    },
    {
        "key": "sports",
        "title": "Sports",
        "keywords": ["sports", "nba", "nfl", "mlb", "soccer", "football", "ufc", "formula", "tennis", "world cup", "goal", "player", "team"],
        "why": "Sports X is a real-time emotion engine. Big clips and takes can foreshadow mainstream coverage by hours.",
    },
    {
        "key": "viral",
        "title": "Overall Viral",
        "keywords": [],
        "why": "This is the open-topic firehose: the posts with enough velocity to break out beyond a single niche.",
    },
]

BROAD_QUERY = '(finance OR markets OR stocks OR investing OR business OR startup OR CEO OR movie OR music OR celebrity OR Hollywood OR Netflix OR sports OR NBA OR NFL OR MLB OR soccer OR football OR UFC OR viral OR breaking OR "just in" OR meme) lang:en min_faves:500'


def parse_json_array(output: str) -> list[dict]:
    """Extract the first JSON array from OpenCLI output, ignoring update notices."""
    start = output.find("[")
    if start == -1:
        return []
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(output[start:], start=start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(output[start : i + 1])
                    return data if isinstance(data, list) else []
                except Exception:
                    return []
    return []


def search_x(query: str, limit: int = 50, top_n: int = 40) -> list[dict]:
    # One broad search is much more reliable than five back-to-back searches;
    # X often rate-limits SearchTimeline after a small burst. We classify the
    # resulting posts locally into the user's sections.
    full_query = f"{query} since:{SINCE} -filter:replies"
    cmd = [
        "opencli",
        "twitter",
        "search",
        full_query,
        "--product",
        "top",
        "--limit",
        str(limit),
        "--top-by-engagement",
        str(top_n),
        "-f",
        "json",
        "--window",
        "background",
        "--site-session",
        "persistent",
    ]
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=150)
    except Exception as e:
        print(f"WARN search failed for {query}: {e}", file=sys.stderr)
        return []
    if proc.returncode != 0:
        print(f"WARN opencli returned {proc.returncode} for {query}: {proc.stderr[:500]}", file=sys.stderr)
    return normalize(parse_json_array(proc.stdout))


def to_int(value) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    s = str(value).replace(",", "").strip()
    try:
        return int(float(s))
    except Exception:
        return 0


def score(tweet: dict) -> float:
    likes = to_int(tweet.get("likes"))
    views = to_int(tweet.get("views"))
    # OpenCLI search output may not always include retweets/replies. This keeps
    # scoring stable even with partial fields.
    return likes + math.log10(views + 1) * 250


def normalize(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for t in items:
        url = t.get("url") or (f"https://x.com/i/status/{t.get('id')}" if t.get("id") else "")
        if not url or url in seen:
            continue
        seen.add(url)
        text = re.sub(r"\s+", " ", str(t.get("text") or "")).strip()
        out.append(
            {
                "id": str(t.get("id") or ""),
                "author": str(t.get("author") or "unknown"),
                "bio": str(t.get("bio") or ""),
                "text": text,
                "created_at": str(t.get("created_at") or ""),
                "likes": to_int(t.get("likes")),
                "views": to_int(t.get("views")),
                "url": url,
                "has_media": bool(t.get("has_media")),
                "score": score(t),
            }
        )
    return sorted(out, key=lambda x: x["score"], reverse=True)


def trending_fallback() -> list[dict]:
    cmd = ["opencli", "twitter", "trending", "-f", "json", "--window", "background", "--site-session", "persistent"]
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=90)
        trends = parse_json_array(proc.stdout)
    except Exception as e:
        print(f"WARN trending fallback failed: {e}", file=sys.stderr)
        return []
    items = []
    for trend in trends:
        topic = str(trend.get("topic") or "").strip()
        if not topic:
            continue
        cat = str(trend.get("category") or "Trending")
        rank = to_int(trend.get("rank")) or len(items) + 1
        items.append(
            {
                "id": f"trend-{rank}",
                "author": "X Trending",
                "bio": cat,
                "text": f"Trending topic: {topic} — {cat}",
                "created_at": "",
                "likes": max(0, 21 - rank) * 100,
                "views": max(0, 21 - rank) * 10_000,
                "url": f"https://x.com/search?q={quote_plus(topic)}&src=typed_query&f=top",
                "has_media": False,
                "score": max(0, 21 - rank) * 1_000,
            }
        )
    return items


def classify(items: list[dict]) -> dict[str, list[dict]]:
    buckets = {s["key"]: [] for s in SECTIONS}
    buckets["viral"] = sorted(items, key=lambda x: x["score"], reverse=True)[:8]
    for item in items:
        blob = f"{item.get('text','')} {item.get('bio','')} {item.get('author','')}".lower()
        for section in SECTIONS:
            key = section["key"]
            if key == "viral":
                continue
            if any(str(keyword).lower() in blob for keyword in section.get("keywords", [])):
                buckets[key].append(item)
    # If a section is thin, fill with high-signal overall items so the page stays useful.
    for section in SECTIONS:
        key = section["key"]
        if key == "viral":
            continue
        if len(buckets[key]) < 3:
            existing = {x["url"] for x in buckets[key]}
            for item in buckets["viral"]:
                if item["url"] not in existing:
                    buckets[key].append(item)
                    existing.add(item["url"])
                if len(buckets[key]) >= 3:
                    break
        buckets[key] = sorted(buckets[key], key=lambda x: x["score"], reverse=True)[:8]
    return buckets


def compact_num(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def text_summary(items: list[dict], section: str) -> str:
    if not items:
        return "Quiet signal this morning — nothing strong enough came back from X search yet."
    top = items[0]
    cleaned = re.sub(r"https?://\S+", "", top["text"]).strip()
    if len(cleaned) > 160:
        cleaned = cleaned[:157].rstrip() + "…"
    return f"The lead {section.lower()} signal is from @{top['author']}: “{escape(cleaned)}”"


def card(tweet: dict, idx: int) -> str:
    text = escape(tweet["text"] or "")
    author = escape(tweet["author"])
    url = escape(tweet["url"])
    media = '<span class="pill">media</span>' if tweet.get("has_media") else ""
    return f"""
      <article class="post-card">
        <div class="rank">{idx}</div>
        <div class="post-body">
          <div class="post-meta"><strong>@{author}</strong>{media}<span>{compact_num(tweet['likes'])} likes</span><span>{compact_num(tweet['views'])} views</span></div>
          <p>{text}</p>
          <a href="{url}" target="_blank" rel="noopener noreferrer">Open on X →</a>
        </div>
      </article>
    """


def render(sections: list[dict]) -> str:
    total_posts = sum(len(s["items"]) for s in sections)
    top_overall = []
    urls = set()
    for s in sections:
        for item in s["items"]:
            if item["url"] not in urls:
                urls.add(item["url"])
                top_overall.append(item)
    top_overall = sorted(top_overall, key=lambda x: x["score"], reverse=True)[:5]

    nav = "".join(f'<a href="#{s["key"]}">{escape(s["title"])}</a>' for s in sections)
    section_html = []
    for s in sections:
        posts = "".join(card(t, i + 1) for i, t in enumerate(s["items"][:6])) or '<p class="empty">No strong posts found yet. The next run will try again.</p>'
        section_html.append(
            f"""
            <section id="{escape(s['key'])}" class="section {'viral' if s['key'] == 'viral' else ''}">
              <div class="section-head">
                <p class="eyebrow">{escape(s['title'])}</p>
                <h2>{escape(text_summary(s['items'], s['title']))}</h2>
                <p class="why"><strong>Why this is important:</strong> {escape(s['why'])}</p>
              </div>
              <div class="post-grid">{posts}</div>
            </section>
            """
        )

    top_cards = "".join(card(t, i + 1) for i, t in enumerate(top_overall))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Daily X Briefing — {escape(TODAY)}</title>
  <meta name="description" content="Daily viral X briefing for finance, business, entertainment, sports, and overall viral posts." />
  <style>
    :root {{
      --bg:#f6f1e8; --paper:#fffaf1; --ink:#17130f; --muted:#766d60; --line:#e3d8c8;
      --accent:#d66b2d; --accent-2:#243b53; --soft:#ede3d2;
    }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: radial-gradient(circle at top left, #fff8ea, var(--bg) 38%, #efe3d0); color:var(--ink); }}
    a {{ color:var(--accent-2); text-decoration:none; }} a:hover {{ text-decoration:underline; }}
    .wrap {{ width:min(1120px, calc(100% - 34px)); margin:0 auto; }}
    header {{ padding:56px 0 24px; }}
    .hero {{ border:1px solid var(--line); background:linear-gradient(135deg, rgba(255,250,241,.94), rgba(255,250,241,.72)); border-radius:32px; padding:36px; box-shadow:0 24px 80px rgba(77,49,21,.09); }}
    .kicker {{ letter-spacing:.18em; text-transform:uppercase; color:var(--accent); font-size:12px; font-weight:800; }}
    h1 {{ font-family: Georgia, "Times New Roman", serif; font-size:clamp(42px, 7vw, 88px); line-height:.92; letter-spacing:-.06em; margin:18px 0 18px; }}
    .intro {{ max-width:760px; color:#4f473d; font-size:18px; line-height:1.65; }}
    .stats {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:24px; }}
    .stat {{ border:1px solid var(--line); background:rgba(255,255,255,.42); border-radius:999px; padding:10px 14px; color:var(--muted); font-size:14px; }}
    nav {{ display:flex; gap:10px; flex-wrap:wrap; margin:22px 0 18px; }}
    nav a {{ border:1px solid var(--line); background:rgba(255,250,241,.75); border-radius:999px; padding:9px 13px; color:var(--ink); font-size:14px; }}
    .section {{ margin:22px 0; border:1px solid var(--line); border-radius:28px; background:rgba(255,250,241,.74); overflow:hidden; }}
    .section.viral {{ border-color:rgba(214,107,45,.45); box-shadow:0 20px 70px rgba(214,107,45,.10); }}
    .section-head {{ padding:28px 28px 10px; }}
    .eyebrow {{ color:var(--accent); font-size:12px; font-weight:900; text-transform:uppercase; letter-spacing:.2em; margin:0 0 10px; }}
    h2 {{ font-family: Georgia, "Times New Roman", serif; font-size:clamp(25px, 3.5vw, 44px); letter-spacing:-.035em; line-height:1.04; margin:0; }}
    .why {{ color:#5b5146; line-height:1.6; max-width:820px; }}
    .post-grid {{ display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:12px; padding:18px; }}
    .post-card {{ display:grid; grid-template-columns:42px 1fr; gap:14px; min-height:170px; border:1px solid var(--line); border-radius:22px; padding:16px; background:rgba(255,255,255,.55); }}
    .rank {{ width:34px; height:34px; border-radius:50%; background:var(--ink); color:var(--paper); display:grid; place-items:center; font-weight:800; }}
    .post-meta {{ display:flex; align-items:center; flex-wrap:wrap; gap:8px; color:var(--muted); font-size:13px; }}
    .post-meta strong {{ color:var(--ink); }}
    .pill {{ border:1px solid var(--line); border-radius:999px; padding:2px 7px; color:var(--accent); background:#fff7ea; }}
    .post-card p {{ margin:12px 0 14px; line-height:1.5; color:#342d26; }}
    .post-card a {{ font-weight:800; font-size:14px; }}
    .top-strip {{ margin:0 0 24px; border:1px solid var(--line); border-radius:28px; padding:24px; background:#15120f; color:#fff7ea; }}
    .top-strip h2 {{ color:#fff7ea; }}
    .top-strip .why, .top-strip .eyebrow {{ color:#f0b384; }}
    .top-strip .post-card {{ background:#211c17; border-color:#40362d; }}
    .top-strip .post-card p, .top-strip .post-meta, .top-strip .post-meta strong {{ color:#fff7ea; }}
    .top-strip a {{ color:#ffd19a; }}
    .empty {{ padding:10px; color:var(--muted); }}
    footer {{ color:var(--muted); padding:24px 0 54px; font-size:13px; text-align:center; }}
    @media (max-width:800px) {{ .hero {{ padding:26px; border-radius:24px; }} .post-grid {{ grid-template-columns:1fr; }} .post-card {{ grid-template-columns:34px 1fr; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div class="hero">
        <div class="kicker">Daily X Briefing · {escape(TODAY)}</div>
        <h1>The morning signal from the scroll.</h1>
        <p class="intro">A casual, clickable scan of viral X posts across finance, business, entertainment, and sports — plus an open-topic viral section for whatever broke out overnight.</p>
        <div class="stats"><span class="stat">Generated {escape(NOW.strftime('%-I:%M %p %Z') if sys.platform != 'win32' else NOW.strftime('%#I:%M %p %Z'))}</span><span class="stat">{total_posts} posts scanned into the brief</span><span class="stat">Source: Agent-Reach / OpenCLI X</span></div>
      </div>
      <nav>{nav}</nav>
    </header>

    <section class="top-strip">
      <p class="eyebrow">Fastest attention</p>
      <h2>Top viral posts across all sections</h2>
      <p class="why"><strong>Why this is important:</strong> this gives you the five posts most likely to bleed out of X and into broader conversation today.</p>
      <div class="post-grid">{top_cards}</div>
    </section>

    {''.join(section_html)}

    <footer>Updated daily by Hermes Agent. Links open the original X posts.</footer>
  </div>
</body>
</html>
"""


def main() -> int:
    items = search_x(BROAD_QUERY)
    if not items:
        print("Broad X search returned no usable posts; using X trending fallback.")
        items = trending_fallback()
    buckets = classify(items)
    generated = []
    raw = {"all": items}
    for section in SECTIONS:
        section_items = buckets.get(section["key"], [])
        raw[section["key"]] = section_items
        generated.append({**section, "items": section_items})
        print(f"{section['title']}: {len(section_items)} posts")

    data_dir = ROOT / "data"
    archive_dir = ROOT / "archive"
    data_dir.mkdir(exist_ok=True)
    archive_dir.mkdir(exist_ok=True)
    (data_dir / f"{STAMP}.json").write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    html = render(generated)
    (ROOT / "index.html").write_text(html, encoding="utf-8")
    (archive_dir / f"{STAMP}.html").write_text(html, encoding="utf-8")
    print(f"Wrote {ROOT / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
