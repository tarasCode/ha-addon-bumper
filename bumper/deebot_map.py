"""Draw the map of an XMPP Deebot (OZMO 930 family) to a PNG, live, through the local Bumper.

Stdlib only, so it runs inside the add-on without new packages. It logs into Bumper's XMPP
server as a client and keeps one connection open:
- the map layout (GetMapM) and its non-empty pieces (PullMP, base64 LZMA) are downloaded
  again only when a piece's CRC changes;
- virtual walls come from GetMapSet tp=vw;
- while the robot cleans, its position (GetPos) is read every few seconds and the path it
  covered is painted on the map; a new cleaning run starts a new path.
The image is rewritten atomically only when something visible changed.

Usage: deebot_map.py --bot <did>@<class>.ecorobot.net/atom --out /path/map.png
"""

import argparse
import base64
import json
import lzma
import os
import re
import socket
import ssl
import struct
import sys
import time
import uuid
import zlib

FLOOR = (186, 218, 255)
WALL_CELL = (40, 40, 60)
BACKGROUND = (255, 255, 255)
CLEANED = (110, 200, 140)
VIRTUAL_WALL = (230, 60, 60)
ROBOT = (20, 90, 200)
CELL_COLORS = {1: FLOOR, 2: WALL_CELL, 3: FLOOR}
SCALE = 4
ROBOT_RADIUS_MM = 170


class Xmpp:
    """Minimal XMPP client: STARTTLS, SASL PLAIN, bind, one iq at a time."""

    def __init__(self, host, port, user, domain, resource):
        self.user, self.domain, self.resource = user, domain, resource
        self.sock = socket.create_connection((host, port), timeout=20)
        self.buf = ""
        self._open()
        self._read_until("</stream:features>")
        self._send("<starttls xmlns='urn:ietf:params:xml:ns:xmpp-tls'/>")
        self._read_until("<proceed")
        # Bumper only has a self-signed certificate and this connection never leaves the add-on.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.sock = ctx.wrap_socket(self.sock, server_hostname=domain)
        self.buf = ""
        self._open()
        self._read_until("</stream:features>")
        password = f"0/{resource}/map"
        token = base64.b64encode(f"{user}\0{user}\0{password}".encode()).decode()
        self._send(
            f"<auth xmlns='urn:ietf:params:xml:ns:xmpp-sasl' mechanism='PLAIN'>{token}</auth>"
        )
        self._read_until("success")
        self.buf = ""
        self._open()
        self._read_until("</stream:features>")
        self._send(
            f"<iq type='set' id='bind'><bind xmlns='urn:ietf:params:xml:ns:xmpp-bind'>"
            f"<resource>{resource}</resource></bind></iq>"
        )
        self._read_until("</iq>")
        self._send(
            "<iq type='set' id='sess'><session xmlns='urn:ietf:params:xml:ns:xmpp-session'/></iq>"
        )
        self._read_until("</iq>")

    def _open(self):
        self._send(
            f"<?xml version='1.0'?><stream:stream to='{self.domain}' xmlns='jabber:client' "
            "xmlns:stream='http://etherx.jabber.org/streams' version='1.0'>"
        )

    def _send(self, text):
        self.sock.sendall(text.encode())

    def _recv(self, timeout):
        self.sock.settimeout(max(0.1, timeout))
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("server closed the stream")
        self.buf += chunk.decode(errors="replace")

    def _read_until(self, marker, timeout=20.0):
        end = time.time() + timeout
        while marker not in self.buf:
            if time.time() > end:
                raise TimeoutError(f"no {marker!r} from server")
            self._recv(end - time.time())
        head, _, self.buf = self.buf.partition(marker)
        return head + marker

    def ctl(self, bot, body, timeout=15.0):
        """Send one ctl to the bot and return its reply, up to the closing iq."""
        cid = uuid.uuid4().hex[:8]
        me = f"{self.user}@{self.domain}/{self.resource}"
        body = body.replace("<ctl ", f'<ctl id="{cid}" ', 1)
        self._send(
            f'<iq type="set" id="m{cid}" from="{me}" to="{bot}"><query xmlns="com:ctl">{body}</query></iq>'
        )
        end = time.time() + timeout
        while True:
            m = re.search(r"<ctl[^>]*id=['\"]" + cid + r"['\"]", self.buf)
            if m:
                tail = self.buf[m.start() :]
                close = tail.find("</iq>")
                if close >= 0:
                    self.buf = tail[close + 5 :]
                    return tail[:close]
            if time.time() > end:
                break
            try:
                self._recv(end - time.time())
            except socket.timeout:
                break
        # Drop what piled up (pushes from the bot) so the buffer does not grow forever.
        self.buf = self.buf[-4096:]
        raise TimeoutError(f"no reply to {body[:40]}")

    def close(self):
        try:
            self._send("</stream:stream>")
            self.sock.close()
        except OSError:
            pass


