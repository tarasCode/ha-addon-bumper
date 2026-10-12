#!/bin/sh
# Start Bumper; if a map robot is configured, also redraw its map every 10 minutes for HA.
BOT=$(/bumper/.venv/bin/python -c 'import json;print(json.load(open("/data/options.json")).get("map_bot",""))' 2>/dev/null)
if [ -n "$BOT" ]; then
  (sleep 90; exec /bumper/.venv/bin/python /deebot_map.py --bot "$BOT" \
     --out /share/deebot/mappa.png --every 600) &
fi
exec /bumper/.venv/bin/bumper
