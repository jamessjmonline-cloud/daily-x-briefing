#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 run_signal_now.py >/tmp/daily-x-signal-now.json
python3 render_signal_artifact.py
git add index.html archive data/signal-now.json render_signal_artifact.py run_signal_now.py update-now.sh
git commit -m "chore: update on-demand signal brief $(date +%Y-%m-%d-%H%M)" || true
git push origin main
printf '\nPublished: https://jamessjmonline-cloud.github.io/daily-x-briefing/\n'
