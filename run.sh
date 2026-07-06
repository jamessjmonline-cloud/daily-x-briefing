#!/bin/zsh
# Daily X briefing entrypoint — run by launchd every morning, or manually:
#   ./run.sh            full daily run (retries once if rate-limited, ~30 min worst case)
#   ./run.sh refresh    quick midday page refresh (no search, no seen-marking, no email)
set -uo pipefail
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

# ANTHROPIC_API_KEY lives in .env (gitignored) — enables Claude enrichment.
[ -f .env ] && source .env

mkdir -p logs

if [ "${1:-daily}" = "refresh" ]; then
  .venv/bin/python generate_briefing.py --refresh --no-search
else
  .venv/bin/python generate_briefing.py --retry
fi
rc=$?

if [ $rc -eq 0 ]; then
  # Publish to GitHub Pages
  git add -A
  git commit -q -m "chore: daily X briefing $(date +%F)" 2>/dev/null || true
  git push -q origin HEAD 2>/dev/null || echo "push failed (offline?) — will publish next run"

  # Morning nudge: top quick-read line as a macOS notification
  TOP=$(.venv/bin/python - <<'PY' 2>/dev/null || echo "Briefing ready"
import json, glob
d = json.load(open(sorted(glob.glob("data/20*.json"))[-1]))
q = d.get("quick_read") or ["Briefing ready"]
print(q[0][:150].replace('"', "'"))
PY
)
  osascript -e "display notification \"$TOP\" with title \"Daily X Briefing\" sound name \"Glass\"" 2>/dev/null || true
else
  osascript -e 'display notification "Generation FAILED — check logs/run.log" with title "Daily X Briefing"' 2>/dev/null || true
fi

exit $rc
