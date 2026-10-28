#!/usr/bin/env python3
"""dimsheet: measure a UI component in a real browser and draw it as an Apple-style dimensional drawing.

    python dimsheet.py examples/card.html -o out                 # out/sheet.svg, out/sheet-motion.svg, out/measure.json
    python dimsheet.py https://example.com --select main -o out  # any page, any CSS selector
    python dimsheet.py --json out/measure.json -o out            # redraw from a saved measurement, no browser
    python dimsheet.py examples/card.html --png -o out           # also sheet.png + component.png side by side

The house rules and sheet layout follow enesozturk/dimensional-drawing (MIT): monochrome, slots instead of copy,
variable codes with a value table, nothing invented ([TBD]). The self-drawing loop follows lovisdotio/motion2code
(MIT): one self-contained SVG, one timeline, a seamless loop, a still frame for reduced motion.
Measuring uses the Chrome or Edge already installed; the drawing itself is pure standard library.
"""
import argparse
import base64
import datetime
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

MEASURE_JS = r"""(sel => {
  const root = document.querySelector(sel);
  if (!root) return null;
  const R0 = root.getBoundingClientRect();
  const px = v => Math.round((parseFloat(v) || 0) * 10) / 10;
  const box = (el, depth) => {
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    const kids = depth < 3 ? [...el.children].filter(k => {
      const b = k.getBoundingClientRect(), s = getComputedStyle(k);
      return b.width > 0 && b.height > 0 && s.position !== 'absolute' && s.position !== 'fixed';
    }).map(k => box(k, depth + 1)) : [];
    return {tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '', cls: String(el.className || ''),
      x: px(r.left - R0.left), y: px(r.top - R0.top), w: px(r.width), h: px(r.height),
      pad: [cs.paddingTop, cs.paddingRight, cs.paddingBottom, cs.paddingLeft].map(px),
      border: px(cs.borderTopWidth), radius: px(cs.borderTopLeftRadius), gap: px(cs.rowGap), colGap: px(cs.columnGap),
      display: cs.display, dir: cs.flexDirection, grow: px(cs.flexGrow), kids};
  };
  const t = box(root, 0), cs = getComputedStyle(root);
  t.shadow = cs.boxShadow === 'none' ? '' : cs.boxShadow;
  t.minW = cs.minWidth; t.maxW = cs.maxWidth;
  t.page = {x: R0.left + scrollX, y: R0.top + scrollY, vw: innerWidth};
  return t;
})"""