def attrs(xml, tag="ctl"):
    part = xml.split("<" + tag, 1)
    if len(part) < 2:
        return {}
    head = part[1].split(">", 1)[0]
    return {
        k: a or b for k, a, b in re.findall(r"(\w+)=(?:\"([^\"]*)\"|'([^']*)')", head)
    }


def png(width, height, pixels, meta=None):
    """Encode an RGB PNG; `meta` travels in a tEXt chunk keyed "vallenza" for the HA card."""
    stride = width * 3
    rows = b"".join(
        b"\x00" + bytes(pixels[y * stride : (y + 1) * stride]) for y in range(height)
    )

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + (chunk(b"tEXt", b"vallenza\0" + json.dumps(meta).encode()) if meta else b"")
        + chunk(b"IDAT", zlib.compress(rows, 6))
        + chunk(b"IEND", b"")
    )


def segment(cells, a, b, radius=0):
    (x0, y0), (x1, y1) = a, b
    steps = max(abs(x1 - x0), abs(y1 - y0), 1)
    for i in range(steps + 1):
        cx, cy = round(x0 + (x1 - x0) * i / steps), round(y0 + (y1 - y0) * i / steps)
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if dx * dx + dy * dy <= radius * radius:
                    cells.add((cx + dx, cy + dy))


