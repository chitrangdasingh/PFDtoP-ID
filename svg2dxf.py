"""
svg2dxf.py - convert the P&ID drawing sheet (SVG) into a DXF file that AutoCAD, BricsCAD, DraftSight and LibreCAD open.

The DXF is AutoCAD R12 ASCII (AC1009), the most widely readable version. Geometry is written on named layers so the
drawing can be edited like a normal CAD P&ID:
  PROCESS (main process lines)    SOLIDS (solids transfer, dashed)    UTILITY (cooling water)
  SIGNAL (instrument signals, dashed)    TRIP (safety trip signals, red dashed)    INST-CONN (instrument connections)
  EQUIPMENT    VALVES    INSTRUMENTS    MOTORS    TEXT    BORDER (frame, tables, title block)
  REMARKS (open review remark flags)    REVISIONS (revision triangles on corrected items)
Curves (arcs, circles in symbols, quadratic curves) are flattened to polylines except whole circles, which stay CIRCLE.
Filled triangles and quadrilaterals (arrowheads, check valve halves) become SOLID entities.
Units: drawing points (1 pt = 1 unit). Scale in AutoCAD with SCALE if a different unit is wanted.

Run:  python svg2dxf.py out_aspen_10tpd/pid_sheet.svg out_aspen_10tpd/pid_sheet.dxf
"""
import math
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

NS = "{http://www.w3.org/2000/svg}"
LAYERS = {  # name: (AutoCAD colour index, linetype)
    "PROCESS": (7, "CONTINUOUS"), "SOLIDS": (30, "DASHED"), "UTILITY": (5, "CONTINUOUS"), "SIGNAL": (6, "DASHED"),
    "TRIP": (1, "DASHED"), "INST-CONN": (8, "CONTINUOUS"), "EQUIPMENT": (7, "CONTINUOUS"), "VALVES": (3, "CONTINUOUS"),
    "INSTRUMENTS": (4, "CONTINUOUS"), "MOTORS": (2, "CONTINUOUS"), "TEXT": (7, "CONTINUOUS"), "BORDER": (7, "CONTINUOUS"),
    "REMARKS": (1, "CONTINUOUS"), "REVISIONS": (5, "CONTINUOUS"),
}
CLASS_FONT = {"tag": 7.5, "ln": 6, "l": 6.4, "s": 6, "tt": 11, "hd": 7.5, "z": 8, "t": 11, "b": 17}
CLASS_ANCHOR = {"l": "start", "hd": "start", "c": "middle"}


