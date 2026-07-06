#!/usr/bin/env python3
"""Daily X briefing generator — "The X Wire" edition.

Run it on demand:
  .venv/bin/python generate_briefing.py            normal run
  .venv/bin/python generate_briefing.py --no-search  skip searches (feeds+cache only)
  .venv/bin/python generate_briefing.py --refresh    update page without marking items "seen"

How it works (see HOW-TO-EDIT.md for the plain-English guide):
- Pulls real posts via agent-reach's active backend (twitter-cli; opencli fallback).
- Home feeds first (stable), then ONE gentle search per topic; any 429 stops
  further searches for the run.
- 48h rolling pool (data/pool.json) so a rate-limited run is never blank.
- Seen ledger (data/seen.json): nothing is shown as "new" twice; big movers
  resurface under "Still climbing".
- Velocity ranking: likes/hour beats raw totals for "viral right now".
- Claude enrichment (enrich.py) clusters posts into stories and writes real
  headlines/summaries; falls back to templates if no ANTHROPIC_API_KEY.
- Never fakes posts, likes, views, or authors.
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
TODAY = NOW.strftime("%A, %B %-d, %Y")
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

def get_candidates(pool: dict, seen: dict) -> list[dict]:
    entries = [e for e in pool.values() if not is_junk(e)]
    for e in entries:
        e["velocity"] = compute_velocity(e)
        e["score"] = e["velocity"] * source_weight(e)
    new_entries = sorted((e for e in entries if e["id"] not in seen),
                         key=lambda e: e["score"], reverse=True)
    return new_entries[:CANDIDATE_LIMIT]


def load_editorial(candidates: list[dict]) -> dict | None:
    """data/editorial.json: story judgments written by an editor (usually
    Claude in-session, when James asks for a briefing). Same schema as the
    enrichment output. Only honored if written for today."""
    ed = load_json(ROOT / "data" / "editorial.json", None)
    if not ed or ed.get("for_date") != STAMP:
        return None
    known = {p["id"] for p in candidates}
    stories = [s for s in ed.get("stories", [])
               if not s.get("drop") and s.get("primary_id") in known]
    for s in stories:
        s["related_ids"] = [r for r in s.get("related_ids", []) if r in known and r != s["primary_id"]]
    if not stories:
        return None
    primary = {s["primary_id"] for s in stories}
    return {
        "stories": stories,
        "top3_ids": [i for i in ed.get("top3_ids", []) if i in primary][:3],
        "quick_read": ed.get("quick_read", []),
    }


def build(pool: dict, seen: dict, errors: list[str]) -> dict:
    candidates = get_candidates(pool, seen)

    result = load_editorial(candidates)
    if result is not None:
        result["enriched"] = True
    else:
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
# Render — "The X Wire": stark newsroom wire. Black on white, red accents,
# hairline rules, zero decoration. Colors/type live in the CSS block below.
# ---------------------------------------------------------------------------

CSS = """
    :root { --ink:#111214; --body:#3c3d42; --faint:#6f7075; --rule:#e4e4e6; --red:#c8102e; }
    * { box-sizing:border-box; }
    body { margin:0; background:#ffffff; color:var(--ink);
      font-family:"Helvetica Neue",Helvetica,Arial,sans-serif; }
    a { color:var(--red); text-decoration:none; font-weight:650; }
    a:hover { text-decoration:underline; }
    .wrap { max-width:700px; margin:0 auto; padding:44px 22px 70px; }
    .masthead { border-bottom:3px solid var(--ink); padding-bottom:10px;
      display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:6px; }
    .masthead h1 { margin:0; font-size:15px; font-weight:800; letter-spacing:.26em; text-transform:uppercase; }
    .masthead time { font-size:12px; color:var(--faint); letter-spacing:.08em; }
    .runinfo { margin:10px 0 0; font-size:12px; color:var(--faint); letter-spacing:.04em; }
    .item { padding:22px 0; border-bottom:1px solid var(--rule); }
    .item .k { font-size:11px; font-weight:800; letter-spacing:.2em; color:var(--red); text-transform:uppercase; }
    .item h2 { margin:6px 0; font-size:24px; line-height:1.2; font-weight:750; letter-spacing:-.02em; text-wrap:balance; }
    .lede h2 { font-size:34px; }
    .item p { margin:0 0 4px; font-size:15px; line-height:1.6; color:var(--body); max-width:62ch; }
    .item .quote { color:var(--faint); font-size:14px; }
    .item .src { margin-top:8px; font-size:13px; color:var(--faint); font-variant-numeric:tabular-nums; }
    .item .src b { color:var(--ink); font-weight:650; }
    .kicker { margin:34px 0 4px; font-size:12px; font-weight:800; letter-spacing:.22em;
      text-transform:uppercase; border-bottom:3px solid var(--ink); padding-bottom:8px; }
    .brief-row { display:flex; gap:10px; padding:12px 0; border-bottom:1px solid var(--rule);
      font-size:15px; line-height:1.5; }
    .brief-row .t { color:var(--red); font-weight:800; white-space:nowrap; font-variant-numeric:tabular-nums; }
    .empty { padding:12px 0; border-bottom:1px solid var(--rule); color:var(--faint); font-size:14px; }
    details.notes { margin-top:30px; font-size:12px; color:var(--faint); }
    details.notes li { margin:4px 0; }
    footer { margin-top:44px; padding-top:12px; border-top:3px solid var(--ink);
      font-size:12px; color:var(--faint); display:flex; justify-content:space-between; flex-wrap:wrap; gap:6px; }
"""


def vel_label(e: dict) -> str:
    v = int(e.get("velocity", 0))
    return f" · ▲ {compact_num(v)} likes/hr" if v >= 100 else ""


def wire_item(story: dict, pool: dict, topic_title: str, lede: bool = False) -> str:
    e = pool[story["primary_id"]]
    metrics = f"{compact_num(e['likes'])} likes"
    if e.get("views"):
        metrics += f" · {compact_num(e['views'])} views"
    related = ""
    rel_links = [f'<a href="{escape(pool[r]["url"])}" target="_blank" rel="noopener noreferrer">@{escape(pool[r]["author"])}</a>'
                 for r in story.get("related_ids", []) if r in pool]
    if rel_links:
        related = f" · also: {' · '.join(rel_links)}"
    quote = ""
    if e["text"] and e["text"][:60].lower() not in story["headline"].lower():
        quote = f'<p class="quote">&ldquo;{escape(e["text"][:180])}&rdquo;</p>'
    return f"""
    <div class="item{' lede' if lede else ''}">
      <span class="k">{escape(topic_title)}{escape(vel_label(e))}</span>
      <h2>{escape(story['headline'])}</h2>
      {quote}
      <p>{escape(story['why'])}</p>
      <div class="src">Source: <b>@{escape(e['author'])}</b> · {escape(metrics)} ·
        <a href="{escape(e['url'])}" target="_blank" rel="noopener noreferrer">open post</a>{related}</div>
    </div>"""


def render(result: dict, pool: dict, trends: list[dict], errors: list[str], enriched: bool) -> str:
    stories = result["stories"]
    by_id = {s["primary_id"]: s for s in stories}
    topic_titles = {t["key"]: t["title"] for t in CONFIG["topics"]}
    topic_titles["viral"] = "Viral"

    top3_ids = result.get("top3_ids", [])[:3]
    top3 = [by_id[i] for i in top3_ids if i in by_id]
    used = set(s["primary_id"] for s in top3)

    max_stories = int(CONFIG.get("max_stories", 14))
    budget = max_stories - len(top3)

    top_html = ""
    for i, s in enumerate(top3):
        title = "Top story" if i == 0 else topic_titles.get(s["topic"], s["topic"])
        top_html += wire_item(s, pool, title, lede=(i == 0))
    if not top_html:
        top_html = '<div class="empty">No new stories cleared the bar this run.</div>'

    sections_html = ""
    for topic in CONFIG["topics"] + [{"key": "viral", "title": "Viral"}]:
        t_stories = [s for s in stories
                     if s["topic"] == topic["key"] and s["primary_id"] not in used][:3]
        t_stories = t_stories[:max(budget, 0)]
        for s in t_stories:
            used.add(s["primary_id"])
        budget -= len(t_stories)
        if not t_stories:
            continue
        items = "".join(wire_item(s, pool, topic["title"]) for s in t_stories)
        sections_html += f'\n    <div class="kicker">{escape(topic["title"])}</div>{items}'

    brief_rows = ""
    for c in result.get("climbers", []):
        e = c["entry"]
        brief_rows += (f'<div class="brief-row"><span class="t">+{compact_num(c["delta"])}</span>'
                       f'<span>Still climbing since shown: {escape(e["text"][:110])} — '
                       f'<a href="{escape(e["url"])}" target="_blank" rel="noopener noreferrer">@{escape(e["author"])}</a></span></div>')
    story_blob = " ".join(
        (s["headline"] + " " + pool[s["primary_id"]]["text"]).lower()
        for s in stories if s["primary_id"] in pool)
    unexplained = [t for t in trends[:12] if t["topic"].lower() not in story_blob][:5]
    if unexplained:
        links = " · ".join(
            f'<a href="https://x.com/search?q={quote_plus(t["topic"])}&src=typed_query&f=top" '
            f'target="_blank" rel="noopener noreferrer">{escape(t["topic"])}</a>' for t in unexplained)
        brief_rows += (f'<div class="brief-row"><span class="t">TREND</span>'
                       f'<span>Rising, no post captured — worth a look: {links}</span></div>')
    brief_html = f'\n    <div class="kicker">In brief</div>{brief_rows}' if brief_rows else ""

    notes_html = ""
    if errors:
        items = "".join(f"<li>{escape(e)}</li>" for e in errors[:8])
        notes_html = f'<details class="notes"><summary>Collection notes</summary><ul>{items}</ul></details>'

    prev_link = ""
    days = sorted(p.stem for p in (ROOT / "archive").glob("20*.html") if p.stem < STAMP)
    if days:
        prev_link = f'<a href="archive/{days[-1]}.html">← {days[-1]}</a>'

    collection_errors = [e for e in errors if "fallback" not in e]
    status = "partial collection" if collection_errors else "full collection"
    mode = "Claude-edited" if enriched else "auto-templated"
    generated_time = NOW.strftime('%-I:%M %p %Z')

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>The X Wire — {escape(TODAY)}</title>
  <meta name="description" content="A stark daily wire of real viral X posts, velocity-ranked." />
  <style>{CSS}</style>
</head>
<body>
  <div class="wrap">
    <div class="masthead"><h1>The X Wire</h1><time>{escape(TODAY)} · {escape(generated_time)}</time></div>
    <p class="runinfo">{len(used)} stories · {escape(status)} · {escape(mode)} · real posts only, never padded</p>

    {top_html}
    {sections_html}
    {brief_html}
    {notes_html}

    <footer>
      <span>{prev_link}</span>
      <span>Generated on demand via agent-reach</span>
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
    ap.add_argument("--collect", action="store_true",
                    help="fetch + dump candidates to data/candidates.json, then stop (editor step follows)")
    ap.add_argument("--render-only", action="store_true",
                    help="skip fetching; render from pool + data/editorial.json")
    args = ap.parse_args()

    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "archive").mkdir(exist_ok=True)

    if args.render_only:
        posts, errors = [], []
        saved = load_json(ROOT / "data" / "candidates.json", {})
        trends = saved.get("trends", [])
    else:
        posts, errors = acquire(args.no_search)
        if args.retry and xcli.search_blocked():
            posts.extend(retry_searches(errors))
        trends, trend_err = xcli.trending()
        if trend_err:
            errors.append(trend_err)

    pool = load_json(POOL_PATH, {})
    pool = merge_pool(pool, posts)
    log(f"pool: {len(pool)} posts within {POOL_MAX_AGE_H}h window")

    seen = load_json(SEEN_PATH, {})
    seen_cutoff = (NOW.date() - timedelta(days=SEEN_MAX_AGE_D)).isoformat()
    seen = {k: v for k, v in seen.items() if v.get("date", "") >= seen_cutoff}

    if args.collect:
        POOL_PATH.write_text(json.dumps(pool, indent=2, ensure_ascii=False), encoding="utf-8")
        candidates = get_candidates(pool, seen)
        (ROOT / "data" / "candidates.json").write_text(json.dumps({
            "for_date": STAMP,
            "trends": trends,
            "candidates": [{
                "id": p["id"], "author": p["author"], "text": p["text"][:280],
                "likes": p["likes"], "views": p["views"],
                "velocity": round(p.get("velocity", 0), 1),
                "topic_hint": p.get("topic_hint", ""), "sources": p.get("sources", []),
            } for p in candidates],
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        log(f"collected {len(candidates)} candidates -> data/candidates.json")
        log("next: write data/editorial.json (stories/top3_ids/quick_read), then run --render-only")
        return 0

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