class MapState:
    def __init__(self, out):
        self.out = out
        self.trail_file = os.path.join(os.path.dirname(out), "percorso.json")
        self.layout = None
        self.grid = None
        self.walls = {}
        self.msid = "1"
        self.trail = []
        self.robot_mm = None
        self.cleaning = False
        self.last_image = None
        try:
            with open(self.trail_file) as f:
                saved = json.load(f)
            self.trail = [tuple(p) for p in saved.get("trail", [])]
        except (OSError, ValueError):
            pass
        # A saved path means a run may still be going: do not wipe it on the first reading.
        self.cleaning = bool(self.trail)

    def refresh_walls(self, conn, bot):
        """Virtual walls: GetMapSet lists their ids, PullM gives each one's points (mm)."""
        listing = conn.ctl(bot, '<ctl td="GetMapSet" tp="vw" />')
        msid = attrs(listing).get("msid", "1")
        mids = re.findall(r"mid=['\"](\d+)['\"]", listing)
        walls = {}
        for mid in mids:
            if mid in self.walls:
                walls[mid] = self.walls[mid]
                continue
            raw = attrs(
                conn.ctl(bot, f'<ctl td="PullM" tp="vw" msid="{msid}" mid="{mid}" />')
            ).get("m", "")
            nums = [float(v) for v in raw.strip("[]").split(",") if v.strip()]
            # The robot pads the point list with 0,0 pairs.
            while len(nums) >= 4 and nums[-2:] == [0.0, 0.0]:
                nums = nums[:-2]
            walls[mid] = nums
        self.walls = walls
        self.msid = msid

    def refresh_map(self, conn, bot):
        info = attrs(conn.ctl(bot, '<ctl td="GetMapM" />'))
        if not info.get("m"):
            return
        key = (info["i"], info["m"])
        if self.layout and self.layout["key"] == key:
            return
        w, h, rows, cols = (
            int(info["w"]),
            int(info["h"]),
            int(info["r"]),
            int(info["c"]),
        )
        crcs = info["m"].split(",")
        empty = max(set(crcs), key=crcs.count)
        width, height = cols * w, rows * h
        grid = bytearray(width * height)
        for pid, crc in enumerate(crcs):
            if crc == empty:
                continue
            raw = base64.b64decode(
                attrs(conn.ctl(bot, f'<ctl td="PullMP" pid="{pid}" />'))["p"]
            )
            data = lzma.decompress(
                raw[:9] + b"\x00\x00\x00\x00" + raw[9:], format=lzma.FORMAT_ALONE
            )
            # Piece data is column-major against the robot's axes: transpose it, so that a
            # position (x, y) in mm lands on column x and row y (checked against GetPos while cleaning).
            pr, pc = divmod(pid, cols)
            for y in range(h):
                for x in range(w):
                    grid[(pc * w + x) * width + pr * h + y] = data[y * w + x]
        self.layout = {
            "key": key,
            "width": width,
            "height": height,
            "res": int(info.get("p", "50")),
        }
        self.grid = grid

    def refresh_robot(self, conn, bot):
        clean = attrs(conn.ctl(bot, '<ctl td="GetCleanState" />'), "clean")
        cleaning = clean.get("st") == "s" and clean.get("type") not in ("stop", None)
        if cleaning and not self.cleaning:
            self.trail = []
        self.cleaning = cleaning
        pos = attrs(conn.ctl(bot, '<ctl td="GetPos" />'))
        if pos.get("p"):
            x, y = (float(v) for v in pos["p"].split(","))
            self.robot_mm = (x, y)
            if cleaning and (
                not self.trail
                or abs(self.trail[-1][0] - x) + abs(self.trail[-1][1] - y) > 40
            ):
                self.trail.append((x, y))
                try:
                    with open(self.trail_file + ".tmp", "w") as f:
                        json.dump({"trail": self.trail}, f)
                    os.replace(self.trail_file + ".tmp", self.trail_file)
                except OSError:
                    pass

    def render(self):
        if not self.layout:
            return False
        width, height, res = (
            self.layout["width"],
            self.layout["height"],
            self.layout["res"],
        )

        # Coordinates in walls and positions are mm from the map centre; a cell is `res` mm.
        def cell(xmm, ymm):
            return int(width / 2 + xmm / res), int(height / 2 + ymm / res)

        walls = set()
        for nums in self.walls.values():
            pts = [cell(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]
            for a, b in zip(pts, pts[1:]):
                segment(walls, a, b)
        cleaned = set()
        radius = max(1, ROBOT_RADIUS_MM // res)
        pts = [cell(*p) for p in self.trail]
        for a, b in zip(pts, pts[1:] or pts):
            segment(cleaned, a, b, radius)
        robot = cell(*self.robot_mm) if self.robot_mm else None

        used = [i for i, v in enumerate(self.grid) if v]
        if not used:
            return False
        xs, ys = [i % width for i in used], [i // width for i in used]
        x0, x1, y0, y1 = min(xs) - 3, max(xs) + 3, min(ys) - 3, max(ys) + 3
        ow, oh = (x1 - x0 + 1) * SCALE, (y1 - y0 + 1) * SCALE
        pixels = bytearray(ow * oh * 3)
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                value = (
                    self.grid[y * width + x]
                    if 0 <= x < width and 0 <= y < height
                    else 0
                )
                if (x, y) in walls:
                    color = VIRTUAL_WALL
                elif value == 2:
                    color = WALL_CELL
                elif (x, y) in cleaned and value:
                    color = CLEANED
                else:
                    color = CELL_COLORS.get(value, BACKGROUND)
                if (
                    robot
                    and (x - robot[0]) ** 2 + (y - robot[1]) ** 2 <= (radius + 1) ** 2
                ):
                    color = ROBOT
                row = bytes(color) * SCALE
                for dy in range(SCALE):
                    # Rows are flipped: seen from above, the robot's y grows upwards (Fabio, 12/10).
                    base = (((y1 - y) * SCALE + dy) * ow + (x - x0) * SCALE) * 3
                    pixels[base : base + 3 * SCALE] = row
        # The HA card maps a pixel (px, py) back to robot mm with these numbers:
        # x_mm = (px / scale + x0 - half_w) * res,  y_mm = (y1 - py / scale - half_h) * res.
        meta = {
            "scale": SCALE,
            "res": res,
            "x0": x0,
            "y1": y1,
            "half_w": width / 2,
            "half_h": height / 2,
            "walls": dict(self.walls),
            "msid": self.msid,
            "robot": list(self.robot_mm) if self.robot_mm else None,
            "cleaning": self.cleaning,
        }
        image = png(ow, oh, pixels, meta)
        if image == self.last_image and os.path.exists(self.out):
            return False
        with open(self.out + ".tmp", "wb") as f:
            f.write(image)
        os.replace(self.out + ".tmp", self.out)
        self.last_image = image
        return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5223)
    ap.add_argument(
        "--live", type=int, default=5, help="seconds between updates while cleaning"
    )
    ap.add_argument(
        "--idle", type=int, default=30, help="seconds between updates otherwise"
    )
    ap.add_argument(
        "--map-every",
        type=int,
        default=600,
        help="seconds between map checks when idle",
    )
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    state = MapState(args.out)
    conn, next_map = None, 0.0
    while True:
        try:
            if conn is None:
                conn = Xmpp(args.host, args.port, "deebotmap", "ecouser.net", "map")
            state.refresh_robot(conn, args.bot)
            state.refresh_walls(conn, args.bot)
            # While cleaning the robot extends its map: check pieces every minute, otherwise rarely.
            if time.time() >= next_map:
                state.refresh_map(conn, args.bot)
                next_map = time.time() + (60 if state.cleaning else args.map_every)
            if state.render():
                print(
                    time.strftime("%H:%M:%S"),
                    "map updated",
                    "(cleaning)" if state.cleaning else "",
                    flush=True,
                )
        except (
            Exception
        ) as exc:  # robot asleep or Bumper restarting: reconnect next round
            print(time.strftime("%H:%M:%S"), "map not read:", exc, flush=True)
            if conn:
                conn.close()
            conn = None
        if args.once:
            return
        time.sleep(args.live if state.cleaning else args.idle)


if __name__ == "__main__":
    sys.exit(main())
