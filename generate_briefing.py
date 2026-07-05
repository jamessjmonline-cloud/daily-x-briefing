#!/usr/bin/env python3
"""Generate a useful morning X briefing and publishable HTML artifact.

V2 principles:
- Never fake posts, likes, views, or authors.
- Keep actual posts separate from trend fallback data.
- If X search is rate-limited, show a clear degraded-data notice.
- Write a concise morning-newsletter page: quick read, sections, why it matters,
  and clickable source links.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo(os.environ.get("BRIEFING_TZ", "America/Los_Angeles"))
NOW = datetime.now(TZ)
SINCE = (NOW.date() - timedelta(days=1)).isoformat()
TODAY = NOW.strftime("%B %-d, %Y") if sys.platform != "win32" else NOW.strftime("%B %#d, %Y")
STAMP = NOW.strftime("%Y-%m-%d")

SECTION_DEFS = [
    {
        "key": "finance",
        "title": "Finance",
        "deck": "Markets, stocks, crypto, macro, and investing chatter.",
        "queries": [
            'stocks OR markets OR investing OR "Wall Street" lang:en min_faves:300',
            'crypto OR bitcoin OR ethereum OR Fed OR "interest rates" lang:en min_faves:300',
        ],
        "keywords": ["stock", "market", "invest", "wall street", "crypto", "bitcoin", "ethereum", "fed", "rate", "yield", "nasdaq", "s&p", "dollar", "$"],
    },
    {
        "key": "business",
        "title": "Business",
        "deck": "Companies, CEOs, startups, deals, earnings, and operator narratives.",
        "queries": [
            'business OR startup OR founder OR CEO OR SaaS lang:en min_faves:300',
            'earnings OR acquisition OR IPO OR revenue OR company lang:en min_faves:300',
        ],
        "keywords": ["business", "startup", "founder", "ceo", "company", "earnings", "retail", "saas", "acquisition", "ipo", "revenue", "customer"],
    },
    {
        "key": "entertainment",
        "title": "Entertainment",
        "deck": "Movies, music, celebrities, streaming, creators, and culture moments.",
        "queries": [
            'movie OR music OR celebrity OR Hollywood OR Netflix lang:en min_faves:800',
            'trailer OR album OR actor OR streaming OR boxoffice lang:en min_faves:800',
        ],
        "keywords": ["movie", "music", "celebrity", "hollywood", "netflix", "streaming", "box office", "boxoffice", "trailer", "actor", "album", "film", "song", "artist"],
    },
    {
        "key": "sports",
        "title": "Sports",
        "deck": "Games, clips, athletes, controversies, and fan conversation.",
        "queries": [
            'sports OR NBA OR NFL OR MLB OR soccer OR football lang:en min_faves:800',
            'UFC OR Formula1 OR tennis OR WorldCup OR "World Cup" lang:en min_faves:800',
        ],
        "keywords": ["sports", "nba", "nfl", "mlb", "soccer", "football", "ufc", "formula", "f1", "tennis", "world cup", "goal", "player", "team", "coach"],
    },
]

VIRAL_QUERIES = [
    'viral OR breaking OR "just in" OR unbelievable OR wow OR announcement lang:en min_faves:2000',
    'meme OR trend OR insane OR wild OR thread lang:en min_faves:2000',
]

STOP_HEADLINE_WORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "from", "this", "that", "just", "new", "more", "about",
    "https", "t", "co", "is", "are", "was", "were", "it", "you", "i", "we", "they", "he", "she", "at", "by", "as", "be",
}


@dataclass
class CollectionResult:
    posts: list[dict]
    errors: list[str]
    trends: list[dict]


def parse_json_array(output: str) -> list[dict]:
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


def compact_num(n: int | None) -> str:
    n = to_int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def score(post: dict) -> float:
    likes = to_int(post.get("likes"))
    views = to_int(post.get("views"))
    retweets = to_int(post.get("retweets"))
    replies = to_int(post.get("replies"))
    return likes + retweets * 3 + replies * 2 + math.log10(views + 1) * 250


def clean_text(text: str) -> str:
    text = re.sub(r"https?://\S+", "", str(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize(items: list[dict], section_key: str | None = None) -> list[dict]:
    out = []
    for t in items:
        url = t.get("url") or (f"https://x.com/i/status/{t.get('id')}" if t.get("id") else "")
        if not url or "/search?" in url:
            continue
        text = clean_text(t.get("text") or "")
        if not text:
            continue
        p = {
            "id": str(t.get("id") or ""),
            "author": str(t.get("author") or "unknown").lstrip("@"),
            "bio": str(t.get("bio") or ""),
            "text": text,
            "created_at": str(t.get("created_at") or ""),
            "likes": to_int(t.get("likes")),
            "retweets": to_int(t.get("retweets")),
            "replies": to_int(t.get("replies")),
            "views": to_int(t.get("views")),
            "url": url,
            "has_media": bool(t.get("has_media")),
            "media_posters": t.get("media_posters") or [],
            "card": t.get("card"),
            "section_hint": section_key,
        }
        p["score"] = score(p)
        p["headline"] = make_headline(p, section_key)
        p["why"] = make_why(p, section_key)
        out.append(p)
    return out


def dedupe(posts: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique = []
    for p in sorted(posts, key=lambda x: x.get("score", 0), reverse=True):
        key = p.get("id") or p.get("url")
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def run_opencli(args: list[str], timeout: int = 150) -> tuple[int, str, str]:
    cmd = ["opencli", "twitter", *args, "--window", "background", "--site-session", "persistent"]
    proc = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def search_x(query: str, section_key: str | None, limit: int = 10, top_n: int = 6) -> tuple[list[dict], str | None]:
    full_query = f"({query}) since:{SINCE} -filter:replies"
    args = ["search", full_query, "--product", "top", "--limit", str(limit), "--top-by-engagement", str(top_n), "-f", "json"]
    try:
        code, stdout, stderr = run_opencli(args)
    except Exception as e:
        return [], f"{section_key or 'viral'} search failed: {e}"
    items = normalize(parse_json_array(stdout), section_key)
    if code != 0:
        msg = stderr.strip() or stdout.strip()
        msg = re.sub(r"\s+", " ", msg)[:240]
        return items, f"{section_key or 'viral'} search degraded: {msg or 'OpenCLI returned non-zero'}"
    return items, None


def get_timeline() -> tuple[list[dict], str | None]:
    """Fetch the home For You timeline as a real-post fallback.

    This does not replace targeted search, but it often keeps the morning brief
    useful when SearchTimeline is rate-limited.
    """
    try:
        code, stdout, stderr = run_opencli(["timeline", "--type", "for-you", "--limit", "30", "--top-by-engagement", "14", "-f", "json"], timeout=180)
    except Exception as e:
        return [], f"timeline fallback failed: {e}"
    posts = normalize(parse_json_array(stdout), "viral")
    if code != 0:
        msg = stderr.strip() or stdout.strip()
        msg = re.sub(r"\s+", " ", msg)[:240]
        return posts, f"timeline fallback degraded: {msg or 'OpenCLI returned non-zero'}"
    return posts, None


def get_trends() -> list[dict]:
    try:
        code, stdout, stderr = run_opencli(["trending", "-f", "json"], timeout=90)
    except Exception:
        return []
    trends = parse_json_array(stdout)
    cleaned = []
    for trend in trends[:20]:
        topic = str(trend.get("topic") or "").strip()
        if not topic:
            continue
        cleaned.append(
            {
                "rank": to_int(trend.get("rank")) or len(cleaned) + 1,
                "topic": topic,
                "category": str(trend.get("category") or "Trending"),
                "url": f"https://x.com/search?q={quote_plus(topic)}&src=typed_query&f=top",
            }
        )
    return cleaned


def collect() -> CollectionResult:
    all_posts: list[dict] = []
    errors: list[str] = []

    # Keep this conservative to reduce rate-limit risk: one viral query plus one
    # query per section. The second query is only attempted if a section is thin.
    for query in VIRAL_QUERIES[:1]:
        posts, err = search_x(query, "viral", limit=12, top_n=8)
        all_posts.extend(posts)
        if err:
            errors.append(err)
        time.sleep(4)

    for section in SECTION_DEFS:
        section_posts: list[dict] = []
        for i, query in enumerate(section["queries"]):
            if i > 0 and len(section_posts) >= 3:
                break
            posts, err = search_x(query, section["key"], limit=10, top_n=6)
            section_posts.extend(posts)
            all_posts.extend(posts)
            if err:
                errors.append(err)
                # Avoid hammering X after a rate-limit/failed fetch.
                if "429" in err or "rate" in err.lower() or "Failed to fetch" in err:
                    break
            time.sleep(4)

    # Timeline is a real-post safety net. Use it when targeted searches are
    # thin/degraded so the page still has actual posts instead of fake trend cards.
    if len(dedupe(all_posts)) < 8 or errors:
        timeline_posts, timeline_err = get_timeline()
        if timeline_posts:
            all_posts.extend(timeline_posts)
        if timeline_err:
            errors.append(timeline_err)

    return CollectionResult(posts=dedupe(all_posts), errors=errors, trends=get_trends())


def keyword_match(post: dict, keywords: list[str]) -> bool:
    blob = f"{post.get('text','')} {post.get('bio','')} {post.get('author','')}".lower()
    return any(k.lower() in blob for k in keywords)


def section_posts(posts: list[dict], section: dict) -> list[dict]:
    direct = [p for p in posts if p.get("section_hint") == section["key"]]
    matched = [p for p in posts if p not in direct and keyword_match(p, section["keywords"])]
    sectioned = []
    for p in dedupe(direct + matched)[:4]:
        q = dict(p)
        q["headline"] = make_headline(q, section["key"])
        q["why"] = make_why(q, section["key"])
        sectioned.append(q)
    return sectioned


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[A-Za-z][A-Za-z0-9$&.+'-]{2,}", text) if w.lower() not in STOP_HEADLINE_WORDS]


def make_headline(post: dict, section_key: str | None) -> str:
    text = clean_text(post.get("text", ""))
    author = post.get("author", "unknown")
    if len(text) <= 82:
        base = text
    else:
        # Prefer the first sentence/phrase; it usually preserves the actual news hook.
        first = re.split(r"(?<=[.!?])\s+", text)[0]
        base = first if 25 <= len(first) <= 110 else text[:96].rstrip() + "…"
    prefix = {
        "finance": "Markets watch",
        "business": "Business X is talking about",
        "entertainment": "Culture watch",
        "sports": "Sports X is reacting to",
        "viral": "Viral breakout",
        None: "Viral breakout",
    }.get(section_key, "Signal")
    # Avoid duplicating if the post text is already punchy.
    return f"{prefix}: {base}"


def make_why(post: dict, section_key: str | None) -> str:
    likes = compact_num(post.get("likes"))
    views = compact_num(post.get("views"))
    has_media = post.get("has_media")
    section_note = {
        "finance": "It can signal what market participants are emotionally pricing in before formal analysis catches up.",
        "business": "It highlights the company, founder, or customer narrative likely to spill into operator conversations today.",
        "entertainment": "Culture posts like this move fast because fandom turns them into repeatable conversation hooks.",
        "sports": "Sports clips and takes often become the day’s mainstream debate once fan emotion compounds.",
        "viral": "It has enough social velocity to potentially jump from X into broader conversation.",
        None: "It has enough social velocity to potentially jump from X into broader conversation.",
    }.get(section_key, "It is getting attention fast enough to be worth checking early.")
    media_note = " The post includes media, so it is more likely to travel beyond the original audience." if has_media else ""
    metric_note = []
    if to_int(post.get("likes")):
        metric_note.append(f"{likes} likes")
    if to_int(post.get("views")):
        metric_note.append(f"{views} views")
    metrics = f" Current signal: {', '.join(metric_note)}." if metric_note else ""
    return f"{section_note}{media_note}{metrics}"


def briefing_bullets(sections: list[dict], viral: list[dict], result: CollectionResult) -> list[str]:
    bullets = []
    real_count = len(result.posts)
    if real_count:
        top = viral[0] if viral else result.posts[0]
        bullets.append(f"Top viral post: @{top['author']} is driving the strongest engagement with “{trim(top['text'], 115)}”.")
    else:
        bullets.append("X post search did not return real posts this run; use the trend watchlist as a directional fallback only.")
    for section in sections:
        items = section["items"]
        if items:
            bullets.append(f"{section['title']}: {trim(items[0]['headline'], 130)}")
        else:
            bullets.append(f"{section['title']}: no strong real-post signal found in the latest collection window.")
    if result.errors:
        bullets.append("Data quality note: one or more X searches were rate-limited or degraded, so empty sections are intentional rather than padded.")
    return bullets[:6]


def trim(text: str, n: int) -> str:
    text = clean_text(text)
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def status_label(result: CollectionResult) -> tuple[str, str]:
    if result.posts and not result.errors:
        return "Full post search", "All sections were populated from real X post search results."
    if result.posts and result.errors:
        return "Partial post search", "Some real posts were collected, but at least one X search was rate-limited or degraded."
    return "Trend fallback only", "X post search did not return real posts, so this page is showing trend watchlist context only."


def post_card(post: dict, idx: int, compact: bool = False) -> str:
    poster = ""
    media_posters = post.get("media_posters") or []
    if media_posters and not compact:
        poster = f'<a class="thumb" href="{escape(post["url"])}" target="_blank" rel="noopener noreferrer"><img src="{escape(str(media_posters[0]))}" alt="Post media preview" loading="lazy"></a>'
    metrics = []
    if to_int(post.get("likes")):
        metrics.append(f"{compact_num(post.get('likes'))} likes")
    if to_int(post.get("views")):
        metrics.append(f"{compact_num(post.get('views'))} views")
    if to_int(post.get("retweets")):
        metrics.append(f"{compact_num(post.get('retweets'))} reposts")
    metric_html = " · ".join(metrics) if metrics else "engagement unavailable"
    return f"""
    <article class="brief-card">
      <div class="num">{idx}</div>
      <div class="brief-copy">
        <div class="meta"><strong>@{escape(post['author'])}</strong><span>{escape(metric_html)}</span>{'<span>media</span>' if post.get('has_media') else ''}</div>
        <h3>{escape(post['headline'])}</h3>
        <p class="excerpt">{escape(trim(post['text'], 220))}</p>
        <p class="why"><strong>Why it matters:</strong> {escape(post['why'])}</p>
        <a class="source" href="{escape(post['url'])}" target="_blank" rel="noopener noreferrer">Open original post →</a>
      </div>
      {poster}
    </article>
    """


def trend_card(trend: dict) -> str:
    return f"""
    <a class="trend" href="{escape(trend['url'])}" target="_blank" rel="noopener noreferrer">
      <span>#{trend['rank']}</span>
      <strong>{escape(trend['topic'])}</strong>
      <em>{escape(trend['category'])}</em>
    </a>
    """


def render(result: CollectionResult) -> str:
    viral = [p for p in result.posts if p.get("section_hint") == "viral"]
    if len(viral) < 5:
        viral = dedupe(viral + result.posts)[:6]
    else:
        viral = dedupe(viral)[:6]

    sections = []
    for s in SECTION_DEFS:
        sections.append({**s, "items": section_posts(result.posts, s)})

    total_real = len(result.posts)
    status, status_desc = status_label(result)
    bullets = briefing_bullets(sections, viral, result)
    nav = "".join(f'<a href="#{s["key"]}">{escape(s["title"])}</a>' for s in sections)
    nav += '<a href="#viral">Overall Viral</a><a href="#watchlist">Trend Watchlist</a>'

    viral_html = "".join(post_card(p, i + 1) for i, p in enumerate(viral[:5])) or '<p class="empty">No real viral posts were collected this run.</p>'

    section_html = []
    for s in sections:
        quality = "Real posts" if s["items"] else "No strong signal"
        cards = "".join(post_card(p, i + 1, compact=True) for i, p in enumerate(s["items"][:4]))
        if not cards:
            cards = '<div class="empty"><strong>No padded filler.</strong><br>X did not return a clean real-post signal for this section. Check the trend watchlist below, or wait for the next run.</div>'
        lead = f"{s['title']}: {trim(s['items'][0]['headline'], 120)}" if s["items"] else f"{s['title']}: no reliable signal yet"
        section_html.append(
            f"""
            <section id="{escape(s['key'])}" class="section">
              <div class="section-head">
                <div><p class="eyebrow">{escape(s['title'])}</p><h2>{escape(lead)}</h2><p>{escape(s['deck'])}</p></div>
                <span class="quality">{quality}</span>
              </div>
              <div class="stack">{cards}</div>
            </section>
            """
        )

    trends = "".join(trend_card(t) for t in result.trends[:12]) or '<p class="empty">No trend data available.</p>'
    errors = "".join(f"<li>{escape(e)}</li>" for e in result.errors[:6])
    error_box = f"<details class='debug'><summary>Collection notes</summary><ul>{errors}</ul></details>" if errors else ""

    bullet_html = "".join(f"<li>{escape(b)}</li>" for b in bullets)
    generated_time = NOW.strftime('%-I:%M %p %Z') if sys.platform != 'win32' else NOW.strftime('%#I:%M %p %Z')

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Morning X Briefing — {escape(TODAY)}</title>
  <meta name="description" content="Useful morning briefing from viral X posts and trend watchlist." />
  <style>
    :root {{ --bg:#f7f2e9; --paper:#fffaf2; --ink:#15120e; --muted:#6e6559; --line:#e2d6c5; --accent:#c75f2a; --blue:#1d3d5c; --green:#617a45; --dark:#15120e; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:linear-gradient(180deg,#fbf6ee,var(--bg)); color:var(--ink); font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    a {{ color:var(--blue); text-decoration:none; }} a:hover {{ text-decoration:underline; }}
    .wrap {{ width:min(1060px, calc(100% - 32px)); margin:0 auto; }}
    header {{ padding:44px 0 18px; }}
    .hero {{ border:1px solid var(--line); border-radius:28px; padding:30px; background:rgba(255,250,242,.82); box-shadow:0 24px 80px rgba(70,45,20,.08); }}
    .kicker,.eyebrow {{ color:var(--accent); text-transform:uppercase; letter-spacing:.18em; font-weight:850; font-size:12px; }}
    h1 {{ font-family: Georgia, "Times New Roman", serif; font-size:clamp(42px,6vw,72px); line-height:.95; letter-spacing:-.055em; margin:12px 0 14px; max-width:850px; }}
    .intro {{ color:#50483f; line-height:1.65; max-width:760px; font-size:17px; }}
    .pills, nav {{ display:flex; flex-wrap:wrap; gap:9px; margin-top:20px; }}
    .pill, nav a, .quality {{ border:1px solid var(--line); border-radius:999px; padding:8px 12px; background:rgba(255,255,255,.48); color:var(--muted); font-size:13px; }}
    nav {{ margin:18px 0 22px; }} nav a {{ color:var(--ink); }}
    .grid-top {{ display:grid; grid-template-columns:1.1fr .9fr; gap:16px; margin:20px 0; }}
    .panel {{ border:1px solid var(--line); border-radius:24px; padding:24px; background:rgba(255,250,242,.74); }}
    .panel.dark {{ background:var(--dark); color:#fff7ea; border-color:#2b241d; }} .panel.dark a {{ color:#ffd29d; }} .panel.dark .eyebrow {{ color:#f0ad78; }}
    h2 {{ font-family: Georgia, "Times New Roman", serif; font-size:clamp(26px,3.5vw,42px); line-height:1.04; letter-spacing:-.035em; margin:6px 0 10px; }}
    .quick ul {{ padding-left:22px; margin:14px 0 0; }} .quick li {{ margin:10px 0; line-height:1.5; }}
    .brief-card {{ display:grid; grid-template-columns:34px 1fr auto; gap:14px; border:1px solid var(--line); background:rgba(255,255,255,.56); border-radius:20px; padding:16px; margin:12px 0; }}
    .panel.dark .brief-card {{ background:#211c17; border-color:#40362d; }}
    .num {{ width:30px; height:30px; border-radius:50%; background:var(--ink); color:#fffaf2; display:grid; place-items:center; font-weight:800; }}
    .panel.dark .num {{ background:#fff0d8; color:#15120e; }}
    .meta {{ display:flex; flex-wrap:wrap; gap:8px; color:var(--muted); font-size:13px; align-items:center; }} .panel.dark .meta {{ color:#d7c7b4; }}
    .meta strong {{ color:var(--ink); }} .panel.dark .meta strong {{ color:#fff7ea; }}
    h3 {{ margin:8px 0; font-size:18px; line-height:1.25; letter-spacing:-.015em; }}
    .excerpt {{ color:#3f372f; line-height:1.5; margin:0 0 8px; }} .panel.dark .excerpt {{ color:#f2e8dc; }}
    .why {{ color:#5b5148; line-height:1.5; margin:0 0 10px; }} .panel.dark .why {{ color:#dacabb; }}
    .source {{ font-weight:800; font-size:14px; }}
    .thumb img {{ width:128px; height:96px; object-fit:cover; border-radius:14px; border:1px solid rgba(255,255,255,.18); }}
    .section {{ border:1px solid var(--line); border-radius:26px; background:rgba(255,250,242,.74); margin:18px 0; overflow:hidden; }}
    .section-head {{ display:flex; justify-content:space-between; gap:18px; align-items:start; padding:24px 24px 8px; }}
    .section-head p {{ color:var(--muted); line-height:1.55; max-width:720px; margin:0; }}
    .stack {{ padding:8px 18px 18px; }}
    .empty {{ border:1px dashed var(--line); border-radius:18px; color:var(--muted); padding:18px; line-height:1.55; }}
    .trends {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; }}
    .trend {{ border:1px solid var(--line); border-radius:16px; padding:13px; background:rgba(255,255,255,.46); display:block; }}
    .trend span {{ color:var(--accent); font-weight:850; font-size:12px; }} .trend strong {{ display:block; color:var(--ink); margin:3px 0; }} .trend em {{ color:var(--muted); font-size:12px; font-style:normal; }}
    .debug {{ margin-top:14px; color:var(--muted); font-size:13px; }}
    footer {{ text-align:center; color:var(--muted); font-size:13px; padding:28px 0 54px; }}
    @media (max-width:820px) {{ .grid-top {{ grid-template-columns:1fr; }} .brief-card {{ grid-template-columns:30px 1fr; }} .thumb {{ display:none; }} .section-head {{ display:block; }} .trends {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div class="hero">
        <div class="kicker">Morning X Briefing · {escape(TODAY)}</div>
        <h1>What’s worth knowing before the day gets loud.</h1>
        <p class="intro">A clean morning scan of real viral X posts across finance, business, entertainment, and sports. When post search is degraded, the page says so instead of padding sections with fake signals.</p>
        <div class="pills"><span class="pill">Generated {escape(generated_time)}</span><span class="pill">{total_real} real posts collected</span><span class="pill">Status: {escape(status)}</span><span class="pill">Source: Agent-Reach / OpenCLI X</span></div>
      </div>
      <nav>{nav}</nav>
    </header>

    <div class="grid-top">
      <section class="panel quick">
        <p class="eyebrow">5-minute read</p>
        <h2>Today’s quick read</h2>
        <p>{escape(status_desc)}</p>
        <ul>{bullet_html}</ul>
        {error_box}
      </section>
      <section class="panel" id="watchlist">
        <p class="eyebrow">Trend watchlist</p>
        <h2>Topics to check manually</h2>
        <p class="intro">These are X trending topics, not posts. Use them as leads when search is rate-limited.</p>
        <div class="trends">{trends}</div>
      </section>
    </div>

    <section id="viral" class="panel dark">
      <p class="eyebrow">Overall viral</p>
      <h2>The posts with the most breakout potential</h2>
      <p class="why">Open-topic signals first — useful for spotting stories that may cross from X into broader conversation.</p>
      {viral_html}
    </section>

    {''.join(section_html)}
    <footer>Updated daily by Hermes Agent. Real posts and trends are clearly separated.</footer>
  </div>
</body>
</html>
"""


def main() -> int:
    result = collect()
    generated_sections = []
    for s in SECTION_DEFS:
        generated_sections.append({**s, "items": section_posts(result.posts, s)})
        print(f"{s['title']}: {len(generated_sections[-1]['items'])} real posts")
    print(f"Overall real posts: {len(result.posts)}")
    if result.errors:
        print("Collection degraded:")
        for e in result.errors:
            print(f"- {e}")

    data_dir = ROOT / "data"
    archive_dir = ROOT / "archive"
    data_dir.mkdir(exist_ok=True)
    archive_dir.mkdir(exist_ok=True)
    payload = {
        "generated_at": NOW.isoformat(),
        "since": SINCE,
        "posts": result.posts,
        "trends": result.trends,
        "errors": result.errors,
        "sections": generated_sections,
    }
    (data_dir / f"{STAMP}.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    html = render(result)
    (ROOT / "index.html").write_text(html, encoding="utf-8")
    (archive_dir / f"{STAMP}.html").write_text(html, encoding="utf-8")
    print(f"Wrote {ROOT / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