# ---------------------------------------------------------------- browser (DevTools protocol over a stdlib websocket)
def find_chrome():
    for name in ("chrome", "google-chrome", "chromium", "chromium-browser", "msedge", "microsoft-edge"):
        if p := shutil.which(name):
            return p
    for p in (os.environ.get("CHROME", ""), r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        if p and Path(p).exists():
            return p
    return None


class Chrome:
    def __init__(self, exe, width, height):
        self.prof = tempfile.mkdtemp()  # own profile: never touches the user's browser
        self.proc = subprocess.Popen([exe, "--headless=new", f"--user-data-dir={self.prof}", "--remote-debugging-port=0",
                                      "--no-first-run", "--hide-scrollbars", f"--window-size={width},{height}", "about:blank"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file, t0 = Path(self.prof, "DevToolsActivePort"), time.time()
        while not (text := self._port_text(port_file)).count("\n"):
            if time.time() - t0 > 60 or self.proc.poll() is not None:
                self.close()
                raise RuntimeError("Chrome did not start")
            time.sleep(0.05)
        port = int(text.split()[0])
        while not (pages := [p for p in json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=30))
                             if p["type"] == "page"]):
            if time.time() - t0 > 60:
                raise RuntimeError("Chrome opened no page")
            time.sleep(0.05)
        ws = pages[0]["webSocketDebuggerUrl"]
        self.sock, self.id = socket.create_connection(("127.0.0.1", port), timeout=120), 0
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(f"GET {ws.split(str(port), 1)[1]} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                          f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.sock.recv(1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("DevTools handshake failed")
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)

    @staticmethod
    def _port_text(path):
        try:
            return path.read_text()
        except OSError:  # missing, or still locked by Chrome while it writes it (Windows)
            return ""

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            if not (chunk := self.sock.recv(n - len(buf))):
                raise RuntimeError("DevTools connection closed")
            buf += chunk
        return buf

    def call(self, method, **params):
        self.id += 1
        data = json.dumps({"id": self.id, "method": method, "params": params}).encode()
        n, mask = len(data), os.urandom(4)
        size = bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + n.to_bytes(2, "big") if n < 65536 else \
            bytes([0x80 | 127]) + n.to_bytes(8, "big")
        self.sock.sendall(b"\x81" + size + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
        while True:  # skip events until our answer arrives
            msg = b""
            while True:
                b0, b1 = self._read(2)
                n = b1 & 0x7F
                n = int.from_bytes(self._read(2 if n == 126 else 8), "big") if n >= 126 else n
                msg += self._read(n)
                if b0 & 0x80:
                    break
            reply = json.loads(msg)
            if reply.get("id") == self.id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply["result"]

    def js(self, expr):
        return self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)["result"].get("value")

    def open(self, url):
        self.call("Page.navigate", url=url)
        t0 = time.time()
        while self.js("document.readyState") != "complete" and time.time() - t0 < 60:
            time.sleep(0.05)
        self.js("document.fonts.ready.then(() => 1)")

    def shot(self, path, x, y, w, h):
        img = self.call("Page.captureScreenshot", format="png", captureBeyondViewport=True,
                        clip={"x": x, "y": y, "width": w, "height": h, "scale": 1})
        Path(path).write_bytes(base64.b64decode(img["data"]))

    def close(self):
        try:
            self.call("Browser.close")
            self.sock.close()
        except Exception:
            pass
        try:
            self.proc.wait(3)
        except subprocess.TimeoutExpired:  # a throwaway headless profile: nothing to lose by not waiting longer
            self.proc.kill()
        shutil.rmtree(self.prof, ignore_errors=True)


def measure(src, select, width, chrome, shot=None):
    url = src if re.match(r"https?://", src) else Path(src).resolve().as_uri()
    c = Chrome(chrome, width, 900)
    try:
        c.open(url)
        tree = c.js(f"({MEASURE_JS})({json.dumps(select)})")
        if tree is None:
            raise SystemExit(f"dimsheet: nothing matches --select {select!r} in {src}")
        if shot:
            p, pad = tree["page"], 24  # a margin so the shadow is in the picture too
            c.shot(shot, max(0, p["x"] - pad), max(0, p["y"] - pad), tree["w"] + 2 * pad, tree["h"] + 2 * pad)
        return tree
    finally:
        c.close()


# ---------------------------------------------------------------- variables (house rule 3: codes, not numbers)
def num(v):
    return f"{v:g}"


def slot(b, parent):
    """(kind, size word) of a box: kind from the tag, HUG/FLEX/FILL from how it sits in its parent."""
    t, cls = b["tag"], b["cls"].lower()
    if t in ("img", "svg", "picture", "video", "canvas") or b["role"] == "img" or (not b["kids"] and re.search(r"avatar|icon|art|thumb", cls)):
        kind = "LEAD" if abs(b["w"] - b["h"]) < 1 else "MEDIA"
    elif t in ("button", "a") or b["role"] == "button":
        kind = "ACTION"
    elif t in ("input", "textarea", "select"):
        kind = "INPUT"
    elif re.fullmatch(r"h[1-6]", t) or "title" in cls:
        kind = "TITLE SLOT"
    elif t == "label":
        kind = "LABEL SLOT"
    elif b["kids"]:
        kind = "GROUP"
    else:
        kind = "CONTENT SLOT"
    if parent is None:
        return kind, ""
    inner = parent["w"] - parent["pad"][1] - parent["pad"][3] - 2 * parent["border"]
    size = "FILL" if abs(b["w"] - inner) < 1 else "FLEX" if b["grow"] > 0 else "HUG"
    return kind, size


def variables(tree):
    """Every distinct measured value gets one code, in reading order: S# spacing, H# heights, Z# squares, R# radii."""
    tabs = {"S": {}, "H": {}, "Z": {}, "R": {}}
    names = {"S": [], "H": [], "Z": [], "R": []}

    def add(prefix, v, name):
        if v and v > 0 and v not in tabs[prefix]:
            tabs[prefix][v] = f"{prefix}{len(tabs[prefix]) + 1}"
            names[prefix].append((tabs[prefix][v], name, v))
        return tabs[prefix].get(v)

    def walk(b, parent, depth):
        kind, _ = slot(b, parent)
        where = "container" if depth == 0 else kind.split()[0].lower()
        for v, side in zip(b["pad"], ("top", "right", "bottom", "left")):
            add("S", v, f"{where} pad-{side}")
        add("S", b["gap"] if b["display"].endswith("flex") and b["dir"].startswith("column") or "grid" in b["display"] else 0, f"{where} stack gap")
        add("S", b["colGap"] if b["display"].endswith("flex") and b["dir"].startswith("row") else 0, f"{where} inline gap")
        if kind == "LEAD":
            add("Z", b["w"], f"{where} size")
        elif depth == 1 or kind in ("ACTION", "INPUT", "MEDIA"):
            add("H", b["h"], f"{where} height")
        add("R", b["radius"], f"{where} radius")
        row = b["display"].endswith("flex") and b["dir"].startswith("row")
        a0, a1 = ("x", "w") if row else ("y", "h")
        ks = sorted(b["kids"], key=lambda k: k[a0])
        for k, nxt in zip(ks, ks[1:]):  # gaps as laid out (space-between, margins), not only the CSS gap
            g = round((nxt[a0] - k[a0] - k[a1]) * 2) / 2  # half-px: sub-pixel text boxes add noise
            add("S", g if g > 0.5 else 0, f"{where} {'inline' if row else 'stack'} gap")
        for k in b["kids"]:
            walk(k, b, depth + 1)

    walk(tree, None, 0)
    rows = {"Spacing": names["S"], "Size": names["Z"] + names["H"], "Radius": names["R"]}
    formulas = []
    r1, bd = tree["radius"], tree["border"]
    for k in tree["kids"]:  # nested corner: an inset surface whose radius is the outer radius minus border and inset
        inset = min(k["x"], k["y"]) - bd
        if k["radius"] > 0 and r1 > 0 and abs(k["radius"] - (r1 - bd - inset)) < 0.6 and inset in tabs["S"]:
            formulas.append(f"{tabs['R'][k['radius']]} = R1 − B − {tabs['S'][inset]} = {num(k['radius'])}")
    return tabs, rows, formulas


def shadow_parts(s):
    """'rgba(0, 0, 0, 0.08) 0px 8px 24px 0px' -> (Y, BL, SP) of the first shadow"""
    nums = re.findall(r"(-?[\d.]+)px", re.sub(r"rgba?\([^)]*\)", "", s.split("),")[0] if ")," in s else s))
    return tuple(float(v) for v in (nums + ["0"] * 4)[1:4]) if nums else None


# ---------------------------------------------------------------- drawing
CSS = """
svg{background:#fff;font-family:Arimo,Helvetica,Arial,sans-serif}
.sheet{fill:none;stroke:#111;stroke-width:1.2}.ol{fill:#fff;stroke:#111;stroke-width:1.4}
.grp{fill:none;stroke:#555;stroke-width:.8;stroke-dasharray:5 3}.sl{fill:#ececec;stroke:#333;stroke-width:.8}
.x{stroke:#666;stroke-width:.7}.ext{stroke:#777;stroke-width:.5}.dl{stroke:#111;stroke-width:.6}
.cl{stroke:#888;stroke-width:.5;stroke-dasharray:12 3 2 3}.lead{fill:none;stroke:#111;stroke-width:.6}
.dot{fill:#111}.thin{fill:none;stroke:#111;stroke-width:.6}.layer{fill:#fff;stroke:#111;stroke-width:.8}
.hatch{fill:url(#hatch);stroke:#666;stroke-width:.5}.nb{fill:#fff;stroke:#111;stroke-width:.7}
text{fill:#111}.dt{font-size:10px;letter-spacing:.4px}.dts{font-size:9px;fill:#444}.slt{font-size:8.5px;fill:#333;letter-spacing:.6px}
.vt{font-size:11px;font-weight:700;letter-spacing:1.2px}.nt{font-size:10px;letter-spacing:.3px}.nth{font-size:11px;font-weight:700}
.th{font-size:9px;font-weight:700}.tc{font-size:9.5px}.tbt{font-size:22px;font-weight:300;letter-spacing:3px}.tbk{font-size:7.5px;fill:#555}
"""


class Sheet:
    W, H = 1700, 1060

    def __init__(self):
        self.out, self.phase, self.phases = [], 0, 1

    def next(self):
        self.phase += 1
        self.phases = max(self.phases, self.phase + 1)

    def mk(self, tag, cls=None, text=None, **a):
        a = {k.rstrip("_").replace("_", "-"): (f"{v:.1f}".rstrip("0").rstrip(".") if isinstance(v, float) else v) for k, v in a.items()}
        c = " ".join(filter(None, [cls, f"p{self.phase}", "f" if tag == "text" or "marker-end" in a else "s"]))
        attrs = "".join(f' {k}="{html.escape(str(v))}"' for k, v in a.items())
        stroke = "" if tag == "text" else ' pathLength="1"'
        self.out.append(f'<{tag} class="{c}"{attrs}{stroke}>{html.escape(text)}</{tag}>' if text is not None
                        else f'<{tag} class="{c}"{attrs}{stroke}/>')

    def tx(self, x, y, s, cls="dt", **a):
        self.mk("text", cls, s, x=x, y=y, **a)

    def line(self, x1, y1, x2, y2, cls, **a):
        self.mk("line", cls, x1=x1, y1=y1, x2=x2, y2=y2, **a)

    def dim_h(self, x1, x2, y, e1, e2, label, below=False):
        for x, e in ((x1, e1), (x2, e2)):
            if e is not None:
                s = 1 if y > e else -1
                self.line(x, e + s * 3, x, y + s * 5, "ext")
        if x2 - x1 >= 28:
            self.line(x1, y, x2, y, "dl", marker_start="url(#arr)", marker_end="url(#arr)")
        else:
            self.line(x1 - 13, y, x1, y, "dl", marker_end="url(#arr)")
            self.line(x2 + 13, y, x2, y, "dl", marker_end="url(#arr)")
        self.tx((x1 + x2) / 2, y + 13 if below else y - 4, label, text_anchor="middle")

    def dim_v(self, y1, y2, x, e1, e2, label, side="l"):
        for y, e in ((y1, e1), (y2, e2)):
            if e is not None:
                s = 1 if x > e else -1
                self.line(e + s * 3, y, x + s * 5, y, "ext")
        if y2 - y1 >= 28:
            self.line(x, y1, x, y2, "dl", marker_start="url(#arr)", marker_end="url(#arr)")
        else:
            self.line(x, y1 - 13, x, y1, "dl", marker_end="url(#arr)")
            self.line(x, y2 + 13, x, y2, "dl", marker_end="url(#arr)")
        r = side == "r"
        self.tx(x + 6 if r else x - 6, (y1 + y2) / 2 + 3.5, label, text_anchor="start" if r else "end")

    def callout(self, px, py, ex, ey, label):
        s = 1 if ex >= px else -1
        self.mk("circle", "dot", cx=px, cy=py, r=1.8)
        self.mk("polyline", "lead", points=f"{px:.1f},{py:.1f} {ex:.1f},{ey:.1f} {ex + s * 10:.1f},{ey:.1f}")
        self.tx(ex + s * 14, ey + 3.5, label, text_anchor="start" if s > 0 else "end")

    def nb(self, x, y, n):
        self.mk("rect", "nb", x=x - 6, y=y - 6, width=12, height=12)
        self.tx(x, y + 3.5, str(n), "dts", text_anchor="middle")

    def table(self, x, y, title, rows):
        ws, rh = (44, 132, 84), 17
        self.tx(x, y - 8, title.upper(), "vt")
        for i, r in enumerate([("CODE", "VARIABLE", "VALUE")] + rows):
            cx = x
            for j, c in enumerate(r):
                self.mk("rect", "thin", x=cx, y=y + i * rh, width=ws[j], height=rh)
                self.tx(cx + 6, y + i * rh + 12, str(c), "th" if i == 0 else "tc")
                cx += ws[j]
        return y + (len(rows) + 1) * rh + 34

    def svg(self, motion=False, seconds=10.0):
        css = CSS
        if motion:  # every phase draws in its own slice of one timeline, holds, then everything fades for a seamless loop
            n = self.phases
            for k in range(n):
                a, b = 4 + 70 * k / n, 4 + 70 * (k + 1) / n
                css += (f".anim .s.p{k}{{stroke-dasharray:1;animation:d{k} {seconds}s linear infinite}}"
                        f".anim .f.p{k}{{animation:t{k} {seconds}s linear infinite}}"
                        f"@keyframes d{k}{{0%,{a:.1f}%{{stroke-dashoffset:1;fill-opacity:0;opacity:1}}"
                        f"{b:.1f}%,90%{{stroke-dashoffset:0;fill-opacity:1;opacity:1}}97%,100%{{stroke-dashoffset:0;fill-opacity:1;opacity:0}}}}"
                        f"@keyframes t{k}{{0%,{a:.1f}%{{opacity:0}}{b:.1f}%,90%{{opacity:1}}97%,100%{{opacity:0}}}}")
            css += "@media (prefers-reduced-motion:reduce){.anim *{animation:none!important}}"
        defs = ('<defs><marker id="arr" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
                '<path d="M0,1.5 L10,5 L0,8.5 z" fill="#111"/></marker><pattern id="hatch" width="6" height="6" '
                'patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="6" stroke="#999" stroke-width=".7"/></pattern></defs>')
        cls = ' class="anim"' if motion else ""
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.W} {self.H}" width="{self.W}" height="{self.H}"'
                f'{cls}><style>{css}</style>{defs}\n' + "\n".join(self.out) + "\n</svg>\n")


