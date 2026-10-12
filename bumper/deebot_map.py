"""Draw the map stored in an XMPP Deebot (OZMO 930 family) to a PNG, through the local Bumper.

Stdlib only, so it runs inside the add-on without new packages. It logs into Bumper's
XMPP server as a client, asks the bot for the map layout (GetMapM), downloads the pieces
that are not empty (PullMP, base64 LZMA), adds the virtual walls (GetMapSet tp=vw) and
the robot_at position, and writes the image only when the map changed.

Usage: deebot_map.py --bot <did>@<class>.ecorobot.net/atom --out /path/map.png [--every 600]
"""

import argparse
import base64
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

COLORS = {0: (255, 255, 255), 1: (186, 218, 255), 2: (40, 40, 60), 3: (186, 218, 255)}
WALL = (230, 60, 60)
ROBOT = (40, 170, 70)


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

    def _read_until(self, marker, timeout=20.0):
        end = time.time() + timeout
        while marker not in self.buf:
            if time.time() > end:
                raise TimeoutError(f"no {marker!r} from server")
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("server closed the stream")
            self.buf += chunk.decode(errors="replace")
        head, _, self.buf = self.buf.partition(marker)
        return head + marker

    def ctl(self, bot, body, timeout=15.0):
        """Send one ctl to the bot and return the attributes of its reply ctl."""
        cid = uuid.uuid4().hex[:8]
        me = f"{self.user}@{self.domain}/{self.resource}"
        body = body.replace("<ctl ", f'<ctl id="{cid}" ', 1)
        self._send(
            f'<iq type="set" id="m{cid}" from="{me}" to="{bot}"><query xmlns="com:ctl">{body}</query></iq>'
        )
        end = time.time() + timeout
        while time.time() < end:
            m = re.search(r"<ctl[^>]*id=['\"]" + cid + r"['\"]", self.buf)
            if m:
                tail = self.buf[m.start() :]
                close = tail.find("</iq>")
                if close >= 0:
                    reply = tail[:close]
                    self.buf = tail[close + 5 :]
                    return reply
            self.sock.settimeout(max(0.1, end - time.time()))
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                raise ConnectionError("server closed the stream")
            self.buf += chunk.decode(errors="replace")
        raise TimeoutError(f"no reply to {body[:40]}")

    def close(self):
        try:
            self._send("</stream:stream>")
            self.sock.close()
        except OSError:
            pass


def attrs(xml):
    head = xml.split("<ctl", 1)[1].split(">", 1)[0]
    return {
        k: a or b for k, a, b in re.findall(r"(\w+)=(?:\"([^\"]*)\"|'([^']*)')", head)
    }


def png(width, height, pixels):
    rows = b"".join(
        b"\x00" + bytes(pixels[y * width * 3 : (y + 1) * width * 3])
        for y in range(height)
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
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


def line(points_set, x0, y0, x1, y1):
    steps = max(abs(x1 - x0), abs(y1 - y0), 1)
    for i in range(steps + 1):
        points_set.add(
            (round(x0 + (x1 - x0) * i / steps), round(y0 + (y1 - y0) * i / steps))
        )


def draw(conn, bot, out, last_sig):
    info = attrs(conn.ctl(bot, '<ctl td="GetMapM" />'))
    w, h, rows, cols = int(info["w"]), int(info["h"]), int(info["r"]), int(info["c"])
    resolution = int(info.get("p", "50"))
    crcs = info["m"].split(",")
    walls_xml = conn.ctl(bot, '<ctl td="GetMapSet" tp="vw" />')
    # The 930 answers GetChargerPos with ret="fail"; GetPos gives the robot itself (mm, "x,y").
    try:
        pos_xml = conn.ctl(bot, '<ctl td="GetPos" />')
    except TimeoutError:
        pos_xml = ""
    sig = (
        info["i"] + info["m"] + walls_xml.split(">", 1)[-1] + pos_xml.split(">", 1)[-1]
    )
    if sig == last_sig and os.path.exists(out):
        return sig, False

    empty = max(set(crcs), key=crcs.count)
    width, height = cols * w, rows * h
    grid = bytearray(width * height)
    for pid, crc in enumerate(crcs):
        if crc == empty:
            continue
        reply = conn.ctl(bot, f'<ctl td="PullMP" pid="{pid}" />')
        raw = base64.b64decode(attrs(reply)["p"])
        data = lzma.decompress(
            raw[:9] + b"\x00\x00\x00\x00" + raw[9:], format=lzma.FORMAT_ALONE
        )
        pr, pc = divmod(pid, cols)
        for y in range(h):
            start = (pr * h + y) * width + pc * w
            grid[start : start + w] = data[y * w : (y + 1) * w]

    # Map cells are `resolution` mm wide; coordinates in walls and robot position are mm from the map centre.
    def cell(xmm, ymm):
        return int(width / 2 + float(xmm) / resolution), int(
            height / 2 + float(ymm) / resolution
        )

    walls = set()
    for item in re.findall(r"<m [^>]*p=['\"]([^'\"]+)", walls_xml):
        nums = [float(v) for v in item.split(",")]
        pts = [cell(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]
        for a, b in zip(pts, pts[1:]):
            line(walls, *a, *b)
    robot_at = None
    m = re.search(r"p=['\"](-?[\d.]+),(-?[\d.]+)", pos_xml)
    if m:
        robot_at = cell(m.group(1), m.group(2))

    used = [i for i, v in enumerate(grid) if v]
    if not used:
        return sig, False
    xs, ys = [i % width for i in used], [i // width for i in used]
    x0, x1, y0, y1 = min(xs) - 3, max(xs) + 3, min(ys) - 3, max(ys) + 3
    scale = 4
    ow, oh = (x1 - x0 + 1) * scale, (y1 - y0 + 1) * scale
    pixels = bytearray(ow * oh * 3)
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            if (x, y) in walls:
                color = WALL
            elif robot_at and abs(x - robot_at[0]) <= 2 and abs(y - robot_at[1]) <= 2:
                color = ROBOT
            elif 0 <= x < width and 0 <= y < height:
                color = COLORS.get(grid[y * width + x], COLORS[0])
            else:
                color = COLORS[0]
            for dy in range(scale):
                base = (((y - y0) * scale + dy) * ow + (x - x0) * scale) * 3
                pixels[base : base + 3 * scale] = bytes(color) * scale
    tmp = out + ".tmp"
    with open(tmp, "wb") as f:
        f.write(png(ow, oh, pixels))
    os.replace(tmp, out)
    return sig, True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5223)
    ap.add_argument(
        "--every", type=int, default=0, help="seconds between refreshes; 0 = once"
    )
    args = ap.parse_args()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    sig = None
    while True:
        conn = None
        try:
            conn = Xmpp(args.host, args.port, "deebotmap", "ecouser.net", "map")
            sig, changed = draw(conn, args.bot, args.out, sig)
            print(
                time.strftime("%H:%M:%S"),
                "map",
                "updated" if changed else "unchanged",
                flush=True,
            )
        except (
            Exception
        ) as exc:  # the robot can be asleep or away from the network: retry next round
            print(time.strftime("%H:%M:%S"), "map not read:", exc, flush=True)
        finally:
            if conn:
                conn.close()
        if not args.every:
            return
        time.sleep(args.every)


if __name__ == "__main__":
    sys.exit(main())
