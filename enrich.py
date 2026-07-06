"""Story enrichment: cluster posts into stories, classify by topic, write
real 'why it matters' lines and the quick read.

Uses the Claude API (claude-haiku-4-5, structured output) when credentials
are available; otherwise falls back to rule-based keyword classification so
the briefing still ships. Never fabricates posts or metrics — it only works
with the candidate posts it is given.
"""
from __future__ import annotations

import json
import os

MODEL = "claude-haiku-4-5"

STORY_SCHEMA = {
    "type": "object",
    "properties": {
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "headline": {"type": "string"},
                    "why": {"type": "string"},
                    "topic": {"type": "string"},
                    "primary_id": {"type": "string"},
                    "related_ids": {"type": "array", "items": {"type": "string"}},
                    "drop": {"type": "boolean"},
                },
                "required": ["headline", "why", "topic", "primary_id", "related_ids", "drop"],
                "additionalProperties": False,
            },
        },
        "top3_ids": {"type": "array", "items": {"type": "string"}},
        "quick_read": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["stories", "top3_ids", "quick_read"],
    "additionalProperties": False,
}


def _have_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def enrich(candidates: list[dict], topic_keys: list[str]) -> tuple[dict | None, str | None]:
    """Returns (result, note). result is None when the LLM path is unavailable
    or fails; the caller then uses the rule-based fallback."""
    if not candidates:
        return None, "no candidates to enrich"
    if not _have_credentials():
        return None, "ANTHROPIC_API_KEY not set; using rule-based fallback"
    try:
        import anthropic
    except ImportError:
        return None, "anthropic SDK not installed; using rule-based fallback"

    lines = []
    for p in candidates:
        lines.append(json.dumps({
            "id": p["id"],
            "author": p["author"],
            "text": p["text"][:280],
            "likes": p["likes"],
            "views": p["views"],
            "velocity_likes_per_hr": round(p.get("velocity", 0), 1),
        }, ensure_ascii=False))

    topics = ", ".join(topic_keys)
    prompt = f"""You are the editor of a sharp daily X (Twitter) briefing. Below are real posts collected in the last 24h, one JSON object per line. Turn them into briefing stories.

Rules:
- Cluster posts about the same underlying story into ONE story: pick the strongest post as primary_id, put the rest in related_ids. Unrelated posts are their own story with empty related_ids.
- topic: one of [{topics}, viral]. Use "viral" for pure culture/meme moments that fit no topic.
- headline: a specific, punchy line (max 90 chars) capturing the actual news/moment — not a template.
- why: ONE sentence on why this matters or where it's heading. Concrete, no filler.
- drop: true for ads, promos, engagement bait, or spam (e.g. shopping promos with huge views but tiny likes).
- top3_ids: the 3 primary_ids most worth knowing today across all topics (highest stakes or fastest-moving).
- quick_read: 3-5 bullets summarizing the day in X, each under 140 chars.
- Use only the posts given. Never invent posts, numbers, or events.

Posts:
{chr(10).join(lines)}"""

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            output_config={"format": {"type": "json_schema", "schema": STORY_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        text = next(b.text for b in response.content if b.type == "text")
        result = json.loads(text)
    except Exception as e:
        return None, f"enrichment call failed ({e}); using rule-based fallback"

    known = {p["id"] for p in candidates}
    stories = [s for s in result.get("stories", [])
               if not s.get("drop") and s.get("primary_id") in known]
    for s in stories:
        s["related_ids"] = [r for r in s.get("related_ids", []) if r in known and r != s["primary_id"]]
    result["stories"] = stories
    result["top3_ids"] = [i for i in result.get("top3_ids", []) if i in {s["primary_id"] for s in stories}][:3]
    return result, None


# ---------------------------------------------------------------------------
# Rule-based fallback (no API key): keyword classification, no clustering.
# ---------------------------------------------------------------------------

def fallback(candidates: list[dict], topics: list[dict]) -> dict:
    def classify(p: dict) -> str:
        blob = p["text"].lower()
        for t in topics:
            if any(k in blob for k in t["keywords"]):
                return t["key"]
        return "viral"

    stories = []
    for p in candidates:
        text = p["text"]
        headline = text if len(text) <= 90 else text[:87].rstrip() + "…"
        stories.append({
            "headline": headline,
            "why": f"Moving fast on X right now: {_num(p['likes'])} likes, {_num(p['views'])} views.",
            "topic": classify(p),
            "primary_id": p["id"],
            "related_ids": [],
            "drop": False,
        })
    ranked = sorted(candidates, key=lambda p: p.get("velocity", 0), reverse=True)
    top3 = [p["id"] for p in ranked[:3]]
    quick = []
    for p in ranked[:4]:
        quick.append(f"@{p['author']}: {p['text'][:110]}")
    return {"stories": stories, "top3_ids": top3, "quick_read": quick}


def _num(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)
