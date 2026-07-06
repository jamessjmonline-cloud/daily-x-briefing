#!/usr/bin/env python3
"""Daily X briefing generator (V3).

Principles:
- Never fake posts, likes, views, or authors.
- Acquire via agent-reach's active backend (twitter-cli, opencli fallback):
  home feeds first (stable), curated lists if configured, then ONE gentle
  search per topic. Any 429 stops all further searches this run.
- Rolling 48h pool: a rate-limited run reuses still-fresh posts, never blank.
- Seen ledger: an item is never shown as "new" twice. Big movers resurface
  in a "Still climbing" strip with deltas.
- Velocity ranking: likes/hour beats raw totals for "viral right now".
- Claude enrichment (claude-haiku-4-5) clusters posts into stories and writes
  real summaries; rule-based fallback if no API key.

Usage:
  generate_briefing.py [--no-search] [--retry] [--refresh]
    --no-search  skip search calls (feeds/lists/cache only)
    --retry      if searches were rate-limited, wait 25 min and retry once
    --refresh    midday refresh: regenerate the page, don't mark items seen
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import enrich as enrich_mod
import xcli

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config" / "topics.json").read_text())
TZ = ZoneInfo(CONFIG.get("timezone", "America/Los_Angeles"))
NOW = datetime.now(TZ)
STAMP = NOW.strftime("%Y-%m-%d")
TODAY = NOW.strftime("%B %-d, %Y")
SINCE = (NOW.date() - timedelta(days=1)).isoformat()

POOL_PATH = ROOT / "data" / "pool.json"
SEEN_PATH = ROOT / "data" / "seen.json"

POOL_MAX_AGE_H = 48
SEEN_MAX_AGE_D = 7
CANDIDATE_LIMIT = 40


def log(msg: str) -> None:
    print(msg, flush=True)


def compact_num(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def parse_iso(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Acquisition
# ---------------------------------------------------------------------------

def acquire(no_search: bool) -> tuple[list[dict], list[str]]:
    posts: list[dict] = []
    errors: list[str] = []

    for kind in ("for-you", "following"):
        got, err = xcli.feed(kind, n=40)
        posts.extend(got)
        if err:
            errors.append(err)
        log(f"feed {kind}: {len(got)} posts")
        time.sleep(2)

    for topic in CONFIG["topics"]:
        for list_id in topic.get("list_ids", []):
            got, err = xcli.list_tweets(str(list_id), n=30)
            for p in got:
                p["topic_hint"] = topic["key"]
            posts.extend(got)
            if err:
                errors.append(err)
            time.sleep(2)

    if not no_search:
        for topic in CONFIG["topics"]:
            if xcli.search_blocked():
                errors.append(f"search skipped for {topic['key']} onward: rate-limited earlier this run")
                break
            got, err = xcli.search(topic["search"], topic["key"], SINCE, topic["min_likes"], n=15)
            for p in got:
                p["topic_hint"] = topic["key"]
            posts.extend(got)
            if err:
                errors.append(err)
            log(f"search {topic['key']}: {len(got)} posts")
            time.sleep(5)

    return posts, errors


def retry_searches(errors: list[str]) -> list[dict]:
    """Wait out the cooldown (the 429 message says 15-30 min) and retry once."""
    log("searches were rate-limited; waiting 25 min before one retry...")
    time.sleep(25 * 60)
    xcli._search_blocked = False
    posts: list[dict] = []
    for topic in CONFIG["topics"]:
        if xcli.search_blocked():
            break
        got, err = xcli.search(topic["search"], topic["key"], SINCE, topic["min_likes"], n=15)
        for p in got:
            p["topic_hint"] = topic["key"]
        posts.extend(got)
        if err:
            errors.append(f"(retry) {err}")
        time.sleep(5)
    return posts


# ---------------------------------------------------------------------------
# Pool (rolling 48h cache with per-run metric history) and seen ledger
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def seed_pool_from_legacy(pool: dict) -> None:
    """One-time: ingest the old data/*.json snapshots so day one has velocity
    baselines and yesterday's items are already marked seen."""
    for f in sorted((ROOT / "data").glob("20*.json")):
        legacy = load_json(f, {})
        ts = legacy.get("generated_at") or f"{f.stem}T08:00:00-07:00"
        for p in legacy.get("posts", []):
            pid = str(p.get("id") or "")
            if not pid or pid in pool:
                continue
            pool[pid] = {
                "id": pid,
                "author": str(p.get("author") or "unknown"),
                "author_name": str(p.get("author") or "unknown"),
                "text": str(p.get("text") or ""),
                "created_iso": str(p.get("created_at") or ""),
                "likes": int(p.get("likes") or 0),
                "retweets": int(p.get("retweets") or 0),
                "replies": int(p.get("replies") or 0),
                "views": int(p.get("views") or 0),
                "url": p.get("url") or "",
                "has_media": bool(p.get("has_media")),
                "media_thumb": (p.get("media_posters") or [""])[0],
                "lang": "", "is_retweet": False,
                "sources": ["legacy"],
                "first_seen": ts,
                "history": [[ts, int(p.get("likes") or 0), int(p.get("views") or 0)]],
            }


def merge_pool(pool: dict, fresh: list[dict]) -> dict:
    now_iso = NOW.isoformat()
    for p in fresh:
        entry = pool.get(p["id"])
        if entry:
            entry.update({k: p[k] for k in ("likes", "retweets", "replies", "views", "text", "has_media", "media_thumb")})
            entry["sources"] = sorted(set(entry.get("sources", [])) | set(p["sources"]))
            if p.get("topic_hint"):
                entry["topic_hint"] = p["topic_hint"]
            entry.setdefault("history", []).append([now_iso, p["likes"], p["views"]])
        else:
            p["first_seen"] = now_iso
            p["history"] = [[now_iso, p["likes"], p["views"]]]
            pool[p["id"]] = p

    cutoff = NOW - timedelta(hours=POOL_MAX_AGE_H)
    pruned = {}
    for pid, e in pool.items():
        ref = parse_iso(e.get("created_iso") or "") or parse_iso(e.get("first_seen") or "")
        if ref and ref >= cutoff:
            e["history"] = e.get("history", [])[-10:]
            pruned[pid] = e
    return pruned


def compute_velocity(entry: dict) -> float:
    """Likes per hour. Prefer observed delta between pool observations;
    first sighting falls back to likes / post age."""
    hist = entry.get("history", [])
    if len(hist) >= 2:
        t0, likes0, _ = hist[0]
        t1, likes1, _ = hist[-1]
        d0, d1 = parse_iso(t0), parse_iso(t1)
        if d0 and d1 and d1 > d0:
            hours = max((d1 - d0).total_seconds() / 3600, 0.5)
            return max((likes1 - likes0) / hours, 0.0)
    created = parse_iso(entry.get("created_iso") or "")
    if created:
        age_h = max((NOW.astimezone(timezone.utc) - created.astimezone(timezone.utc)).total_seconds() / 3600, 2.0)
        return entry.get("likes", 0) / age_h
    return entry.get("likes", 0) / 24.0


def source_weight(entry: dict) -> float:
    sources = entry.get("sources", [])
    if any(s.startswith("list:") for s in sources):
        return 1.3
    if any(s.startswith("feed:") for s in sources):
        return 1.15
    return 1.0


def is_junk(entry: dict) -> bool:
    likes, views = entry.get("likes", 0), entry.get("views", 0)
    if likes < 50:
        return True
    # Promoted-post signature: huge reach, near-zero engagement.
    if views > 200_000 and likes / views < 0.0005:
        return True
    return False


# ---------------------------------------------------------------------------
# Compose
# ---------------------------------------------------------------------------

def build(pool: dict, seen: dict, errors: list[str]) -> dict:
    entries = [e for e in pool.values() if not is_junk(e)]
    for e in entries:
        e["velocity"] = compute_velocity(e)
        e["score"] = e["velocity"] * source_weight(e)

    new_entries = sorted((e for e in entries if e["id"] not in seen),
                         key=lambda e: e["score"], reverse=True)
    candidates = new_entries[:CANDIDATE_LIMIT]

    topic_keys = [t["key"] for t in CONFIG["topics"]]
    result, note = enrich_mod.enrich(candidates, topic_keys)
    if result is None:
        if note:
            errors.append(note)
        result = enrich_mod.fallback(candidates, CONFIG["topics"])
        result["enriched"] = False
    else:
        result["enriched"] = True

    # Still climbing: previously shown items with a big move since shown.
    climbers = []
    for pid, meta in seen.items():
        e = pool.get(pid)
        if not e or is_junk(e):
            continue
        delta = e.get("likes", 0) - int(meta.get("likes", 0))
        if delta >= 5000 or (meta.get("likes") and delta / max(int(meta["likes"]), 1) >= 0.3):
            climbers.append({"entry": e, "delta": delta})
    climbers.sort(key=lambda c: c["delta"], reverse=True)
    result["climbers"] = climbers[:3]
    return result


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

CSS = """
    :root { --bg:#f7f2e9; --paper:#fffaf2; --ink:#15120e; --muted:#6e6559; --line:#e2d6c5; --accent:#c75f2a; --blue:#1d3d5c; --green:#617a45; --dark:#15120e; }
    * { box-sizing:border-box; }
    body { margin:0; background:linear-gradient(180deg,#fbf6ee,var(--bg)); color:var(--ink); font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    a { color:var(--blue); text-decoration:none; } a:hover { text-decoration:underline; }
    .wrap { width:min(1060px, calc(100% - 32px)); margin:0 auto; }
    header { padding:44px 0 18px; }
    .hero { border:1px solid var(--line); border-radius:28px; padding:30px; background:rgba(255,250,242,.82); box-shadow:0 24px 80px rgba(70,45,20,.08); }
    .kicker,.eyebrow { color:var(--accent); text-transform:uppercase; letter-spacing:.18em; font-weight:850; font-size:12px; }
    h1 { font-family: Georgia, "Times New Roman", serif; font-size:clamp(38px,5.5vw,64px); line-height:.97; letter-spacing:-.05em; margin:12px 0 14px; max-width:850px; }
    .pills, nav { display:flex; flex-wrap:wrap; gap:9px; margin-top:20px; }
    .pill, nav a, .quality { border:1px solid var(--line); border-radius:999px; padding:8px 12px; background:rgba(255,255,255,.48); color:var(--muted); font-size:13px; }
    nav { margin:18px 0 22px; } nav a { color:var(--ink); }
    .panel { border:1px solid var(--line); border-radius:24px; padding:24px; background:rgba(255,250,242,.74); margin:18px 0; }
    .panel.dark { background:var(--dark); color:#fff7ea; border-color:#2b241d; } .panel.dark a { color:#ffd29d; } .panel.dark .eyebrow { color:#f0ad78; }
    h2 { font-family: Georgia, "Times New Roman", serif; font-size:clamp(24px,3.2vw,38px); line-height:1.04; letter-spacing:-.03em; margin:6px 0 10px; }
    .quick ul { padding-left:22px; margin:14px 0 0; } .quick li { margin:9px 0; line-height:1.5; }
    .brief-card { display:grid; grid-template-columns:34px 1fr auto; gap:14px; border:1px solid var(--line); background:rgba(255,255,255,.56); border-radius:20px; padding:16px; margin:12px 0; }
    .panel.dark .brief-card { background:#211c17; border-color:#40362d; }
    .num { width:30px; height:30px; border-radius:50%; background:var(--ink); color:#fffaf2; display:grid; place-items:center; font-weight:800; }
    .panel.dark .num { background:#fff0d8; color:#15120e; }
    .meta { display:flex; flex-wrap:wrap; gap:8px; color:var(--muted); font-size:13px; align-items:center; } .panel.dark .meta { color:#d7c7b4; }
    .meta strong { color:var(--ink); } .panel.dark .meta strong { color:#fff7ea; }
    .velocity { color:var(--green); font-weight:800; } .panel.dark .velocity { color:#a8c67f; }
    h3 { margin:8px 0; font-size:18px; line-height:1.3; letter-spacing:-.015em; }
    .excerpt { color:#3f372f; line-height:1.5; margin:0 0 8px; } .panel.dark .excerpt { color:#f2e8dc; }
    .why { color:#5b5148; line-height:1.5; margin:0 0 10px; } .panel.dark .why { color:#dacabb; }
    .source { font-weight:800; font-size:14px; }
    .related { color:var(--muted); font-size:13px; margin-left:10px; }
    .thumb img { width:128px; height:96px; object-fit:cover; border-radius:14px; border:1px solid rgba(255,255,255,.18); }
    .section { border:1px solid var(--line); border-radius:26px; background:rgba(255,250,242,.74); margin:18px 0; overflow:hidden; }
    .section-head { display:flex; justify-content:space-between; gap:18px; align-items:start; padding:24px 24px 8px; }
    .section-head p { color:var(--muted); line-height:1.55; max-width:720px; margin:0; }
    .stack { padding:8px 18px 18px; }
    .empty { border:1px dashed var(--line); border-radius:18px; color:var(--muted); padding:14px 18px; line-height:1.5; margin:0 18px 18px; }
    .trends { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin-top:12px; }
    .trend { border:1px solid var(--line); border-radius:16px; padding:13px; background:rgba(255,255,255,.46); display:block; }
    .trend span { color:var(--accent); font-weight:850; font-size:12px; } .trend strong { display:block; color:var(--ink); margin:3px 0; } .trend em { color:var(--muted); font-size:12px; font-style:normal; }
    .trend.explained { opacity:.55; }
    .climb { display:flex; gap:12px; align-items:baseline; border-bottom:1px dashed var(--line); padding:10px 0; }
    .climb:last-child { border-bottom:none; }
    .climb .delta { color:var(--green); font-weight:850; white-space:nowrap; }
    .debug { margin-top:14px; color:var(--muted); font-size:13px; }
    footer { text-align:center; color:var(--muted); font-size:13px; padding:28px 0 54px; }
    footer .nav-days { margin-bottom:8px; }
    @media (max-width:820px) { .brief-card { grid-template-columns:30px 1fr; } .thumb { display:none; } .section-head { display:block; } .trends { grid-template-columns:1fr; } }
"""


def story_card(story: dict, pool: dict, idx: int, compact: bool = False) -> str:
    e = pool[story["primary_id"]]
    thumb = ""
    if e.get("media_thumb") and not compact:
        thumb = (f'<a class="thumb" href="{escape(e["url"])}" target="_blank" rel="noopener noreferrer">'
                 f'<img src="{escape(e["media_thumb"])}" alt="Post media" loading="lazy"></a>')
    metrics = [f"{compact_num(e['likes'])} likes"]
    if e.get("views"):
        metrics.append(f"{compact_num(e['views'])} views")
    vel = e.get("velocity", 0)
    vel_html = f'<span class="velocity">▲ {compact_num(int(vel))}/hr</span>' if vel >= 100 else ""
    related = ""
    if story.get("related_ids"):
        links = " · ".join(
            f'<a href="{escape(pool[r]["url"])}" target="_blank" rel="noopener noreferrer">@{escape(pool[r]["author"])}</a>'
            for r in story["related_ids"] if r in pool
        )
        if links:
            related = f'<span class="related">+ related: {links}</span>'
    return f"""
    <article class="brief-card">
      <div class="num">{idx}</div>
      <div>
        <div class="meta"><strong>@{escape(e['author'])}</strong><span>{escape(' · '.join(metrics))}</span>{vel_html}</div>
        <h3>{escape(story['headline'])}</h3>
        <p class="excerpt">{escape(e['text'][:220])}</p>
        <p class="why"><strong>Why it matters:</strong> {escape(story['why'])}</p>
        <a class="source" href="{escape(e['url'])}" target="_blank" rel="noopener noreferrer">Open post →</a>{related}
      </div>
      {thumb}
    </article>"""


def render(result: dict, pool: dict, trends: list[dict], errors: list[str], enriched: bool) -> str:
    stories = result["stories"]
    by_id = {s["primary_id"]: s for s in stories}
    top3_ids = result.get("top3_ids", [])[:3]
    top3 = [by_id[i] for i in top3_ids if i in by_id]

    max_stories = int(CONFIG.get("max_stories", 14))
    used = set(top3_ids)
    budget = max_stories - len(top3)

    sections_html, nav_items = [], []
    for topic in CONFIG["topics"]:
        t_stories = [s for s in stories
                     if s["topic"] == topic["key"] and s["primary_id"] not in used][:3]
        t_stories = t_stories[:max(budget, 0)]
        for s in t_stories:
            used.add(s["primary_id"])
        budget -= len(t_stories)
        nav_items.append(f'<a href="#{topic["key"]}">{escape(topic["title"])}</a>')
        if t_stories:
            cards = "".join(story_card(s, pool, i + 1, compact=True) for i, s in enumerate(t_stories))
            body = f'<div class="stack">{cards}</div>'
        else:
            body = '<div class="empty">Nothing above the bar today — no padded filler.</div>'
        sections_html.append(f"""
      <section id="{topic['key']}" class="section">
        <div class="section-head">
          <div><p class="eyebrow">{escape(topic['title'])}</p><p>{escape(topic['deck'])}</p></div>
          <span class="quality">{'Real posts' if t_stories else 'No strong signal'}</span>
        </div>
        {body}
      </section>""")

    # Viral leftovers fold into a Culture/Viral section if budget remains.
    viral = [s for s in stories if s["topic"] == "viral" and s["primary_id"] not in used][:max(budget, 0)][:3]
    if viral:
        cards = "".join(story_card(s, pool, i + 1, compact=True) for i, s in enumerate(viral))
        nav_items.append('<a href="#viral">Viral</a>')
        sections_html.append(f"""
      <section id="viral" class="section">
        <div class="section-head">
          <div><p class="eyebrow">Viral</p><p>Culture moments that fit no box but everyone will mention.</p></div>
          <span class="quality">Real posts</span>
        </div>
        <div class="stack">{cards}</div>
      </section>""")

    top3_html = "".join(story_card(s, pool, i + 1) for i, s in enumerate(top3)) \
        or '<p class="empty">No new stories cleared the bar this run.</p>'

    quick_html = "".join(f"<li>{escape(b)}</li>" for b in result.get("quick_read", []))

    climbers_html = ""
    if result.get("climbers"):
        rows = ""
        for c in result["climbers"]:
            e = c["entry"]
            rows += (f'<div class="climb"><span class="delta">+{compact_num(c["delta"])} likes</span>'
                     f'<span><a href="{escape(e["url"])}" target="_blank" rel="noopener noreferrer">@{escape(e["author"])}</a> '
                     f'{escape(e["text"][:120])}</span></div>')
        climbers_html = f"""
      <section class="panel">
        <p class="eyebrow">Still climbing ↑</p>
        <h2>Yesterday's stories, still growing</h2>
        {rows}
      </section>"""

    story_blob = " ".join(
        (s["headline"] + " " + pool[s["primary_id"]]["text"]).lower()
        for s in stories if s["primary_id"] in pool
    )
    trend_cards = ""
    for t in trends[:12]:
        explained = t["topic"].lower() in story_blob
        cls = "trend explained" if explained else "trend"
        tag = "covered above" if explained else t["category"]
        url = f"https://x.com/search?q={quote_plus(t['topic'])}&src=typed_query&f=top"
        trend_cards += (f'<a class="{cls}" href="{url}" target="_blank" rel="noopener noreferrer">'
                        f'<span>#{t["rank"]}</span><strong>{escape(t["topic"])}</strong><em>{escape(tag)}</em></a>')
    trends_html = f"""
      <section id="watchlist" class="panel">
        <p class="eyebrow">Trend watchlist</p>
        <h2>Trending — dimmed ones are covered above</h2>
        <div class="trends">{trend_cards or '<p class="empty">No trend data this run.</p>'}</div>
      </section>""" if trends else ""

    if errors:
        items = "".join(f"<li>{escape(e)}</li>" for e in errors[:8])
        error_box = f"<details class='debug'><summary>Collection notes</summary><ul>{items}</ul></details>"
    else:
        error_box = ""
    collection_errors = [e for e in errors if "fallback" not in e]
    status = "Partial collection" if collection_errors else "Full collection"

    # prev-day nav from archive
    prev_link = ""
    days = sorted(p.stem for p in (ROOT / "archive").glob("20*.html") if p.stem < STAMP)
    if days:
        prev_link = f'<div class="nav-days"><a href="archive/{days[-1]}.html">← {days[-1]}</a> · <a href="archive/">all days</a></div>'

    generated_time = NOW.strftime('%-I:%M %p %Z')
    total = len(used)
    mode = "Claude-enriched" if enriched else "Rule-based (no API key)"
    nav = "".join(nav_items) + '<a href="#watchlist">Watchlist</a>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Daily X Briefing — {escape(TODAY)}</title>
  <meta name="description" content="Velocity-ranked daily briefing of real viral X posts." />
  <style>{CSS}</style>
</head>
<body>
  <div class="wrap">
    <header>
      <div class="hero">
        <div class="kicker">Daily X Briefing · {escape(TODAY)}</div>
        <h1>What's worth knowing before the day gets loud.</h1>
        <div class="pills"><span class="pill">Generated {escape(generated_time)}</span><span class="pill">{total} stories</span><span class="pill">{escape(status)}</span><span class="pill">{escape(mode)}</span></div>
      </div>
      <nav>{nav}</nav>
    </header>

    <section class="panel dark">
      <p class="eyebrow">Top 3 today</p>
      <h2>The whole briefing in 30 seconds</h2>
      {top3_html}
    </section>

    <section class="panel quick">
      <p class="eyebrow">Quick read</p>
      <ul>{quick_html}</ul>
      {error_box}
    </section>

    {''.join(sections_html)}
    {climbers_html}
    {trends_html}

    <footer>
      {prev_link}
      Updated daily by Hermes Agent via agent-reach. Real posts only — never padded, never faked.
    </footer>
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-search", action="store_true")
    ap.add_argument("--retry", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "archive").mkdir(exist_ok=True)

    posts, errors = acquire(args.no_search)
    if args.retry and xcli.search_blocked():
        posts.extend(retry_searches(errors))

    trends, trend_err = xcli.trending()
    if trend_err:
        errors.append(trend_err)

    pool = load_json(POOL_PATH, {})
    if not pool:
        seed_pool_from_legacy(pool)
    pool = merge_pool(pool, posts)
    log(f"pool: {len(pool)} posts within {POOL_MAX_AGE_H}h window")

    seen = load_json(SEEN_PATH, {})
    seen_cutoff = (NOW.date() - timedelta(days=SEEN_MAX_AGE_D)).isoformat()
    seen = {k: v for k, v in seen.items() if v.get("date", "") >= seen_cutoff}
    # First run: everything already published in old briefings counts as seen.
    if not seen:
        for pid, e in pool.items():
            if "legacy" in e.get("sources", []):
                seen[pid] = {"date": e.get("first_seen", STAMP)[:10], "likes": e.get("likes", 0)}

    result = build(pool, seen, errors)
    html = render(result, pool, trends, errors, result.get("enriched", False))

    (ROOT / "index.html").write_text(html, encoding="utf-8")
    (ROOT / "archive" / f"{STAMP}.html").write_text(html, encoding="utf-8")

    shown_ids = set(result.get("top3_ids", []))
    for s in result["stories"]:
        shown_ids.add(s["primary_id"])
    snapshot = {
        "generated_at": NOW.isoformat(),
        "stories": result["stories"],
        "top3_ids": result.get("top3_ids", []),
        "quick_read": result.get("quick_read", []),
        "enriched": result.get("enriched", False),
        "trends": trends,
        "errors": errors,
        "posts": [pool[i] for i in shown_ids if i in pool],
    }
    (ROOT / "data" / f"{STAMP}.json").write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")

    if not args.refresh:
        for pid in shown_ids:
            if pid in pool:
                seen[pid] = {"date": STAMP, "likes": pool[pid].get("likes", 0)}
        SEEN_PATH.write_text(json.dumps(seen, indent=2), encoding="utf-8")

    POOL_PATH.write_text(json.dumps(pool, indent=2, ensure_ascii=False), encoding="utf-8")

    log(f"stories: {len(result['stories'])} | top3: {len(result.get('top3_ids', []))} | "
        f"climbers: {len(result.get('climbers', []))} | enriched: {result.get('enriched')}")
    if errors:
        log("collection notes:")
        for e in errors:
            log(f"- {e}")
    log(f"wrote {ROOT / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