def draw(tree, name="Component", source="", date=None):
    tabs, rows, formulas = variables(tree)
    S, Hc, Z, Rc = tabs["S"], tabs["H"], tabs["Z"], tabs["R"]
    code = lambda v, d: d.get(v, num(v))
    sh = Sheet()
    sh.mk("rect", "sheet", x=20, y=20, width=sh.W - 40, height=sh.H - 60, rx=16)
    kind = slot(tree, None)[0]
    dwg = "UI-" + (re.sub(r"[^A-Z]", "", name.upper())[:6] or "COMP") + "-001"

    # notes, top left
    sh.next()
    sh.tx(50, 60, "NOTES:", "nth")
    sh.tx(108, 60, "(UNLESS OTHERWISE SPECIFIED)", "nt")
    notes = [(1, "DO NOT SCALE DRAWING."),
             (2, f"DIMENSIONS IN CSS PX, MEASURED IN A {tree['page']['vw']} PX VIEWPORT."),
             (3, "SLOTS ARE PLACEHOLDERS; COPY AND CONTENT ARE OUT OF SCOPE."),
             (4, "HUG = SIZED BY CONTENT. FLEX = TAKES REMAINING SPACE. FILL = PARENT WIDTH."),
             (5, "CODES REFER TO THE TABLES; DERIVED VALUES ARE GIVEN AS FORMULAS."),
             (6, "VALUES NOT FOUND IN THE SOURCE ARE [TBD]. GRAYSCALE ONLY.")]
    for i, (n, t) in enumerate(notes):
        sh.nb(56, 86 + i * 22, n)
        sh.tx(72, 90 + i * 22, t, "nt")

    # plan view, top centre, fitted into its box
    W, H = tree["w"], tree["h"]
    s = min(1.5, 600 / W, 450 / H)
    X0, Y0 = 560 + (600 - W * s) / 2, 150 + (460 - H * s) / 2
    P = lambda b: (X0 + b["x"] * s, Y0 + b["y"] * s, b["w"] * s, b["h"] * s)
    sh.next()
    sh.tx(X0 + W * s / 2, Y0 + H * s + 92, "PLAN VIEW", "vt", text_anchor="middle")
    sh.tx(X0 + W * s / 2, Y0 + H * s + 106, f"SCALE {num(round(s, 2))} : 1", "dts", text_anchor="middle")
    sh.line(X0 + W * s / 2, Y0 - 14, X0 + W * s / 2, Y0 + H * s + 14, "cl")
    sh.line(X0 - 14, Y0 + H * s / 2, X0 + W * s + 14, Y0 + H * s / 2, "cl")
    sh.mk("rect", "ol", x=X0, y=Y0, width=W * s, height=H * s, rx=tree["radius"] * s)

    sh.next()
    leaves, labels = [], []

    def boxes(b, parent):
        for k in b["kids"]:
            kd, size = slot(k, b)
            x, y, w, h = P(k)
            if kd == "GROUP":
                sh.mk("rect", "grp", x=x, y=y, width=w, height=h, rx=k["radius"] * s)
                boxes(k, k)
                continue
            sh.mk("rect", "sl", x=x, y=y, width=w, height=h, rx=k["radius"] * s)
            if kd in ("LEAD", "MEDIA"):
                sh.line(x, y, x + w, y + h, "x")
                sh.line(x + w, y, x, y + h, "x")
            leaves.append(k)
            if w > 46 and h > 12:
                labels.append((x + w / 2, y + h / 2 + 3, f"{kd} · {size}" if w > 120 and kd not in ("LEAD", "MEDIA") else kd.split()[0]))

    boxes(tree, tree)
    for x, y, t in labels:
        sh.tx(x, y, t, "slt", text_anchor="middle")

    # dimensions
    sh.next()
    pt, pr, pb, pl = tree["pad"]
    top = Y0 - 34
    if pl:
        sh.dim_h(X0, X0 + pl * s, top, Y0, Y0, S.get(pl, num(pl)))
    if pr:
        sh.dim_h(X0 + (W - pr) * s, X0 + W * s, top, Y0, Y0, S.get(pr, num(pr)))
    if pl + pr and W - pl - pr > 30:
        sh.dim_h(X0 + pl * s, X0 + (W - pr) * s, top, None, None, "FILL")
    wl = f"W = {num(W)}" + (f" ({tree['minW']}–{tree['maxW']})".replace("px", "") if tree.get("maxW", "none") != "none" else "")
    sh.dim_h(X0, X0 + W * s, Y0 - 66, Y0, Y0, wl)
    # left chain: padding, each region, the gaps between them
    lx, y = X0 - 34, 0.0
    marks = [(0, pt, S.get(pt, num(pt)))] if pt else []
    kids = sorted(tree["kids"], key=lambda k: k["y"])
    for i, k in enumerate(kids):
        marks.append((k["y"], k["y"] + k["h"], code(k["h"], Hc)))
        if i + 1 < len(kids) and kids[i + 1]["y"] - (k["y"] + k["h"]) > 0.5:
            g = round((kids[i + 1]["y"] - k["y"] - k["h"]) * 2) / 2
            marks.append((k["y"] + k["h"], kids[i + 1]["y"], code(g, S)))
    if pb:
        marks.append((H - pb, H, S.get(pb, num(pb))))
    for a, b, lab in marks:
        sh.dim_v(Y0 + a * s, Y0 + b * s, lx, X0, X0, lab)
    sh.dim_v(Y0, Y0 + H * s, X0 + W * s + 40, X0 + W * s, X0 + W * s, f"(REF {num(H)})", side="r")
    # bottom chain: the first row that holds several slots (a header, a footer, a control bar)
    row = next((k for k in kids if k["display"].endswith("flex") and k["dir"].startswith("row") and len(k["kids"]) > 1), None)
    if row:
        by = Y0 + H * s + 40
        items = sorted(row["kids"], key=lambda k: k["x"])
        for i, k in enumerate(items):
            kd, size = slot(k, row)
            lab = code(k["w"], Z) if kd == "LEAD" else size if size != "HUG" or kd != "ACTION" else code(k["h"], Hc) + " · HUG"
            sh.dim_h(X0 + k["x"] * s, X0 + (k["x"] + k["w"]) * s, by, Y0 + (k["y"] + k["h"]) * s, Y0 + (k["y"] + k["h"]) * s, lab, below=True)
            if i + 1 < len(items) and items[i + 1]["x"] - k["x"] - k["w"] > 0.5:
                g = round((items[i + 1]["x"] - k["x"] - k["w"]) * 2) / 2
                sh.dim_h(X0 + (k["x"] + k["w"]) * s, X0 + items[i + 1]["x"] * s, by + 22, None, None, code(g, S), below=True)

    # radius and border callouts
    sh.next()
    seen, marks = set(), []
    for b in [tree] + leaves + [k for k in kids if k not in leaves]:
        if b["radius"] > 0 and b["radius"] not in seen:
            seen.add(b["radius"])
            x, y, w, h = P(b)
            r = min(b["radius"] * s, w / 2, h / 2)
            marks.append((y + r - r * 0.7071, x + w - r + r * 0.7071, f"{Rc[b['radius']]}  R{num(b['radius'])}"))
    if tree["border"]:
        marks.append((Y0 + H * s * 0.9, X0 + W * s, f"B = {num(tree['border'])}"))
    ey = -1e9
    for py, px, lab in sorted(marks):  # leaders fan out to the right without crossing
        ey = max(py, ey + 18)
        sh.callout(px, py, X0 + W * s + 110, ey, lab)
    sh.nb(X0 + W * s + 16, Y0 - 12, 2)
    if labels:
        sh.nb(labels[0][0] - len(labels[0][2]) * 2.6 - 14, labels[0][1] - 3, 3)

    # exploded layers, bottom left
    sh.next()
    L, D, bx, base = 250, 120, 210, 1000
    layers = [("CANVAS", None)] + ([("SHADOW", "hatch")] if tree.get("shadow") else []) + [("CONTAINER", "ol")]
    layers += [("SLOTS & CONTROLS", "slots")]
    sh.tx(bx + (L + D) / 2, base - 30 - len(layers) * 46 - D * 0.45, "EXPLODED VIEW · LAYERS", "vt", text_anchor="middle")
    for i, (lab, kind_) in enumerate(layers):
        y0 = base - 40 - i * 46
        q = lambda u, v: f"{bx + u * L + v * D:.1f},{y0 - v * D * 0.45:.1f}"
        sh.mk("polygon", "hatch" if kind_ == "hatch" else "layer" if kind_ else "thin", points=" ".join(q(u, v) for u, v in ((0, 0), (1, 0), (1, 1), (0, 1))))
        if kind_ == "slots":
            for k in leaves:
                u0, v0 = k["x"] / W, 1 - (k["y"] + k["h"]) / H
                u1, v1 = (k["x"] + k["w"]) / W, 1 - k["y"] / H
                sh.mk("polygon", "sl", points=" ".join(q(u, v) for u, v in ((u0, v0), (u1, v0), (u1, v1), (u0, v1))))
        sh.callout(bx + 6, y0 - 4, bx - 30, y0 - 4, lab)

    # tables, right column
    sh.next()
    ty = 70
    for title, rws in rows.items():
        if rws:
            ty = sh.table(1340, ty, title, [(c, n_, num(v)) for c, n_, v in rws])
    sz = [("B", "border", num(tree["border"]) if tree["border"] else "0"), ("W", "width", num(W)),
          ("W-MIN", "min-width", tree.get("minW", "auto").replace("px", "") if tree.get("minW", "auto") not in ("auto", "0px") else "[TBD]"),
          ("W-MAX", "max-width", tree.get("maxW", "none").replace("px", "") if tree.get("maxW", "none") != "none" else "[TBD]")]
    ty = sh.table(1340, ty, "Container", sz)
    if (sp := shadow_parts(tree.get("shadow", ""))):
        ty = sh.table(1340, ty, "Shadow", [("Y", "offset-y", num(sp[0])), ("BL", "blur", num(sp[1])), ("SP", "spread", num(sp[2])),
                                            ("E1", "envelope top", f"BL+SP−Y = {num(sp[1] + sp[2] - sp[0])}"),
                                            ("E3", "envelope bottom", f"BL+SP+Y = {num(sp[1] + sp[2] + sp[0])}")])
    for i, f in enumerate(formulas):
        sh.tx(1340, ty - 14 + i * 16, f, "tc")

    # title block, bottom right, and footer
    sh.next()
    tbx, tby = 1080, 916
    sh.mk("rect", "thin", x=tbx, y=tby, width=580, height=78)
    for x in (tbx + 330, tbx + 420, tbx + 500):
        sh.line(x, tby, x, tby + 78, "thin")
    sh.line(tbx + 330, tby + 39, tbx + 580, tby + 39, "thin")
    sh.tx(tbx + 14, tby + 34, name.upper()[:22], "tbt")
    sh.tx(tbx + 14, tby + 58, "UI component reference · architecture only", "dts")
    for (x, y, k, v) in ((tbx + 336, tby + 12, "DWG NO.", dwg), (tbx + 426, tby + 12, "UNITS", "CSS PX"), (tbx + 506, tby + 12, "REV", "A"),
                         (tbx + 336, tby + 51, "SCALE", f"{num(round(s, 2))}:1"), (tbx + 426, tby + 51, "SHEETS", "1 OF 1"),
                         (tbx + 506, tby + 51, "SOURCE", Path(source).name[:12] if source else "[TBD]")):
        sh.tx(x, y, k, "tbk")
        sh.tx(x, y + 16, v, "tc")
    date = date or datetime.date.today().isoformat()
    sh.tx(40, sh.H - 22, f"{date} | UI COMPONENT REFERENCE", "dts")
    sh.tx(sh.W - 40, sh.H - 22, f"{name.upper()}, ARCHITECTURE | 1", "dts", text_anchor="end")
    return sh


