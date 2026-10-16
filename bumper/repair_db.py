"""Keep Bumper bootable when its TinyDB file ends with trailing garbage.

TinyDB stores one JSON document; an interrupted or overlapping write can leave a
second fragment after it, and Bumper then dies at start with "Extra data". The
first document is intact, so we keep it, set the damaged file aside, and go on.
"""

import json
import os
import shutil
import sys
import time

path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DB_FILE", "/config/data/bumper.db")
if not os.path.exists(path):
    sys.exit(0)

with open(path, encoding="utf-8") as handle:
    text = handle.read()

try:
    json.loads(text)
    sys.exit(0)
except json.JSONDecodeError:
    pass

try:
    document, end = json.JSONDecoder().raw_decode(text.lstrip())
except json.JSONDecodeError as error:
    print(f"repair_db: {path} unreadable ({error}), left untouched", flush=True)
    sys.exit(0)

aside = f"{path}.damaged-{time.strftime('%Y%m%d-%H%M%S')}"
shutil.copy2(path, aside)
with open(path, "w", encoding="utf-8", newline="") as handle:
    json.dump(document, handle)
print(f"repair_db: trailing data removed from {path} (copy at {aside})", flush=True)