# ---------------------------------------------------------------------------- polygon fill (ear clipping)
def triangulate(P):
    """Simple polygon -> triangles (ear clipping). R12 has no HATCH, so filled shapes become SOLID triangles."""
    pts = [q for i, q in enumerate(P) if i == 0 or abs(q[0] - P[i - 1][0]) + abs(q[1] - P[i - 1][1]) > 1e-6]
    if len(pts) > 3 and abs(pts[0][0] - pts[-1][0]) + abs(pts[0][1] - pts[-1][1]) < 1e-6:
        pts.pop()
    area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1]))
    if area < 0:
        pts = pts[::-1]
    cross = lambda o, a, b: (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    def inside(q, a, b, c):
        return cross(a, b, q) >= 0 and cross(b, c, q) >= 0 and cross(c, a, q) >= 0
    tris, idx, guard = [], list(range(len(pts))), 0
    while len(idx) > 3 and guard < 10000:
        guard += 1
        for k in range(len(idx)):
            i0, i1, i2 = idx[k - 1], idx[k], idx[(k + 1) % len(idx)]
            a, b, c = pts[i0], pts[i1], pts[i2]
            if cross(a, b, c) <= 1e-9:
                continue
            if any(inside(pts[j], a, b, c) for j in idx if j not in (i0, i1, i2)):
                continue
            tris.append([a, b, c])
            idx.pop(k)
            break
        else:
            break
    if len(idx) == 3:
        tris.append([pts[i] for i in idx])
    return tris


# ---------------------------------------------------------------------------- 2-D affine transforms
def mul(a, b):
    return (a[0] * b[0] + a[2] * b[1], a[1] * b[0] + a[3] * b[1], a[0] * b[2] + a[2] * b[3], a[1] * b[2] + a[3] * b[3],
            a[0] * b[4] + a[2] * b[5] + a[4], a[1] * b[4] + a[3] * b[5] + a[5])


IDENT = (1, 0, 0, 1, 0, 0)


def parse_transform(t):
    m = IDENT
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", t or ""):
        v = [float(x) for x in re.split(r"[\s,]+", args.strip()) if x]
        if name == "translate":
            m = mul(m, (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0))
        elif name == "scale":
            m = mul(m, (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0))
        elif name == "rotate":
            a = math.radians(v[0])
            r = (math.cos(a), math.sin(a), -math.sin(a), math.cos(a), 0, 0)
            if len(v) == 3:
                m = mul(m, (1, 0, 0, 1, v[1], v[2]))
                m = mul(m, r)
                m = mul(m, (1, 0, 0, 1, -v[1], -v[2]))
            else:
                m = mul(m, r)
        elif name == "matrix":
            m = mul(m, tuple(v))
    return m


def apply(m, x, y):
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def scale_of(m):
    return math.sqrt(abs(m[0] * m[3] - m[1] * m[2]))


def angle_of(m):
    return math.degrees(math.atan2(m[1], m[0]))


# ---------------------------------------------------------------------------- path flattening
def _arc(x1, y1, rx, ry, phi, fa, fs, x2, y2, n=16):
    """SVG endpoint arc -> points (excluding the start point)."""
    if rx == 0 or ry == 0:
        return [(x2, y2)]
    p = math.radians(phi)
    cp, sp = math.cos(p), math.sin(p)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
    rx, ry = abs(rx), abs(ry)
    lam = (x1p ** 2) / rx ** 2 + (y1p ** 2) / ry ** 2
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    co = math.sqrt(max(0, num / den)) if den else 0
    if fa == fs:
        co = -co
    cxp, cyp = co * rx * y1p / ry, -co * ry * x1p / rx
    cx, cy = cp * cxp - sp * cyp + (x1 + x2) / 2, sp * cxp + cp * cyp + (y1 + y2) / 2
    ang = lambda ux, uy, vx, vy: math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
    t1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not fs and dt > 0:
        dt -= 2 * math.pi
    elif fs and dt < 0:
        dt += 2 * math.pi
    pts = []
    for i in range(1, n + 1):
        t = t1 + dt * i / n
        x, y = rx * math.cos(t), ry * math.sin(t)
        pts.append((cp * x - sp * y + cx, sp * x + cp * y + cy))
    return pts


def path_to_polylines(d):
    """Path data -> list of (points, closed)."""
    toks = re.findall(r"[MmLlHhVvZzAaQqTtCcSs]|-?\d*\.?\d+(?:e-?\d+)?", d)
    polys, cur, i = [], [], 0
    x = y = sx = sy = 0.0
    cmd, last_ctrl = None, None

    def num():
        nonlocal i
        v = float(toks[i])
        i += 1
        return v
    while i < len(toks):
        if re.match(r"[A-Za-z]", toks[i]):
            cmd = toks[i]
            i += 1
            if cmd in "Zz":
                if cur:
                    polys.append((cur, True))
                cur, x, y = [], sx, sy
                continue
        if cmd is None:
            break
        rel = cmd.islower()
        c = cmd.upper()
        if c == "M":
            nx, ny = num(), num()
            if rel:
                nx, ny = x + nx, y + ny
            if len(cur) > 1:
                polys.append((cur, False))
            x, y, sx, sy = nx, ny, nx, ny
            cur = [(x, y)]
            cmd = "l" if rel else "L"
        elif c == "L":
            nx, ny = num(), num()
            x, y = (x + nx, y + ny) if rel else (nx, ny)
            cur.append((x, y))
        elif c == "H":
            nx = num()
            x = x + nx if rel else nx
            cur.append((x, y))
        elif c == "V":
            ny = num()
            y = y + ny if rel else ny
            cur.append((x, y))
        elif c == "A":
            rx, ry, phi, fa, fs, nx, ny = num(), num(), num(), num(), num(), num(), num()
            if rel:
                nx, ny = x + nx, y + ny
            cur += _arc(x, y, rx, ry, phi, int(fa), int(fs), nx, ny)
            x, y = nx, ny
        elif c in "QT":
            if c == "Q":
                qx, qy = num(), num()
                if rel:
                    qx, qy = x + qx, y + qy
            else:
                qx, qy = (2 * x - last_ctrl[0], 2 * y - last_ctrl[1]) if last_ctrl else (x, y)
            nx, ny = num(), num()
            if rel:
                nx, ny = x + nx, y + ny
            for k in range(1, 9):
                t = k / 8
                cur.append(((1 - t) ** 2 * x + 2 * (1 - t) * t * qx + t * t * nx, (1 - t) ** 2 * y + 2 * (1 - t) * t * qy + t * t * ny))
            last_ctrl = (qx, qy)
            x, y = nx, ny
            continue
        elif c in "CS":
            if c == "C":
                c1x, c1y = num(), num()
                if rel:
                    c1x, c1y = x + c1x, y + c1y
            else:
                c1x, c1y = (2 * x - last_ctrl[0], 2 * y - last_ctrl[1]) if last_ctrl else (x, y)
            c2x, c2y, nx, ny = num(), num(), num(), num()
            if rel:
                c2x, c2y, nx, ny = x + c2x, y + c2y, x + nx, y + ny
            for k in range(1, 11):
                t = k / 10
                a, b, cc, dd = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3
                cur.append((a * x + b * c1x + cc * c2x + dd * nx, a * y + b * c1y + cc * c2y + dd * ny))
            last_ctrl = (c2x, c2y)
            x, y = nx, ny
            continue
        last_ctrl = None
    if len(cur) > 1:
        polys.append((cur, False))
    return polys


# ---------------------------------------------------------------------------- DXF writer
class DXF:
    def __init__(self, height):
        self.H = height
        self.e = []

    def _xy(self, x, y, code=10):
        return [f"{code}", f"{x:.3f}", f"{code + 10}", f"{self.H - y:.3f}", f"{code + 20}", "0.0"]

    def line(self, layer, a, b):
        self.e += ["0", "LINE", "8", layer] + self._xy(*a) + self._xy(*b, code=11)

    def polyline(self, layer, pts, closed=False):
        if len(pts) == 2 and not closed:
            return self.line(layer, *pts)
        self.e += ["0", "POLYLINE", "8", layer, "66", "1", "10", "0.0", "20", "0.0", "30", "0.0", "70", "1" if closed else "0"]
        for x, y in pts:
            self.e += ["0", "VERTEX", "8", layer] + self._xy(x, y)
        self.e += ["0", "SEQEND", "8", layer]

    def circle(self, layer, c, r):
        self.e += ["0", "CIRCLE", "8", layer] + self._xy(*c) + ["40", f"{r:.3f}"]

    def solid(self, layer, pts):
        p = list(pts) + [pts[-1]] * (4 - len(pts))
        order = [p[0], p[1], p[3], p[2]]           # DXF SOLID vertex order
        self.e += ["0", "SOLID", "8", layer]
        for k, (x, y) in enumerate(order):
            self.e += self._xy(x, y, code=10 + k)

    def text(self, layer, pos, h, s, rot=0.0, anchor="middle"):
        s = s.replace("\n", " ").strip()
        if not s:
            return
        ha = {"start": 0, "middle": 1, "end": 2}.get(anchor, 1)
        self.e += ["0", "TEXT", "8", layer] + self._xy(*pos) + ["40", f"{h:.3f}", "1", s, "50", f"{rot:.3f}", "7", "CAD", "72", str(ha)]
        if ha:
            self.e += self._xy(*pos, code=11)

    def write(self, path, width):
        out = ["0", "SECTION", "2", "HEADER", "9", "$ACADVER", "1", "AC1009", "9", "$INSUNITS", "70", "0",
               "9", "$EXTMIN", "10", "0.0", "20", "0.0", "30", "0.0", "9", "$EXTMAX", "10", f"{width:.1f}", "20", f"{self.H:.1f}", "30", "0.0",
               "9", "$LTSCALE", "40", "4.0", "0", "ENDSEC",
               "0", "SECTION", "2", "TABLES",
               "0", "TABLE", "2", "LTYPE", "70", "3",
               "0", "LTYPE", "2", "CONTINUOUS", "70", "0", "3", "Solid line", "72", "65", "73", "0", "40", "0.0",
               "0", "LTYPE", "2", "DASHED", "70", "0", "3", "__ __ __", "72", "65", "73", "2", "40", "1.5", "49", "1.0", "49", "-0.5",
               "0", "LTYPE", "2", "DASHDOT", "70", "0", "3", "__ . __", "72", "65", "73", "4", "40", "2.0", "49", "1.2", "49", "-0.3", "49", "0.0", "49", "-0.5",
               "0", "ENDTAB",
               "0", "TABLE", "2", "LAYER", "70", str(len(LAYERS) + 1),
               "0", "LAYER", "2", "0", "70", "0", "62", "7", "6", "CONTINUOUS"]
        for name, (col, lt) in LAYERS.items():
            out += ["0", "LAYER", "2", name, "70", "0", "62", str(col), "6", lt]
        out += ["0", "ENDTAB",
                "0", "TABLE", "2", "STYLE", "70", "1",
                "0", "STYLE", "2", "CAD", "70", "0", "40", "0.0", "41", "0.85", "50", "0.0", "71", "0", "42", "2.5", "3", "romans.shx", "4", "",
                "0", "ENDTAB", "0", "ENDSEC",
                "0", "SECTION", "2", "ENTITIES"] + self.e + ["0", "ENDSEC", "0", "EOF"]
        Path(path).write_text("\n".join(out) + "\n", encoding="ascii", errors="replace")
        return len([x for x in self.e if x in ("LINE", "POLYLINE", "CIRCLE", "SOLID", "TEXT")])


# ---------------------------------------------------------------------------- SVG walk
def _style(el):
    st = {}
    for part in (el.get("style") or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            st[k.strip()] = v.strip()
    return st


def _layer(el, ctx):
    cls = set((el.get("class") or "").split())
    if cls & {"hit", "hitbox", "cell"}:
        return None if cls & {"hit", "hitbox"} else "BORDER"
    if el.get("stroke") == "#c00" or ctx.get("stroke") == "#c00":
        return "TRIP"
    for c, layer in (("proc", "PROCESS"), ("solids", "SOLIDS"), ("util", "UTILITY"), ("sig", "SIGNAL"), ("thin", "INST-CONN"),
                     ("proch", "PROCESS"), ("solidsh", "SOLIDS"), ("utilh", "UTILITY")):
        if c in cls:
            return layer
    return ctx.get("layer", "BORDER")


def convert(svg_path, dxf_path):
    root = ET.parse(svg_path).getroot()
    W = float(root.get("width") or root.get("viewBox").split()[2])
    H = float(root.get("height") or root.get("viewBox").split()[3])
    dxf = DXF(H)

    def walk(el, m, ctx):
        tag = el.tag.replace(NS, "")
        if tag in ("style", "title", "desc", "defs"):
            return
        m = mul(m, parse_transform(el.get("transform")))
        cls = set((el.get("class") or "").split())
        ctx = dict(ctx)
        for c, layer in (("eq", "EQUIPMENT"), ("vlv", "VALVES"), ("ins", "INSTRUMENTS"), ("mot", "MOTORS"), ("rmk", "REMARKS"), ("rev", "REVISIONS")):
            if c in cls:
                ctx["layer"] = layer
        if el.get("stroke"):
            ctx["stroke"] = el.get("stroke")
        if tag == "svg" and el is not root:          # nested symbol viewport (legend sheet)
            x, y, w, h = (float(el.get(k, 0)) for k in ("x", "y", "width", "height"))
            vb = [float(v) for v in el.get("viewBox").split()]
            sc = min(w / vb[2], h / vb[3])
            m = mul(m, (1, 0, 0, 1, x + (w - vb[2] * sc) / 2, y + (h - vb[3] * sc) / 2))
            m = mul(m, (sc, 0, 0, sc, -vb[0] * sc, -vb[1] * sc))
            ctx.setdefault("layer", "INSTRUMENTS")
        layer = _layer(el, ctx)
        filled = "f" in cls or "proch" in cls or "solidsh" in cls or "utilh" in cls
        if layer is not None:
            if tag == "path":
                for pts, closed in path_to_polylines(el.get("d", "")):
                    P = [apply(m, *q) for q in pts]
                    if filled and 3 <= len(P) <= 4:
                        dxf.solid(layer, P)
                    elif filled and len(P) > 4 and "logo-f" in cls:     # logo: fill with triangles, keep the outline
                        for tri in triangulate(P):
                            dxf.solid(layer, tri)
                        dxf.polyline(layer, P, True)
                    else:
                        dxf.polyline(layer, P, closed)
            elif tag == "rect":
                x, y, w, h = (float(el.get(k, 0)) for k in ("x", "y", "width", "height"))
                if w and h:
                    dxf.polyline(layer, [apply(m, *q) for q in ((x, y), (x + w, y), (x + w, y + h), (x, y + h))], True)
            elif tag == "circle":
                c = apply(m, float(el.get("cx", 0)), float(el.get("cy", 0)))
                r = float(el.get("r", 0)) * scale_of(m)
                dxf.circle(layer, c, r)
                if filled and r < 6:
                    dxf.circle(layer, c, r / 2)
            elif tag == "ellipse":
                cx, cy, rx, ry = (float(el.get(k, 0)) for k in ("cx", "cy", "rx", "ry"))
                dxf.polyline(layer, [apply(m, cx + rx * math.cos(t * math.pi / 16), cy + ry * math.sin(t * math.pi / 16)) for t in range(32)], True)
            elif tag == "line":
                dxf.line(layer, apply(m, float(el.get("x1", 0)), float(el.get("y1", 0))), apply(m, float(el.get("x2", 0)), float(el.get("y2", 0))))
            elif tag == "text":
                st = _style(el)
                fs = float(st.get("font-size", "0").replace("px", "") or 0) or next((CLASS_FONT[c] for c in cls if c in CLASS_FONT), 6)
                anchor = st.get("text-anchor") or next((CLASS_ANCHOR[c] for c in cls if c in CLASS_ANCHOR), "middle")
                pos = apply(m, float(el.get("x", 0)), float(el.get("y", 0)) + float(el.get("dy", 0) or 0))
                rot = -angle_of(m)
                dxf.text("TEXT" if ctx.get("layer") not in ("INSTRUMENTS", "VALVES", "MOTORS", "REMARKS", "REVISIONS") else ctx["layer"], pos, fs * scale_of(m) * 0.72, "".join(el.itertext()), rot, anchor)
                return
        for ch in el:
            walk(ch, m, ctx)

    walk(root, IDENT, {})
    n = dxf.write(dxf_path, W)
    return n, (W, H)


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "out_aspen_10tpd/pid_sheet.svg"
    dst = sys.argv[2] if len(sys.argv) > 2 else str(Path(src).with_suffix(".dxf"))
    n, size = convert(src, dst)
    print(f"{n} entities -> {dst}  (sheet {size[0]:.0f} x {size[1]:.0f} units)")
