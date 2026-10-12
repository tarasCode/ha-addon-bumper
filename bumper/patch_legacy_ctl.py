"""Patch Bumper's XMPP relay so legacy clients (HA ecovacs via sucks) get answers from XMPP bots.

The Ecovacs cloud does two things Bumper 0.4.1 does not:
1. it gives every client command an `id` attribute; an OZMO 930 ignores a ctl without one
   (it acks the iq but sends no data);
2. it puts the `td` name back on the bot's reply (`<ctl id=.. ret="ok"><battery/>` becomes
   `<ctl td="BatteryInfo" ..>`); sucks drops replies without `td`.
Both are done here with a small id -> td map. A halted 930 also reports clean type="auto"
(st="h"), which HA shows as cleaning; such reports are rewritten to type="stop". The build fails if the anchors are not found,
so a new Bumper version cannot silently ship without the fix.
"""

import glob
import sys

paths = sys.argv[1:] or glob.glob("/bumper/.venv/lib/python*/site-packages/bumper/xmpp/xmpp.py")
if len(paths) != 1:
    sys.exit(f"xmpp.py not found exactly once: {paths}")
path = paths[0]
src = open(path, encoding="utf-8").read()
if "vallenza patch" in src:
    sys.exit("already patched")

HELPERS = '''

# --- vallenza patch: legacy ctl id/td translation (see patch_legacy_ctl.py) ---
_VALLENZA_PENDING: dict[str, str] = {}
_VALLENZA_REPLY_TD = {"GetCleanState": "CleanReport"}


def _vallenza_ctl(xml):
    try:
        return xml[0][0] if len(xml) and len(xml[0]) else None
    except Exception:
        return None


def _vallenza_tag_request(xml) -> None:
    ctl = _vallenza_ctl(xml)
    if ctl is None or not ctl.get("td") or ctl.get("id"):
        return
    td = ctl.get("td")
    ctl_id = uuid.uuid4().hex[:8]
    ctl.set("id", ctl_id)
    reply = _VALLENZA_REPLY_TD.get(td, td[3:] if td.startswith("Get") else td)
    _VALLENZA_PENDING[ctl_id] = reply
    if len(_VALLENZA_PENDING) > 500:
        _VALLENZA_PENDING.pop(next(iter(_VALLENZA_PENDING)))


def _vallenza_tag_response(xml) -> None:
    ctl = _vallenza_ctl(xml)
    if ctl is None:
        return
    if not ctl.get("td"):
        reply = _VALLENZA_PENDING.pop(ctl.get("id") or "", None)
        if reply:
            ctl.set("td", reply)
    # A halted 930 still reports type="auto" (st="h"); sucks would read it as cleaning.
    if ctl.get("td") == "CleanReport":
        for clean in ctl:
            if clean.tag.endswith("clean") and clean.get("st") == "h":
                clean.set("type", "stop")
# --- end vallenza patch ---
'''

REQ_ANCHOR = "            # forward\n            for client in XMPPServer.clients:\n"
RES_ANCHOR = (
    '    def _handle_result(self, xml: Element, data: str) -> None:\n'
    '        try:\n'
    '            ctl_to = xml.get("to")\n'
)
IMPORT_ANCHOR = "from bumper.utils.settings import config as bumper_isc\n"

for anchor in (REQ_ANCHOR, RES_ANCHOR, IMPORT_ANCHOR):
    if src.count(anchor) != 1:
        sys.exit(f"anchor not found exactly once: {anchor!r}")

src = src.replace(IMPORT_ANCHOR, IMPORT_ANCHOR + HELPERS)
src = src.replace(REQ_ANCHOR, "            _vallenza_tag_request(xml)\n" + REQ_ANCHOR)
src = src.replace(RES_ANCHOR, RES_ANCHOR + "            if self.type == self.BOT:\n                _vallenza_tag_response(xml)\n")
open(path, "w", encoding="utf-8").write(src)
print("patched", path)
