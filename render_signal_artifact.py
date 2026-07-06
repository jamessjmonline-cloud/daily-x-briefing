#!/usr/bin/env python3
"""Render the on-demand Signal Brief into a clean, readable HTML artifact.

Input:  data/signal-now.json (created by run_signal_now.py)
Output: index.html + archive/YYYY-MM-DD-signal.html
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "signal-now.json"
TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime.now(TZ)
STAMP = NOW.strftime("%Y-%m-%d")


def load() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


def clean(text: str, n: int | None = None) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if n and len(text) > n:
        return text[: n - 1].rstrip() + "…"
    return text


def compact(n: int | None) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def all_posts(payload: dict) -> dict[str, dict]:
    posts = {}
    for items in payload.get("sections", {}).values():
        for p in items:
            posts[p.get("id") or p.get("url")] = p
    return posts


def contains(p: dict, terms: list[str]) -> bool:
    blob = f"{p.get('text','')} {p.get('author','')}".lower()
    for term in terms:
        t = term.lower()
        if t == "ai":
            if re.search(r"\bai\b", blob):
                return True
        elif t in blob:
            return True
    return False


def reject(p: dict, terms: list[str]) -> bool:
    return contains(p, terms)


def unique(items: list[dict], limit: int) -> list[dict]:
    seen = set(); out = []
    for p in items:
        key = p.get("id") or p.get("url")
        if not key or key in seen:
            continue
        seen.add(key); out.append(p)
        if len(out) >= limit:
            break
    return out


def source_label(p: dict) -> str:
    return f"@{p.get('author','unknown')} · {compact(p.get('likes'))} likes · {compact(p.get('views'))} views · ▲ {p.get('velocity',0)}/h"


def infer_why(p: dict, section: str) -> str:
    text = p.get("text", "")
    if section == "business":
        if contains(p, ["alibaba", "pentagon", "blacklist"]):
            return "Business/geopolitics item involving China, U.S. policy, and a major tech company. Worth tracking beyond the meme cycle."
        if contains(p, ["federal reserve", "home prices", "rents"]):
            return "A market-policy claim with housing implications; useful, but should be verified against the cited report."
        if contains(p, ["ai agents", "cinemas", "china"]):
            return "Shows AI-agent language spreading into consumer/retail policy, not just software circles."
        if contains(p, ["positions", "fund"]):
            return "Investor watchlist signal; useful for seeing what public-market operators are focused on."
        return "Business-relevant enough to check, but treat it as a lead rather than confirmed news."
    if section == "security":
        return "Relevant to MSP/security awareness: social engineering, account recovery, vendor trust, or incident response lessons."
    if section == "ai":
        return "Useful AI/automation signal: either a macro narrative from a major account or a practical adoption/tooling item."
    if section == "culture":
        return "High social velocity; likely to spill from X into sports/culture conversation today."
    if section == "usa":
        return "U.S.-relevant social signal mixing sports, politics, culture, or markets."
    if section == "world":
        return "Global social signal; current worldwide attention is heavily football/World Cup driven."
    return "High velocity and broad reach make this worth knowing before the day gets loud."


def card(p: dict, i: int, section: str) -> str:
    title = clean(p.get("text", ""), 142)
    url = escape(p.get("url", "#"))
    return f"""
      <article class="item">
        <div class="idx">{i}</div>
        <div>
          <p class="meta">{escape(source_label(p))}</p>
          <h3>{escape(title)}</h3>
          <p class="why"><strong>Why it matters:</strong> {escape(infer_why(p, section))}</p>
          <a href="{url}" target="_blank" rel="noopener noreferrer">Open post →</a>
        </div>
      </article>
    """


def section(title: str, deck: str, items: list[dict], key: str, empty: str = "No strong clean signal found in this pull.") -> str:
    body = "".join(card(p, i + 1, key) for i, p in enumerate(items)) if items else f'<div class="empty">{escape(empty)}</div>'
    return f"""
    <section id="{escape(key)}" class="section">
      <div class="section-head">
        <p class="eyebrow">{escape(title)}</p>
        <h2>{escape(deck)}</h2>
      </div>
      <div class="items">{body}</div>
    </section>
    """


def render(payload: dict) -> str:
    posts = list(all_posts(payload).values())
    posts = sorted(posts, key=lambda p: float(p.get("score") or 0), reverse=True)
    sec = payload.get("sections", {})

    must = unique(sec.get("must_know", []), 5)
    fast = unique(sec.get("fast_risers", []), 5)
    business_pool = sec.get("business_markets", []) + posts
    business = unique([p for p in business_pool if contains(p, ["alibaba", "federal reserve", "home prices", "rents", "ai agents", "cinemas", "positions", "fund", "market", "bitcoin", "crypto", "earnings", "ipo"]) and not reject(p, ["ronaldo confirms", "norway stuns brazil", "world cup quarterfinals", "trump thanks fifa", "folarin", "neymar", "world cup final", "fifa president", "polymarket status/207398", "polymarket/status/207398"])], 5)
    security = unique([p for p in posts if contains(p, ["microsoft", "hacked", "hack", "cve", "ransomware", "breach", "phishing", "zero day", "outage", "vulnerability", "exploit"]) and not reject(p, ["body-cam", "jidion", "police"])], 4)
    ai = unique([p for p in posts if contains(p, ["ai+robots", "ai agents", "openai", "anthropic", "claude", "chatgpt", "github", "repo", "llm", "automation", "voice cloning"]) and not reject(p, ["all the feels", "tennis", "england midfielder", "police body-cam"])], 5)
    culture = unique(sec.get("culture_sports_ent", []), 6)
    usa = unique(sec.get("usa", []), 5)
    world = unique(sec.get("worldwide", []), 5)
    trends = payload.get("trends", [])[:12]

    collected = payload.get("collected_at", "")
    trend_html = "".join(f'<a class="trend" href="https://x.com/search?q={escape(str(t.get("topic",""))).replace(" ", "+")}&src=typed_query&f=top" target="_blank"><span>#{t.get("rank")}</span><strong>{escape(str(t.get("topic","")))}</strong><em>{escape(str(t.get("category","Trending")))}</em></a>' for t in trends)

    quick = [
        "World Cup / football is the dominant social layer right now: England, Mexico, Norway, FIFA, and Haaland-adjacent clips are everywhere.",
        "The most useful non-sports leads are Venezuela earthquake videos, Alibaba/Pentagon blacklist relief, China cinema AI-agent guidance, and a Microsoft/social-engineering security thread.",
        "USA conversation is a mix of sports + politics; worldwide conversation is overwhelmingly football-driven.",
        "AI and security sections are intentionally stricter now; weak false positives are not padded into the brief.",
    ]
    quick_html = "".join(f"<li>{escape(q)}</li>" for q in quick)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Morning Signal Brief — {escape(STAMP)}</title>
<style>
:root {{--bg:#f7f4ee;--paper:#fffdfa;--ink:#171717;--muted:#6f6a62;--line:#e5ddd0;--accent:#b24a2f;--blue:#163f63;}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;}}
a{{color:var(--blue);font-weight:750;text-decoration:none}}a:hover{{text-decoration:underline}}.wrap{{width:min(1060px,calc(100% - 32px));margin:0 auto}}
header{{padding:44px 0 22px}}.hero{{background:var(--paper);border:1px solid var(--line);border-radius:28px;padding:34px;box-shadow:0 18px 60px rgba(50,35,20,.07)}}
.kicker,.eyebrow{{font-size:12px;letter-spacing:.18em;text-transform:uppercase;color:var(--accent);font-weight:900}}h1{{font-family:Georgia,serif;font-size:clamp(42px,6vw,78px);line-height:.92;letter-spacing:-.055em;margin:12px 0}}.intro{{font-size:18px;line-height:1.62;color:#514b43;max-width:780px}}
.pills,nav{{display:flex;gap:9px;flex-wrap:wrap;margin-top:20px}}.pill,nav a{{border:1px solid var(--line);border-radius:999px;padding:8px 12px;background:#fff;color:#514b43;font-size:13px}}nav a{{color:var(--ink)}}
.grid{{display:grid;grid-template-columns:1.2fr .8fr;gap:16px;margin:18px 0}}.panel,.section{{background:var(--paper);border:1px solid var(--line);border-radius:24px;padding:24px;margin:16px 0}}h2{{font-family:Georgia,serif;font-size:clamp(26px,3vw,38px);line-height:1.05;letter-spacing:-.035em;margin:6px 0 0}}.quick li{{margin:10px 0;line-height:1.5}}
.item{{display:grid;grid-template-columns:36px 1fr;gap:14px;padding:16px 0;border-top:1px solid var(--line)}}.item:first-child{{border-top:0}}.idx{{width:30px;height:30px;border-radius:50%;display:grid;place-items:center;background:var(--ink);color:#fff;font-weight:850}}.meta{{margin:0 0 6px;color:var(--muted);font-size:13px}}h3{{margin:0 0 8px;font-size:19px;line-height:1.28}}.why{{margin:0 0 10px;color:#4d463d;line-height:1.5}}.empty{{border:1px dashed var(--line);border-radius:18px;padding:18px;color:var(--muted);line-height:1.5}}
.trends{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}}.trend{{display:block;border:1px solid var(--line);border-radius:16px;padding:12px;background:#fff}}.trend span{{color:var(--accent);font-size:12px}}.trend strong{{display:block;color:var(--ink)}}.trend em{{font-style:normal;color:var(--muted);font-size:12px}}footer{{text-align:center;color:var(--muted);font-size:13px;padding:30px 0 56px}}
@media(max-width:820px){{.grid{{grid-template-columns:1fr}}.trends{{grid-template-columns:1fr}}.hero{{padding:26px}}}}
</style></head>
<body><div class="wrap">
<header><div class="hero"><p class="kicker">Morning Signal Brief · {escape(NOW.strftime('%A, %B %-d, %Y'))}</p><h1>The useful parts of the scroll.</h1><p class="intro">A clean, on-demand brief from Agent-Reach: viral momentum, fast risers, business/markets, IT/security, AI, culture, USA, and worldwide signals. Trends are separated from posts.</p><div class="pills"><span class="pill">Collected {escape(collected)}</span><span class="pill">{payload.get('post_count',0)} real posts scanned</span><span class="pill">No fake metrics</span></div></div><nav><a href="#must">Must Know</a><a href="#business">Business</a><a href="#security">Security</a><a href="#ai">AI</a><a href="#culture">Culture</a><a href="#usa">USA</a><a href="#world">Worldwide</a></nav></header>
<div class="grid"><section class="panel quick"><p class="eyebrow">Read first</p><h2>Today’s takeaways</h2><ul>{quick_html}</ul></section><section class="panel"><p class="eyebrow">Trend watchlist</p><h2>Topics to check manually</h2><div class="trends">{trend_html}</div></section></div>
{section('Must Know', 'The posts most likely to matter or cross into conversation.', must, 'must')}
{section('Fast Risers', 'High-velocity posts gaining attention quickly.', fast, 'fast')}
{section('Business & Markets', 'Useful leads without padding sports-betting noise into markets.', business, 'business')}
{section('IT / Security / MSP', 'Actionable or semi-actionable security and IT leads.', security, 'security')}
{section('AI / Automation', 'AI macro narratives, adoption signals, and tool/workflow leads.', ai, 'ai')}
{section('Culture / Sports / Entertainment', 'The social layer people are most likely to reference today.', culture, 'culture')}
{section('USA Trending', 'U.S.-relevant posts and cultural/political/sports crossovers.', usa, 'usa')}
{section('Worldwide Trending', 'Global viral posts and international sports/culture signals.', world, 'world')}
<footer>Generated by Hermes Agent using Agent-Reach/OpenCLI. Run <code>./update-now.sh</code> to refresh manually.</footer>
</div></body></html>"""


def main() -> int:
    payload = load()
    html = render(payload)
    (ROOT / "index.html").write_text(html, encoding="utf-8")
    (ROOT / "archive" / f"{STAMP}-signal.html").write_text(html, encoding="utf-8")
    print(f"wrote {ROOT / 'index.html'}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
