"""X/Twitter access via agent-reach's routing chain.

agent-reach doctor reports twitter's active backend as twitter-cli (the
`twitter` command); OpenCLI is the fallback. This module follows that chain
programmatically: prefer `twitter`, fall back to `opencli twitter`, and
degrade gracefully on 429s (stop issuing search calls for the rest of the run).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

TWITTER_BIN = shutil.which("twitter")
OPENCLI_BIN = shutil.which("opencli")

# Once any search hits a rate limit, stop searching for the rest of the run.
_search_blocked = False


def search_blocked() -> bool:
    return _search_blocked


def _run(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def _short(msg: str) -> str:
    return re.sub(r"\s+", " ", msg.strip())[:240]


def _is_rate_limited(msg: str) -> bool:
    m = msg.lower()
    return "429" in m or "rate-limit" in m or "rate limit" in m


# ---------------------------------------------------------------------------
# twitter-cli JSON envelope: {"ok": true, "schema_version": "1", "data": [...]}
# ---------------------------------------------------------------------------

def _twitter_json(args: list[str], timeout: int = 120) -> tuple[list[dict], str | None]:
    try:
        code, stdout, stderr = _run([TWITTER_BIN, *args, "--json"], timeout)
    except Exception as e:  # timeout, OSError
        return [], f"twitter {args[0]} failed: {e}"
    try:
        payload = json.loads(stdout)
    except Exception:
        payload = None
    if isinstance(payload, dict) and payload.get("ok") and isinstance(payload.get("data"), list):
        return payload["data"], None
    err = _short(stderr or stdout) or "twitter-cli returned no data"
    return [], f"twitter {args[0]} degraded: {err}"


def _normalize_twitter(item: dict, source: str) -> dict | None:
    author = item.get("author") or {}
    screen = str(author.get("screenName") or "").lstrip("@")
    tid = str(item.get("id") or "")
    text = re.sub(r"https?://\S+", "", str(item.get("text") or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if not tid or not screen or not text:
        return None
    metrics = item.get("metrics") or {}
    media = item.get("media") or []
    thumb = ""
    for m in media:
        if m.get("type") == "photo" and m.get("url"):
            thumb = m["url"]
            break
    return {
        "id": tid,
        "author": screen,
        "author_name": str(author.get("name") or screen),
        "text": text,
        "created_iso": str(item.get("createdAtISO") or ""),
        "likes": int(metrics.get("likes") or 0),
        "retweets": int(metrics.get("retweets") or 0),
        "replies": int(metrics.get("replies") or 0),
        "views": int(metrics.get("views") or 0),
        "url": f"https://x.com/{screen}/status/{tid}",
        "has_media": bool(media),
        "media_thumb": thumb,
        "lang": str(item.get("lang") or ""),
        "is_retweet": bool(item.get("isRetweet")),
        "sources": [source],
    }


# ---------------------------------------------------------------------------
# OpenCLI fallback (legacy flat schema used by the old generator)
# ---------------------------------------------------------------------------

def _opencli_json(args: list[str], timeout: int = 150) -> tuple[list[dict], str | None]:
    cmd = [OPENCLI_BIN, "twitter", *args, "--window", "background", "--site-session", "persistent"]
    try:
        code, stdout, stderr = _run(cmd, timeout)
    except Exception as e:
        return [], f"opencli {args[0]} failed: {e}"
    start = stdout.find("[")
    items: list[dict] = []
    if start != -1:
        try:
            data = json.loads(stdout[start:stdout.rfind("]") + 1])
            items = data if isinstance(data, list) else []
        except Exception:
            items = []
    if code != 0:
        return items, f"opencli {args[0]} degraded: {_short(stderr or stdout)}"
    return items, None


def _normalize_opencli(item: dict, source: str) -> dict | None:
    tid = str(item.get("id") or "")
    author = str(item.get("author") or "").lstrip("@")
    text = re.sub(r"https?://\S+", "", str(item.get("text") or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if not tid or not author or not text:
        return None
    posters = item.get("media_posters") or []
    return {
        "id": tid,
        "author": author,
        "author_name": author,
        "text": text,
        "created_iso": str(item.get("created_at") or ""),
        "likes": int(item.get("likes") or 0),
        "retweets": int(item.get("retweets") or 0),
        "replies": int(item.get("replies") or 0),
        "views": int(item.get("views") or 0),
        "url": item.get("url") or f"https://x.com/{author}/status/{tid}",
        "has_media": bool(item.get("has_media")),
        "media_thumb": str(posters[0]) if posters else "",
        "lang": "",
        "is_retweet": False,
        "sources": [source],
    }


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

def feed(kind: str, n: int = 40) -> tuple[list[dict], str | None]:
    """kind: 'for-you' or 'following'."""
    source = f"feed:{kind}"
    if TWITTER_BIN:
        items, err = _twitter_json(["feed", "--type", kind, "-n", str(n)], timeout=150)
        posts = [p for p in (_normalize_twitter(i, source) for i in items) if p]
        return posts, err
    if OPENCLI_BIN:
        items, err = _opencli_json(["timeline", "--type", kind, "--limit", str(n), "-f", "json"], timeout=180)
        posts = [p for p in (_normalize_opencli(i, source) for i in items) if p]
        return posts, err
    return [], "no twitter backend available (twitter-cli or opencli)"


def list_tweets(list_id: str, n: int = 30) -> tuple[list[dict], str | None]:
    source = f"list:{list_id}"
    if TWITTER_BIN:
        items, err = _twitter_json(["list", list_id, "-n", str(n)], timeout=150)
        return [p for p in (_normalize_twitter(i, source) for i in items) if p], err
    return [], "list fetch requires twitter-cli"


def search(query: str, topic: str, since: str, min_likes: int, n: int = 15) -> tuple[list[dict], str | None]:
    """One gentle search call. Sets the module-wide block flag on 429."""
    global _search_blocked
    source = f"search:{topic}"
    if _search_blocked:
        return [], None
    if TWITTER_BIN:
        args = ["search", query, "-t", "top", "--lang", "en", "--since", since,
                "--min-likes", str(min_likes), "--exclude", "replies", "-n", str(n)]
        items, err = _twitter_json(args, timeout=150)
        posts = [p for p in (_normalize_twitter(i, source) for i in items) if p]
    elif OPENCLI_BIN:
        q = f"({query}) min_faves:{min_likes} lang:en since:{since} -filter:replies"
        items, err = _opencli_json(["search", q, "--product", "top", "--limit", str(n), "-f", "json"])
        posts = [p for p in (_normalize_opencli(i, source) for i in items) if p]
    else:
        return [], "no twitter backend available"
    if err and _is_rate_limited(err):
        _search_blocked = True
    return posts, err


def trending() -> tuple[list[dict], str | None]:
    """Trend watchlist. twitter-cli has no trending command; opencli only."""
    if not OPENCLI_BIN:
        return [], None
    items, err = _opencli_json(["trending", "-f", "json"], timeout=90)
    trends = []
    for t in items[:20]:
        topic = str(t.get("topic") or "").strip()
        if topic:
            trends.append({
                "rank": int(t.get("rank") or 0) or len(trends) + 1,
                "topic": topic,
                "category": str(t.get("category") or "Trending"),
            })
    return trends, err