# ---------------------------------------------------------------- cli
def main(argv=None):
    ap = argparse.ArgumentParser(description="Measure a UI component in a real browser and draw an Apple-style dimensional drawing.")
    ap.add_argument("src", nargs="?", help="HTML file or http(s) URL")
    ap.add_argument("--select", default="body > *", help="CSS selector of the component (default: first element in <body>)")
    ap.add_argument("--name", help="name in the title block (default: from the file name)")
    ap.add_argument("--width", type=int, default=1280, help="viewport width for measuring (default 1280)")
    ap.add_argument("--json", help="draw from a saved measure.json instead of opening a browser")
    ap.add_argument("--png", action="store_true", help="also write sheet.png and component.png (needs Chrome or Edge)")
    ap.add_argument("--seconds", type=float, default=10.0, help="length of the self-drawing loop (default 10)")
    ap.add_argument("--date", help="date in the footer (default: today)")
    ap.add_argument("-o", "--out", default="out")
    a = ap.parse_args(argv)
    if not (a.src or a.json):
        ap.error("give an HTML file or URL, or --json measure.json")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    chrome = find_chrome()
    if a.json:
        tree = json.loads(Path(a.json).read_text(encoding="utf-8"))
    else:
        if not chrome:
            sys.exit("dimsheet: measuring needs Chrome or Edge (or set CHROME=/path/to/chrome); --json redraws without one")
        tree = measure(a.src, a.select, a.width, chrome, out / "component.png" if a.png else None)
        (out / "measure.json").write_text(json.dumps(tree, indent=1), encoding="utf-8")
    source = a.src or tree.get("source", "")
    tree["source"] = source
    name = a.name or re.sub(r"[-_]+", " ", Path(source.split("?")[0].rstrip("/")).stem or "component").strip().title()
    sh = draw(tree, name, source, a.date)
    (out / "sheet.svg").write_text(sh.svg(), encoding="utf-8")
    (out / "sheet-motion.svg").write_text(sh.svg(motion=True, seconds=a.seconds), encoding="utf-8")
    if a.png and chrome:
        c = Chrome(chrome, Sheet.W, Sheet.H)
        try:
            c.open((out / "sheet.svg").resolve().as_uri())
            c.shot(out / "sheet.png", 0, 0, Sheet.W, Sheet.H)
        finally:
            c.close()
    _, rows, formulas = variables(tree)
    count = sum(len(r) for r in rows.values())
    print(f"dimsheet: {name}: {num(tree['w'])} x {num(tree['h'])} px, {count} variables"
          + (f", {len(formulas)} derived" if formulas else "") + f" -> {out / 'sheet.svg'}, {out / 'sheet-motion.svg'}")


if __name__ == "__main__":
    main()
