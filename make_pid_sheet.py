"""
make_pid_sheet.py - draw the P&ID as a drawing sheet with standard-style symbols.

Equipment is placed by Graphviz (orthogonal routing); valves, fittings and line instruments are then drawn ON the
pipe lines, instrument bubbles follow ISA-5.1 shapes, and a border, title block and legend are added.

Run:  python make_pid_sheet.py --streams streams_aspen_10tpd.csv --out out_aspen_10tpd --title "Aspen 10 TPD case"
Writes <out>/pid_sheet.svg and <out>/pid_sheet.html (open in a browser; print to PDF from there).

Symbols are drawn from general knowledge of ISA-5.1 / ISO 10628 / PIP PIC001 conventions. Draft only.
"""
import argparse
import datetime
import json
import math
import re
import subprocess
from pathlib import Path

import pandas as pd

import make_symbols as ms
import pfd_to_pid as p

HERE = Path(__file__).parent
SYM = {name: body for _, name, _, _, body in ms.S}
EQ_SYMBOL = {"package": "Vendor package", "hopper": "Hopper / bin", "screw_heated": "Screw conveyor", "electric_heater_vessel": "Electric heater",
             "auger_reactor": "Screw conveyor", "cyclone": "Cyclone", "condenser": "Shell and tube exchanger", "tank": "Atmospheric tank", "bin": "Hopper / bin",
             "blower": "Roots blower", "flare": "Flare", "cooling_tower": "Cooling tower", "solids_cooler": "Shell and tube exchanger",
             "solids_separator": "Filter / strainer", "heat_exchanger": "Shell and tube exchanger", "gasifier": "Reactor (packed bed)",
             "gas_cooler": "Shell and tube exchanger", "bag_filter": "Bag house filter", "filter": "Filter / strainer", "knockout_drum": "Vertical vessel",
             "stack": "Stack", "air_intake": "Filter / strainer"}
GATE = '<path class="w" d="M-9 -6V6L0 0Z M9 -6V6L0 0Z"/>'
INLINE = {  # valve prefix -> symbol body centred on the pipe at (0,0)
    "HV": GATE,
    "SG": GATE + '<path d="M0 0V-9M-5 -9H5"/>',
    "CK": '<path class="f" d="M-9 -6V6L0 0Z"/><path class="w" d="M9 -6V6L0 0Z"/>',
    "FV": GATE + '<path d="M0 0V-10"/><path class="w" d="M-7 -10H7A7 6 0 0 0 -7 -10Z"/>',
    "XV": GATE + '<path d="M0 0V-10"/><rect class="w" x="-5" y="-19" width="10" height="9"/><path d="M-5 -14.5H5"/>',
    "PCV": GATE + '<path d="M0 0V-10"/><path class="w" d="M-7 -10H7A7 6 0 0 0 -7 -10Z"/><path d="M7 -13H13V0" stroke-dasharray="2 1.5"/>',
    "RV": '<circle class="w" r="7"/><path d="M-7 0H7M-3.5 -6.1L3.5 6.1M3.5 -6.1L-3.5 6.1"/>',
    "FA": '<rect class="w" x="-7" y="-6" width="14" height="12"/><text class="s" y="2.6">F</text>',
}
INLINE["TV"] = INLINE["LV"] = INLINE["FV"]
ACT_TOP = {"FV": -16, "TV": -16, "LV": -16, "XV": -19, "PCV": -16}
EW, EH, STEP = 96, 80, 30     # equipment symbol box (pt) and spacing of in-line items


def outline(body, s, ox, oy):
    """Segments of an equipment symbol outline in sheet coordinates (symbol box 100 x 84, scale s, origin ox, oy)."""
    import svg2dxf
    segs = []
    T = lambda u, v: (ox + u * s, oy + v * s)
    for d in re.findall(r'<path[^>]*\sd="([^"]+)"', body):
        for pts, closed in svg2dxf.path_to_polylines(d):
            P = [T(*q) for q in pts] + ([T(*pts[0])] if closed else [])
            segs += list(zip(P, P[1:]))
    for cx, cy, r in re.findall(r'<circle[^>]*cx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)"', body):
        cx, cy, r = float(cx), float(cy), float(r)
        P = [T(cx + r * math.cos(k * math.pi / 16), cy + r * math.sin(k * math.pi / 16)) for k in range(33)]
        segs += list(zip(P, P[1:]))
    for x, y, w, h in re.findall(r'<rect[^>]*x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"', body):
        x, y, w, h = map(float, (x, y, w, h))
        P = [T(x, y), T(x + w, y), T(x + w, y + h), T(x, y + h), T(x, y)]
        segs += list(zip(P, P[1:]))
    return segs


def cut_vertical(segs, x):
    """y values where the vertical line at x meets the outline."""
    ys = []
    for (x1, y1), (x2, y2) in segs:
        if min(x1, x2) - 0.01 <= x <= max(x1, x2) + 0.01:
            if abs(x2 - x1) < 1e-6:
                ys += [y1, y2]
            else:
                ys.append(y1 + (y2 - y1) * (x - x1) / (x2 - x1))
    return ys


def cut_horizontal(segs, y):
    xs = []
    for (x1, y1), (x2, y2) in segs:
        if min(y1, y2) - 0.01 <= y <= max(y1, y2) + 0.01:
            if abs(y2 - y1) < 1e-6:
                xs += [x1, x2]
            else:
                xs.append(x1 + (x2 - x1) * (y - y1) / (y2 - y1))
    return xs


def bubble(tag, loc, x, y):
    letters, num = tag.split("-")
    if loc == "DCS":
        shape = f'<rect class="w" x="{x-11}" y="{y-11}" width="22" height="22"/><circle class="w" cx="{x}" cy="{y}" r="11"/><path d="M{x-11} {y}H{x+11}"/>'
    elif loc == "SIS":
        shape = f'<rect class="w" x="{x-11}" y="{y-11}" width="22" height="22"/><path class="w" d="M{x-11} {y}L{x} {y-11}L{x+11} {y}L{x} {y+11}Z"/>'
    else:
        shape = f'<circle class="w" cx="{x}" cy="{y}" r="11"/>'
    fs = 5.6 if len(letters) > 3 else 6.4
    return f'<g class="ins" data-id="{tag}">{shape}<text x="{x}" y="{y-2}" style="font-size:{fs}px">{letters}</text><text x="{x}" y="{y+6.5}" style="font-size:5.6px">{num}</text></g>'


def layout(G, eq_nodes, lines, rows=2):
    """Graphviz places the equipment and routes the lines at right angles. Returns node boxes and polylines."""
    def run(free=()):
        dot = ['digraph L {rankdir=LR; splines=ortho; nodesep=1.5; ranksep=2.1; node [shape=box, fixedsize=true, label=""];']
        for n, w in eq_nodes.items():
            dot.append(f'"{n}" [width={w / 72:.3f}, height={EH / 72:.3f}];')
        for ln, (a, b) in lines.items():
            extra = ", constraint=false" if ln in free else ""
            dot.append(f'"{a}" -> "{b}" [comment="{ln.replace(chr(34), "in")}"{extra}];')
        dot.append("}")
        return json.loads(subprocess.run(["dot", "-Tjson"], input="\n".join(dot), capture_output=True, text=True, check=True).stdout)

    js = run()
    if rows > 1:        # fold a long train into rows: lines crossing a cut no longer force left-to-right order
        x = {o["name"]: float(o["pos"].split(",")[0]) for o in js["objects"]}
        width = float(js["bb"].split(",")[2])
        band = lambda n: min(rows - 1, int(x[n] / (width / rows + 1e-9)))
        js = run({ln for ln, (a, b) in lines.items() if band(a) != band(b)})
    W, H = [float(v) for v in js["bb"].split(",")[2:]]
    pos = {}
    for o in js["objects"]:
        x, y = [float(v) for v in o["pos"].split(",")]
        pos[o["name"]] = (x, H - y)
    routes = {}
    for e in js.get("edges", []):
        pts, start, end = [], None, None
        for tok in e["pos"].split():
            if tok.startswith("e,"):
                end = tuple(float(v) for v in tok[2:].split(","))
            elif tok.startswith("s,"):
                start = tuple(float(v) for v in tok[2:].split(","))
            else:
                pts.append(tuple(float(v) for v in tok.split(",")))
        poly = ([start] if start else []) + pts[0::3] + ([end] if end else [])
        poly = [(x, H - y) for x, y in poly]
        clean = [poly[0]]
        for q in poly[1:]:
            if abs(q[0] - clean[-1][0]) + abs(q[1] - clean[-1][1]) > 0.5:
                clean.append(q)
        routes[e["comment"]] = clean
    return W, H, pos, routes


def company_settings():
    """Title-block settings from data/company.json (company name, address, logo, signatures, sheet size)."""
    f = p.DATA / "company.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def _logo(cfg, x, y, w, h):
    """The company logo from data/<logo>.svg, scaled into the box (x, y, w, h). Empty if there is no logo file."""
    f = p.DATA / cfg.get("logo", "")
    if not cfg.get("logo") or not f.exists():
        return ""
    t = f.read_text(encoding="utf-8")
    m = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', t)
    vw, vh = float(m.group(1)), float(m.group(2))
    k = min(w / vw, h / vh)
    inner = re.sub(r"^<svg[^>]*>|</svg>\s*$", "", t.strip())
    inner = inner.replace('class="lw"', 'class="logo-l"').replace('class="f"', 'class="f logo-f"')
    return f'<g class="logo" transform="translate({x + (w - vw * k) / 2:.2f},{y + (h - vh * k) / 2:.2f}) scale({k:.4f})">{inner}</g>'


def title_block(SW, SH, M2, title, rev, case=None, company="", drawing_no="", designed="", drawn="", checked="", approved="", heading="P & I DIAGRAM OF"):
    """Title block in the company drawing standard (data/company.json): product, title, company name, address and logo,
    projection symbol, scale, sheet size, signatures with dates, drawing and revision number, copyright strip,
    and an alteration table filled from the case's revision log."""
    import workflow
    cfg = company_settings()
    co = (company or cfg.get("name") or "").upper()
    esc = lambda v: str(v).replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
    today = datetime.date.today().strftime("%d.%m.%Y")
    sign = [("DESIGNED", designed or cfg.get("designed") or "-"), ("DRAWN", drawn or cfg.get("drawn") or "GENERATED"),
            ("CHECKED", checked or cfg.get("checked") or "-"), ("APPROVED", approved or cfg.get("approved") or "-")]
    drg = drawing_no or cfg.get("drawing_no") or "PID-001"
    tw, thh, cw = 470, 150, 44                       # title block, copyright strip
    tx, ty = SW - M2 - tw, SH - M2 - thh
    lw_, rx = 270, tx + 270                          # left part (product / title / company) and right part
    t6 = lambda x, y, v, size=6, cls="l", extra="": f'<text class="{cls}" x="{x:.1f}" y="{y:.1f}" style="font-size:{size}px{extra}">{esc(v)}</text>'
    tb = (f'<rect class="w" x="{tx - cw}" y="{ty}" width="{tw + cw}" height="{thh}" style="stroke-width:1.6"/>'
          f'<path d="M{tx} {ty}v{thh}M{tx} {ty + 28}h{tw}M{tx} {ty + 86}h{tw}M{rx} {ty}v{thh}"/>'
          # product and title
          + t6(tx + 4, ty + 8, "PRODUCT", 5) + t6(tx + lw_ / 2, ty + 20, cfg.get("product", "BIOMASS GASIFICATION PLANT"), 8, "l c")
          + t6(tx + 4, ty + 36, "TITLE", 5) + t6(tx + lw_ / 2, ty + 54, heading, 10, "l c") + t6(tx + lw_ / 2, ty + 74, title.upper(), 10, "l c")
          + f'<path class="thin" d="M{tx + lw_ / 2 - 52} {ty + 57}h104M{tx + lw_ / 2 - min(130, 3.2 * len(title))} {ty + 77}h{2 * min(130, 3.2 * len(title))}"/>'
          # company block with logo
          + f'<path d="M{tx + lw_ - 56} {ty + 86}v{thh - 86}"/>'
          + t6(tx + (lw_ - 56) / 2, ty + 108, co, 15, "l c", ";letter-spacing:1px")
          + t6(tx + (lw_ - 56) / 2, ty + 124, cfg.get("address", ""), 6, "l c") + t6(tx + (lw_ - 56) / 2, ty + 138, cfg.get("unit", ""), 6, "l c")
          + _logo(cfg, tx + lw_ - 53, ty + 89, 50, thh - 92)
          # projection symbol, scale, sheet size, signatures
          + f'<path d="M{rx + 70} {ty}v86M{rx} {ty + 57}h70"/>'
          + f'<g transform="translate({rx + 22},{ty + 14})" class="thin"><circle r="7"/><circle r="3.5"/><path d="M-10 0h20M0 -10v20"/></g>'
          + f'<g transform="translate({rx + 50},{ty + 14})" class="thin"><path d="M-12 -4L12 -9V9L-12 4Z"/><path d="M-16 0h32" style="stroke-dasharray:3 2"/></g>'
          + t6(rx + 4, ty + 37, "SCALE", 5) + t6(rx + 35, ty + 50, cfg.get("scale", "NTS"), 8, "l c") + t6(rx + 35, ty + 76, cfg.get("sheet_size", "A1"), 12, "l c"))
    for i, (lab, who) in enumerate(sign):
        yy = ty + 28 + i * 14.5
        tb += (f'<path class="thin" d="M{rx + 70} {yy}h{tw - lw_ - 70}"/>' if i else "") + t6(rx + 73, yy + 10, lab, 6) + t6(rx + 118, yy + 10, who, 6) + t6(rx + 160, yy + 10, today, 6)
    tb += (f'<path class="thin" d="M{rx + 115} {ty + 28}v58M{rx + 157} {ty + 28}v58"/>'
           f'<path d="M{rx + 145} {ty + 86}v{thh - 86}"/>'
           + t6(rx + 4, ty + 95, "DRG. No.", 6) + t6(rx + 72, ty + 128, drg, 15, "l c")
           + t6(rx + 149, ty + 95, "REV. No.", 6) + t6(rx + 172, ty + 128, rev, 14, "l c"))
    # copyright strip (rotated text), like the company standard
    words, cl, cur = esc(cfg.get("copyright", "")).split(), [], ""
    for w_ in words:
        if len(cur) + len(w_) > 46:
            cl.append(cur)
            cur = w_
        else:
            cur = (cur + " " + w_).strip()
    cl.append(cur)
    tb += "".join(f'<text class="l" transform="translate({tx - cw + 7 + i * 6.2:.1f},{ty + thh - 4}) rotate(-90)" style="font-size:4.6px">{ln_}</text>' for i, ln_ in enumerate(cl[:6]) if ln_)
    # alteration table above the title block: the latest recorded changes from the revision log
    log_ = workflow.revisions(case).tail(4).iloc[::-1] if case else workflow.pd.DataFrame(columns=workflow.LOG_COLS)
    ah, ax, aw = 13, tx - cw, tw + cw
    ay = ty - ah * 5 - 4
    tb += (f'<rect class="w" x="{ax}" y="{ay}" width="{aw}" height="{ah * 5}"/>'
           f'<path class="thin" d="' + "".join(f"M{ax} {ay + ah * k}h{aw}" for k in range(1, 5)) + f'M{ax + 40} {ay}v{ah * 5}M{ax + aw - 130} {ay}v{ah * 5}M{ax + aw - 70} {ay}v{ah * 5}"/>'
           + t6(ax + 20, ay + 9, "REV.", 6, "hd c") + t6(ax + 46, ay + 9, "ALTERATION", 6, "hd") + t6(ax + aw - 100, ay + 9, "DATE", 6, "hd c") + t6(ax + aw - 35, ay + 9, "MADE BY", 6, "hd c"))
    nlog = len(workflow.revisions(case)) if case else 0
    for k, row_ in enumerate(log_.itertuples()):
        yy = ay + ah * (k + 1) + 9
        txt = (row_.what + (": " + row_.detail if row_.detail else ""))[:95]
        tb += t6(ax + 20, yy, f"A.{nlog - k}", 5.5, "l c") + t6(ax + 44, yy, txt.upper(), 5.2) + t6(ax + aw - 100, yy, str(row_.date)[:10], 5.5, "l c") + t6(ax + aw - 35, yy, str(row_.by).upper(), 5.5, "l c")
    if not len(log_):
        tb += t6(ax + 20, ay + ah + 9, "A", 5.5, "l c") + t6(ax + 44, ay + ah + 9, "DRAFT ISSUED FOR REVIEW", 5.2) + t6(ax + aw - 100, ay + ah + 9, today, 5.5, "l c")
    return tb


def draw(streams_file, title, out_dir, rows=2, company="", drawing_no="", designed="", rev=None, remarks=None, drawn="", checked="", approved=""):
    """remarks: list of dicts (id, target, status); open ones are flagged on the sheet. Items changed by an applied
    correction get a revision triangle. rev defaults to the case's revision mark from the workflow log."""
    import workflow
    G, eq, lines, inst, valves = p.build(streams_file)
    rev = rev or workflow.revision_letter(streams_file)
    anchors = {}
    lines = lines.rename(columns={"from": "src"})
    loc = {n: d.get("loc") for n, d in G.nodes(data=True)}
    eq_inst = {t: [i for i in inst[inst.attached_to == t].tag] for t in eq.tag}
    for t, lst in eq_inst.items():           # relief valve in the middle so it gets a nozzle on top of the shell
        psv = [x for x in lst if x.startswith("PSV")]
        rest = [x for x in lst if not x.startswith("PSV")]
        eq_inst[t] = rest[:len(rest) // 2] + psv + rest[len(rest) // 2:]
    eq_w = {t: max(EW, len(eq_inst[t]) * 26 + 8) for t in eq.tag}
    key = lambda s: s.replace('"', "in")
    L = {key(r.line_no): (r.src, r.to) for r in lines.itertuples()}
    W, H, pos, routes = layout(G, eq_w, L, rows)
    import engineering
    ENG = engineering.all_tables(streams_file, G, eq, lines.rename(columns={"src": "from"}), inst, valves)
    ENG_DV = {}
    for r_ in ENG["drains_vents"].itertuples():
        ENG_DV.setdefault(r_.item, []).append(r_.kind)
    FAILPOS = dict(zip(ENG["valve_schedule"].tag, ENG["valve_schedule"].fail_position))
    PAD, TB = 95, 250
    out, sig, top = [], [], []       # pipes / signals / symbols (painted in that order)
    where = {}                       # tag -> (x, y) for signal routing

    # ---- equipment-mounted instruments: a row of bubbles above each symbol, each tied to the equipment outline at a nozzle
    import engineering as engx
    sym_s = EH / 84
    geo = {}
    for e in eq.itertuples():
        body = re.sub(r'<text[^>]*>[^<]*</text>', "", SYM[EQ_SYMBOL.get(e.type, "Vendor package")])
        x0, y0 = pos[e.tag]
        geo[e.tag] = outline(body, sym_s, x0 - 50 * sym_s, y0 - EH / 2)
    DV_LATER = []
    for t in eq.tag:
        x0, y0 = pos[t]
        tags = eq_inst[t]
        segs = geo[t]
        xs_all = [q[0] for sg in segs for q in sg]
        ys_all = [q[1] for sg in segs for q in sg]
        top_y, bot_y = min(ys_all), max(ys_all)
        # nozzles spread over the top of the shell; outer instruments bend lower so leader lines never cross
        span = [x for x in (x0 + (i - 50) * (max(xs_all) - min(xs_all)) / 100 for i in range(0, 101))
                if cut_vertical(segs, x) and min(cut_vertical(segs, x)) <= top_y + 0.22 * (bot_y - top_y)]
        n = len(tags)
        if span:
            lo, hi = min(span) + 2.5, max(span) - 2.5
            fit = max(1, int((hi - lo) / 5) + 1)
        else:
            lo = hi = x0
            fit = 1
        use = min(n, fit)
        first = (n - use) // 2                 # middle instruments get top nozzles, the rest go to the side shell
        noz = {}
        for j in range(use):
            noz[first + j] = lo + (hi - lo) * (j / (use - 1) if use > 1 else 0.5)
        mid = (n - 1) / 2
        bub_y = y0 - EH / 2 - 26
        gap = top_y - (bub_y + 11)
        side_rank = {}
        for k in sorted(range(n), key=lambda k: abs(k - mid)):
            tag = tags[k]
            bx, by = x0 + (k - mid) * 26, bub_y
            where[tag] = (bx, by)
            anchors[tag] = (bx, by)
            drives = any(d["kind"] != "process" for _, v, d in G.out_edges(tag, data=True) if v == t)
            style = ' stroke-dasharray="3 2"' if drives else ""
            color = ' stroke="#c00"' if drives and loc[tag] == "SIS" else ""
            if k in noz:
                nx_ = noz[k]
                ny = min(cut_vertical(segs, nx_)) if cut_vertical(segs, nx_) else top_y
                if abs(nx_ - bx) < 0.6:
                    path_ = f"M{bx:.1f} {by + 11:.1f}V{ny:.1f}"
                else:
                    lvl = abs(k - mid)
                    yb = by + 11 + gap * (0.25 + 0.6 * lvl / (mid + 1))
                    path_ = f"M{bx:.1f} {by + 11:.1f}V{yb:.1f}H{nx_:.1f}V{ny:.1f}"
            else:                      # no room on top: down beside the symbol, then across to the shell
                sd = 1 if bx > x0 else -1
                side_rank[sd] = side_rank.get(sd, -1) + 1
                yy = min(top_y + 0.3 * (bot_y - top_y) + 7 * side_rank[sd], y0 - 6)
                xs = cut_horizontal(segs, yy)
                nx_ = (max(xs) if sd > 0 else min(xs)) if xs else (max(xs_all) if sd > 0 else min(xs_all))
                ny = yy
                path_ = f"M{bx:.1f} {by + 11:.1f}V{yy:.1f}H{nx_:.1f}"
            sig.append(f'<path d="{path_}"{style}{color} class="thin"/>')
            top.append(f'<circle class="f" cx="{nx_:.1f}" cy="{ny:.1f}" r="1.5"/>')
            if tag.startswith("PSV"):
                top.append(f'<g class="ins" data-id="{tag}" transform="translate({bx},{by+4})"><path class="w" d="M-7 -5V5L0 0Z M-5 8H5L0 0Z"/><path d="M0 0L0 -3L-3 -5L3 -7L-3 -9L3 -11L0 -13"/></g>'
                           f'<text x="{bx}" y="{by-12}" style="font-size:5.6px">{tag}</text>')
            else:
                top.append(bubble(tag, loc[tag], bx, by))
        DV_LATER.append((t, segs, xs_all, list(noz.values()), n))

    # ---- lines with their in-line items
    all_segs = []
    todo = []
    for r in lines.itertuples():
        ln, k = r.line_no, key(r.line_no)
        poly = routes.get(k)
        if not poly:
            continue
        a, b = pos[r.src], pos[r.to]

        def into(pt, nxt, c):       # from a point on the equipment box edge, run square to the symbol centre
            horizontal = abs(nxt[1] - pt[1]) < 1 if nxt else abs(pt[0] - c[0]) > abs(pt[1] - c[1])
            return [(c[0], pt[1]), c] if horizontal else [(pt[0], c[1]), c]
        full = into(poly[0], poly[1] if len(poly) > 1 else None, a)[::-1] + poly + into(poly[-1], poly[-2] if len(poly) > 1 else None, b)
        all_segs.append([(full[i], full[i + 1]) for i in range(len(full) - 1)])
        todo.append((r, poly, full))

    def crowded(x0, x1, y0, y1, own):
        """True if another line runs through the rectangle (used to keep bubbles off neighbouring pipes)."""
        for k, segs in enumerate(all_segs):
            if k == own:
                continue
            for (ax, ay), (bx, by) in segs:
                if min(ax, bx) <= max(x0, x1) and max(ax, bx) >= min(x0, x1) and min(ay, by) <= max(y0, y1) and max(ay, by) >= min(y0, y1):
                    return True
        return False

    for own, (r, poly, full) in enumerate(todo):
        ln = r.line_no
        nb = p.to_nb(r.size)
        shown = (ln + (f"  ({nb})" if nb else "")).replace("&", "&amp;")
        a, b = pos[r.src], pos[r.to]
        cls = "solids" if r.phase == "solid" else ("util" if r.service in ("CWS", "CWR") else "proc")
        d = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in full)
        out.append(f'<path class="{cls}" d="{d}"/><path class="hit" data-id="{ln.replace(chr(34), "&quot;")}" d="{d}"/>')
        (x1, y1), (x2, y2) = poly[-2] if len(poly) > 1 else a, poly[-1]
        ang = {(1, 0): 0, (-1, 0): 180, (0, 1): 90, (0, -1): 270}.get((round((x2 - x1) / (abs(x2 - x1) + abs(y2 - y1) + 1e-9)), round((y2 - y1) / (abs(x2 - x1) + abs(y2 - y1) + 1e-9))), 0)
        top.append(f'<path class="f {cls}h" transform="translate({x2:.1f},{y2:.1f}) rotate({ang})" d="M0 0L-9 -4V4Z"/>')
        # in-line chain in flow direction
        chain, node = [], r.src
        while True:
            nxt = [v for _, v, dd in G.out_edges(node, data=True) if dd.get("kind") == "process" and dd.get("line") == ln]
            if not nxt or nxt[0] == r.to:
                break
            node = nxt[0]
            chain.append(node)
        segs = [(poly[i], poly[i + 1]) for i in range(len(poly) - 1)]
        if not segs:
            segs = [(a, b)]
        seglen = lambda s: abs(s[1][0] - s[0][0]) + abs(s[1][1] - s[0][1])
        (sx, sy), (ex, ey) = max(segs, key=lambda s: seglen(s) + (40 if abs(s[1][1] - s[0][1]) < 1 else 0))
        horiz = abs(ey - sy) < 1
        length = seglen(((sx, sy), (ex, ey)))
        ux, uy = ((ex - sx) / length, (ey - sy) / length) if length else (1, 0)
        n = len(chain)
        step = min(STEP, (length - (30 if horiz else 46)) / (n + 0.5)) if n else 0
        used = n * step
        for i, item in enumerate(chain):
            t = (20 if horiz else 36) + step * (i + 0.5)
            cx, cy = sx + ux * t, sy + uy * t
            kind = G.nodes[item]["kind"]
            if kind == "valve":
                pre = re.match(r"[A-Z]+", item).group()
                rot = 0 if horiz else 90
                flip = ' scale(-1,1)' if (ux < 0 and pre == "CK") else ""
                top.append(f'<g class="vlv" data-id="{item}" transform="translate({cx:.1f},{cy:.1f}) rotate({rot}){flip}">{INLINE.get(pre, GATE)}<rect class="hitbox" x="-10" y="-10" width="20" height="20"/></g>')
                tx, ty = (cx, cy + 15) if horiz else (cx - 10, cy + 2)
                top.append(f'<text x="{tx:.1f}" y="{ty:.1f}" style="font-size:5.6px{"" if horiz else ";text-anchor:end"}">{item}</text>')
                fp = FAILPOS.get(item, "-")
                if fp not in ("-", "", None) and pre in ("XV", "FV", "TV", "LV", "SG"):
                    fx, fy = (cx + 8, cy - 12) if horiz else (cx + 12, cy - 8)
                    top.append(f'<text x="{fx:.1f}" y="{fy:.1f}" style="font-size:5px;font-weight:bold;text-anchor:start">{fp}</text>')
                where[item] = (cx, cy + ACT_TOP.get(pre, -6)) if horiz else (cx - ACT_TOP.get(pre, -6), cy)
                anchors[item] = (cx, cy)
            else:  # tap with instrument(s)
                tags = [v for _, v in G.out_edges(item)]
                first = tags[0]
                ox, oy = (0, -30) if horiz else (-32, 0)
                if horiz and crowded(cx - 14, cx + 14, cy - 62, cy - 6, own) and not crowded(cx - 14, cx + 14, cy + 6, cy + 62, own):
                    ox, oy = 0, 30
                bx, by = cx + ox, cy + oy
                sig.append(f'<path class="thin" d="M{cx:.1f} {cy:.1f}L{bx:.1f} {by:.1f}"/>')
                top.append(f'<circle class="f" cx="{cx:.1f}" cy="{cy:.1f}" r="1.6"/>')
                top.append(bubble(first, loc[first], bx, by))
                where[first] = (bx, by)
                anchors[first] = (bx, by)
                for _, ctrl, dd in G.out_edges(first, data=True):       # FT -> FIC
                    if G.nodes[ctrl]["kind"] == "instrument":
                        qx, qy = (bx, by + (28 if oy > 0 else -28)) if horiz else (bx - 28, by)
                        sig.append(f'<path class="sig" d="M{bx:.1f} {by:.1f}L{qx:.1f} {qy:.1f}"/>')
                        top.append(bubble(ctrl, loc[ctrl], qx, qy))
                        where[ctrl] = (qx, qy)
        # line number: on another long segment if there is one, else on the free part of the chosen segment
        others = [g for g in segs if g != ((sx, sy), (ex, ey)) and seglen(g) > 75]
        free0 = 20 + used + 6
        if others and n:
            (sx, sy), (ex, ey) = max(others, key=seglen)
            horiz = abs(ey - sy) < 1
            length = seglen(((sx, sy), (ex, ey)))
            ux, uy = (ex - sx) / length, (ey - sy) / length
            free0 = 0
        if horiz:
            lx = sx + ux * (free0 + (length - free0) / 2) if length - free0 > 60 else sx + ux * length / 2
            top.append(f'<text class="ln" x="{lx:.1f}" y="{sy - 4 if length - free0 > 60 else sy + 24:.1f}">{shown}</text>')
            anchors[ln] = (lx, sy)
        else:
            ly = sy + uy * (free0 + (length - free0) / 2) if length - free0 > 60 else sy + uy * length / 2
            top.append(f'<text class="ln" transform="translate({sx - 10:.1f},{ly:.1f}) rotate(-90)" y="6">{shown}</text>')
            anchors[ln] = (sx, ly)

    # ---- vents (top) and drains (bottom) with valve and blind, where the rules ask for them and there is room
    DRAW_DV = {"gasifier", "knockout_drum", "gas_cooler", "heat_exchanger", "condenser", "tank"}
    etype = dict(zip(eq.tag, eq.type))

    def busy(x0_, x1_, y0_, y1_):
        for sg in all_segs:
            for (ax, ay), (bx_, by_) in sg:
                if min(ax, bx_) <= max(x0_, x1_) and max(ax, bx_) >= min(x0_, x1_) and min(ay, by_) <= max(y0_, y1_) and max(ay, by_) >= min(y0_, y1_):
                    return True
        return False
    for t, segs, xs_all, nozx, ninst in DV_LATER:
        if etype.get(t) not in DRAW_DV:
            continue
        x0, y0 = pos[t]
        kinds = set(ENG_DV.get(t, []))
        if "Vent" in kinds and ninst < 4:
            cands = [x for x in (x0 + f_ * (max(xs_all) - x0) for f_ in (0.35, 0.55, -0.35, -0.55)) if cut_vertical(segs, x)]
            cands = [x for x in cands if all(abs(x - u) > 6 for u in nozx) and not busy(x - 3, x + 3, min(cut_vertical(segs, x)) - 16, min(cut_vertical(segs, x)) - 2)]
            if cands:
                vx = cands[0]
                vy = min(cut_vertical(segs, vx))
                top.append(f'<g class="vlv" data-id="{t}-VENT"><path class="thin" d="M{vx:.1f} {vy:.1f}V{vy - 15:.1f}M{vx - 3:.1f} {vy - 15:.1f}H{vx + 3:.1f}"/>'
                           f'<g transform="translate({vx:.1f},{vy - 8:.1f}) rotate(90) scale(.5)">{GATE}</g>'
                           f'<text x="{vx + 4:.1f}" y="{vy - 13:.1f}" style="font-size:4.2px;text-anchor:start">VENT 1"</text></g>')
        if "Drain" in kinds:
            for f_, sd in ((0.2, 1), (-0.2, -1), (0.45, 1), (-0.45, -1)):
                dx = x0 + f_ * (max(xs_all) - min(xs_all))
                ys = cut_vertical(segs, dx)
                if not ys:
                    continue
                dy = max(ys)
                if busy(min(dx, dx + 22 * sd), max(dx, dx + 22 * sd), dy + 1, dy + 10):
                    continue
                ex = dx + 17 * sd
                top.append(f'<g class="vlv" data-id="{t}-DRAIN"><path class="thin" d="M{dx:.1f} {dy:.1f}V{dy + 6:.1f}H{ex:.1f}M{ex:.1f} {dy + 3:.1f}V{dy + 9:.1f}"/>'
                           f'<g transform="translate({dx + 9 * sd:.1f},{dy + 6:.1f}) scale(.5)">{GATE}</g>'
                           f'<text x="{ex + 3 * sd:.1f}" y="{dy + 7.6:.1f}" style="font-size:4.2px;text-anchor:{"start" if sd > 0 else "end"}">DRN 1"</text></g>')
                break

    # ---- remote signals: controller / trip -> valve
    for u, v, d in G.edges(data=True):
        if d["kind"] in ("signal", "trip") and u in where and v in where and G.nodes[v]["kind"] == "valve":
            (x1, y1), (x2, y2) = where[u], where[v]
            ymid = max(-70, min(y1, y2) - 22 - (sum(map(ord, u + v)) % 4) * 4)
            color = ' stroke="#c00"' if d["kind"] == "trip" else ""
            sig.append(f'<path class="sig"{color} d="M{x1:.1f} {y1-11:.1f}V{ymid:.1f}H{x2:.1f}V{y2:.1f}"/>')

    # ---- equipment symbols and tags
    motor_of = {m["drives"]: m["tag"] for m in G.graph.get("motors", [])}
    for e in eq.itertuples():
        x0, y0 = pos[e.tag]
        body = SYM[EQ_SYMBOL.get(e.type, "Vendor package")]
        body = re.sub(r'<text[^>]*>[^<]*</text>', "", body)
        s = EH / 84
        top.append(f'<g class="eq" data-id="{e.tag}" transform="translate({x0 - 50 * s:.1f},{y0 - EH / 2:.1f}) scale({s:.3f})">{body}<rect class="hitbox" x="0" y="0" width="100" height="84"/></g>')
        top.append(f'<text class="tag" x="{x0:.1f}" y="{y0 + EH / 2 + 10:.1f}">{e.tag}</text>'
                   f'<text x="{x0:.1f}" y="{y0 + EH / 2 + 18:.1f}" style="font-size:5.8px">{str(e.name).upper().replace("&", "&amp;")}</text>')
        m = motor_of.get(e.tag)
        if m:       # motor + gearbox, drawn below the drive end
            mx, my = x0 - 35 * s, y0 + 25
            anchors[m] = (mx, my)
            top.append(f'<g class="mot" data-id="{m}"><path d="M{mx:.1f} {y0 + 8:.1f}V{my - 7:.1f}"/><rect class="w" x="{mx - 5:.1f}" y="{my - 13:.1f}" width="10" height="6"/>'
                       f'<rect class="w" x="{mx - 15:.1f}" y="{my - 7:.1f}" width="30" height="13"/>'
                       f'<text x="{mx:.1f}" y="{my + 1.8:.1f}" style="font-size:5.4px">MOTOR ({m})</text><text x="{mx:.1f}" y="{my - 8.2:.1f}" style="font-size:4px">GB</text></g>')

    for e in eq.itertuples():
        anchors[e.tag] = (pos[e.tag][0], pos[e.tag][1])

    # ---- revision triangles on items changed by an applied correction, and flags for open review remarks
    marks = []
    changed = {}
    for n, d in G.nodes(data=True):
        src = str(d.get("source", ""))
        if src.startswith("correction"):
            changed.setdefault(n, src.split()[-1])
    for r in lines.itertuples():
        if str(getattr(r, "size_source", "")).startswith("correction"):
            changed.setdefault(r.line_no, r.size_source.split()[-1])
    for tag, (pos_, cid) in G.graph.get("fail_override", {}).items():
        changed.setdefault(tag, cid)
    for n, cid in changed.items():
        if n in anchors:
            x, y = anchors[n]
            marks.append(f'<g class="rev" data-id="{n.replace(chr(34), "&quot;")}"><path class="w" style="stroke:#1f5fa8;stroke-width:.9" d="M{x - 21:.1f} {y - 13:.1f}l6 -10l6 10z"/>'
                         f'<text x="{x - 15:.1f}" y="{y - 14:.1f}" style="font-size:4.4px;fill:#1f5fa8">{cid.split("-")[-1].lstrip("0")}</text></g>')
    flagged = 0
    for k, rmk in enumerate(remarks or []):
        if str(rmk.get("status", "open")).lower() != "open" or rmk.get("target") not in anchors:
            continue
        x, y = anchors[rmk["target"]]
        flagged += 1
        num = str(rmk["id"]).split("-")[-1].lstrip("0")
        marks.append(f'<g class="rmk" data-id="{str(rmk["target"]).replace(chr(34), "&quot;")}"><path style="fill:#fff2f2;stroke:#c00;stroke-width:1" d="M{x + 8:.1f} {y - 6:.1f}l7 -12l7 12z"/>'
                     f'<text x="{x + 15:.1f}" y="{y - 8:.1f}" style="font-size:5px;fill:#c00;font-weight:bold">{num}</text></g>')
    top += marks

    # ---- sheet: zone-grid border, equipment list, legends, tag key, notes, title block
    LD = ENG["line_designation"]
    LLH = 26 + 8.2 * (len(LD) + 1) + 8
    SW, SH = max(W + 2 * PAD, 1640), H + 2 * PAD + TB + LLH
    by = H + 2 * PAD + 6 + LLH
    band_y = H + 2 * PAD + 6
    M1, M2 = 10, 26                      # outer and inner border
    ncol, nrow = 12, 8
    zone = [f'<rect x="{M1}" y="{M1}" width="{SW - 2 * M1:.0f}" height="{SH - 2 * M1:.0f}" style="stroke-width:1.2"/>',
            f'<rect x="{M2}" y="{M2}" width="{SW - 2 * M2:.0f}" height="{SH - 2 * M2:.0f}" style="stroke-width:2"/>']
    for i in range(ncol):
        xa = M2 + (SW - 2 * M2) * i / ncol
        xm = xa + (SW - 2 * M2) / ncol / 2
        if i:
            zone.append(f'<path d="M{xa:.1f} {M1}V{M2}M{xa:.1f} {SH - M2}V{SH - M1}"/>')
        zone.append(f'<text class="z" x="{xm:.1f}" y="{M1 + 11}">{ncol - i}</text><text class="z" x="{xm:.1f}" y="{SH - M1 - 5}">{ncol - i}</text>')
    for j in range(nrow):
        ya = M2 + (SH - 2 * M2) * j / nrow
        ym = ya + (SH - 2 * M2) / nrow / 2
        if j:
            zone.append(f'<path d="M{M1} {ya:.1f}H{M2}M{SW - M2} {ya:.1f}H{SW - M1}"/>')
        letter = "ABCDEFGH"[nrow - 1 - j]
        zone.append(f'<text class="z" x="{M1 + 8}" y="{ym + 3:.1f}">{letter}</text><text class="z" x="{SW - M1 - 8}" y="{ym + 3:.1f}">{letter}</text>')

    # line designation table and pipe class / MOC / insulation key
    e_ = lambda v: str(v).replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
    short_end = lambda v: ("BW + RF FLG" if v.startswith("Butt") else "SW, NO THREADS" if v.startswith("Socket") else "NPT THREADED" if v.startswith("Thread")
                           else "FLANGED CHUTE")
    fmt = lambda v: "-" if v is None or (isinstance(v, float) and math.isnan(v)) or v == "" else (f"{v:g}" if isinstance(v, (int, float)) else str(v))
    lcols = [("LINE No.", 98), ("SIZE", 46), ("SERVICE", 70), ("FROM", 36), ("TO", 36), ("CLASS", 26), ("MOC", 46), ("INSUL.", 34),
             ("OP. T / P", 52), ("DES. T / P", 52), ("TEST P", 28), ("END CONNECTION", 62), ("VALVES ON LINE", 150), ("DRAINS / VENTS / PURGE", 128)]
    lx0, ly0 = M2 + 14, band_y + 4
    lw = sum(w for _, w in lcols)
    ll = [f'<text class="hd" x="{lx0}" y="{ly0 + 8}">LINE DESIGNATION TABLE</text>'
          f'<text class="l" x="{lx0 + 120}" y="{ly0 + 8}" style="font-size:5.4px">LINE No. = {e_(engineering.LINE_NOMENCLATURE.upper())}.  T IN C, P IN BAR G.</text>']
    ty0 = ly0 + 13
    nrow_ = len(LD) + 1
    ll.append(f'<rect class="w" x="{lx0}" y="{ty0}" width="{lw}" height="{8.2 * nrow_ + 2}"/>')
    xx = lx0
    for i_, (h_, w_) in enumerate(lcols):
        if i_:
            ll.append(f'<path class="thin" d="M{xx} {ty0}v{8.2 * nrow_ + 2}"/>')
        ll.append(f'<text x="{xx + w_ / 2}" y="{ty0 + 7}" style="font-size:5.2px;font-weight:bold">{h_}</text>')
        xx += w_
    ll.append(f'<path class="thin" d="M{lx0} {ty0 + 9.5}h{lw}"/>')
    for j_, r_ in enumerate(LD.itertuples()):
        yy = ty0 + 9.5 + 8.2 * (j_ + 1) - 2
        ins = "-" if r_.insulation in ("-", "N") else f"{r_.insulation} {int(r_.insulation_mm)}"
        vals = [r_.line_no, (r_.size if "solid" not in str(r_.size) else "-") + (f" ({r_.nb.replace(' NB', '')})" if r_.nb else ""), r_.service_name.upper(),
                getattr(r_, "_7", None) or LD.loc[r_.Index, "from"], r_.to, r_.pipe_class, r_.moc if r_.phase != "solid" else "-", ins,
                f"{fmt(r_.op_temp_C)} / {fmt(r_.op_press_barg)}", (f"{fmt(r_.design_temp_C)} / {fmt(r_.design_press_barg)}" if r_.phase != "solid" else f"{fmt(r_.design_temp_C)} / -"), fmt(r_.test_press_barg),
                short_end(r_.end_connection), r_.valve_tags, r_.drains_vents.upper()]
        xx = lx0
        for (h_, w_), v_ in zip(lcols, vals):
            v_ = str(v_)
            maxc = int(w_ / 2.55)
            v_ = v_ if len(v_) <= maxc else v_[:maxc - 1] + "."
            ll.append(f'<text class="l" x="{xx + 2}" y="{yy}" style="font-size:5px">{e_(v_)}</text>')
            xx += w_
    # key: pipe classes and MOC abbreviations, insulation codes
    kx0 = lx0 + lw + 14
    kw = SW - M2 - 14 - kx0
    ll.append(f'<text class="hd" x="{kx0}" y="{ly0 + 8}">PIPE CLASS / MATERIAL OF CONSTRUCTION</text>')
    ky = ly0 + 13
    pcs = list(engineering.PIPE_CLASSES.items())
    ll.append(f'<rect class="w" x="{kx0}" y="{ky}" width="{kw}" height="{8.2 * (len(pcs) + 1) + 2}"/><path class="thin" d="M{kx0} {ky + 9.5}h{kw}M{kx0 + 26} {ky}v{8.2 * (len(pcs) + 1) + 2}M{kx0 + 76} {ky}v{8.2 * (len(pcs) + 1) + 2}M{kx0 + 200} {ky}v{8.2 * (len(pcs) + 1) + 2}"/>')
    for x_, h_ in ((kx0 + 13, "CLASS"), (kx0 + 51, "MOC"), (kx0 + 138, "PIPE MATERIAL / RATING"), (kx0 + 200 + (kw - 200) / 2, "USE")):
        ll.append(f'<text x="{x_}" y="{ky + 7}" style="font-size:5.2px;font-weight:bold">{h_}</text>')
    for j_, (code, d_) in enumerate(pcs):
        yy = ky + 9.5 + 8.2 * (j_ + 1) - 2
        use_ = d_["use"].split(".")[0]
        maxc = int((kw - 204) / 2.5)
        use_ = use_ if len(use_) <= maxc else use_[:maxc - 1] + "."
        ll.append(f'<text class="l" x="{kx0 + 3}" y="{yy}" style="font-size:5px">{code}</text><text class="l" x="{kx0 + 29}" y="{yy}" style="font-size:5px">{d_["moc"]}</text>'
                  f'<text class="l" x="{kx0 + 79}" y="{yy}" style="font-size:4.8px">{e_((d_["pipe"].split(",")[0] + ", " + d_["rating"]).upper())}</text>'
                  f'<text class="l" x="{kx0 + 203}" y="{yy}" style="font-size:4.8px">{e_(use_.upper())}</text>')
    ky2 = ky + 8.2 * (len(pcs) + 1) + 12
    ab = [("INSULATION", "H = HEAT CONSERVATION,  P = PERSONNEL PROTECTION,  N = NONE;  THICKNESS IN MM MINERAL WOOL (MW)"),
          ("END CONNECTIONS", "BW = BUTT-WELD, RF FLG = RAISED-FACE FLANGE, SW = SOCKET-WELD, NPT = THREADED. NO THREADS IN FLAMMABLE / TOXIC / OXIDANT LINES"),
          ("MOC", "CS = CARBON STEEL, SS316L = STAINLESS 316L, SS316L-OC = 316L OXYGEN-CLEANED, SS310S = HEAT-RESISTANT STAINLESS 310S"),
          ("VALVES", "FC = FAIL CLOSED, FO = FAIL OPEN, FL = FAIL LOCKED; DRN / VENT 1\" = DRAIN / VENT WITH GATE VALVE AND BLIND")]
    for j_, (h_, t_) in enumerate(ab):
        yy = ky2 + 8 * j_
        ll.append(f'<text class="l" x="{kx0}" y="{yy}" style="font-size:5.2px;font-weight:bold">{h_}:</text><text class="l" x="{kx0 + 62}" y="{yy}" style="font-size:4.9px">{e_(t_)}</text>')
    ll.append(f'<path d="M{M2} {by - 6}H{SW - M2}"/>')
    tbl_ll = "".join(ll)

    # list of equipment (as on a typical P&ID sheet)
    x0, rh = M2 + 14, 8.2
    cols = [0, 26, 168, 226, 252]
    hdr = ["SR.NO.", "EQUIPMENT", "DESIGNATION", "QTY."]
    rows_eq = [(i + 1, str(e.name).upper()[:40], e.tag, "01") for i, e in enumerate(eq.itertuples())]
    th = (len(rows_eq) + 2) * rh
    tbl = [f'<rect x="{x0}" y="{by + 4}" width="{cols[-1]}" height="{th:.1f}"/><text class="hd c" x="{x0 + cols[-1] / 2}" y="{by + 4 + rh - 2}">LIST OF EQUIPMENT</text>']
    for k in range(len(rows_eq) + 1):
        tbl.append(f'<path class="thin" d="M{x0} {by + 4 + rh * (k + 1):.1f}h{cols[-1]}"/>')
    for c in cols[1:-1]:
        tbl.append(f'<path class="thin" d="M{x0 + c} {by + 4 + rh}v{th - rh:.1f}"/>')
    for k, row in enumerate([hdr] + rows_eq):
        for c, v in enumerate(row):
            anchor, xx = ("start", x0 + cols[c] + 3) if c == 1 else ("middle", x0 + (cols[c] + cols[c + 1]) / 2)
            tbl.append(f'<text x="{xx:.1f}" y="{by + 4 + rh * (k + 2) - 2.2:.1f}" style="font-size:5.4px;text-anchor:{anchor}{";font-weight:bold" if k == 0 else ""}">{str(v).replace("&", "&amp;")}</text>')

    lx = x0 + cols[-1] + 26
    red = lambda c: 'stroke="#c00"' if "trip" in c else ""
    legend = [("proc", "PROCESS LINE"), ("solids", "SOLIDS TRANSFER"), ("util", "UTILITY (COOLING WATER)"), ("thin", "INSTRUMENT CONNECTION"),
              ("sig", "ELECTRICAL SIGNAL"), ("sig trip", "SAFETY TRIP SIGNAL")]
    leg = f'<text class="hd" x="{lx}" y="{by + 12}">LINES</text>' + "".join(
        f'<path class="{c}" {red(c)} d="M{lx} {by + 24 + i * 12}h40"/><text class="l" x="{lx + 48}" y="{by + 26.5 + i * 12}">{t}</text>' for i, (c, t) in enumerate(legend))
    iy = by + 104
    bub = (f'<text class="hd" x="{lx}" y="{iy}">INSTRUMENTS (ISA-5.1)</text>'
           + bubble("XX-000", "FIELD", lx + 12, iy + 20) + f'<text class="l" x="{lx + 30}" y="{iy + 22}">FIELD MOUNTED</text>'
           + bubble("XX-000", "DCS", lx + 12, iy + 47) + f'<text class="l" x="{lx + 30}" y="{iy + 49}">SHARED DISPLAY / CONTROL (DCS)</text>'
           + bubble("XX-000", "SIS", lx + 12, iy + 74) + f'<text class="l" x="{lx + 30}" y="{iy + 76}">SAFETY FUNCTION (TRIP)</text>'
           + f'<text class="l" x="{lx}" y="{iy + 98}">HH / H / L / LL = HIGH-HIGH, HIGH, LOW, LOW-LOW</text>')
    vx = lx + 200
    vl = [("HV", "HV", "MANUAL VALVE"), ("CK", "CK", "NON-RETURN VALVE"), ("FV", "FV/TV/LV", "CONTROL VALVE"), ("XV", "XV", "ON/OFF SHUTDOWN VALVE"),
          ("PCV", "PCV", "PRESSURE REGULATOR"), ("RV", "RV", "ROTARY AIRLOCK VALVE"), ("SG", "SG", "SLIDE GATE"), ("FA", "FA", "FLAME ARRESTOR")]
    vth = (len(vl)) * 22 + 6
    val = f'<text class="hd" x="{vx}" y="{by + 12}">VALVES AND FITTINGS</text><rect x="{vx}" y="{by + 18}" width="196" height="{vth}"/><path class="thin" d="M{vx + 44} {by + 18}v{vth}M{vx + 86} {by + 18}v{vth}"/>'
    for i, (k, code, t) in enumerate(vl):
        yy = by + 18 + 22 * i
        val += (f'<path class="thin" d="M{vx} {yy + 22}h196"/>' if i < len(vl) - 1 else "") + \
               f'<g transform="translate({vx + 22},{yy + 15})"><path class="thin" d="M-16 0H16"/>{INLINE[k]}</g>' \
               f'<text x="{vx + 65}" y="{yy + 15}" style="font-size:6px">{code}</text><text class="l" x="{vx + 90}" y="{yy + 15}" style="font-size:6px">{t}</text>'
    kx = vx + 222
    key = [("TAG NUMBER DETAILS", True), ("INSTRUMENT:  FIC - 4 021", False), ("   FIC = FUNCTION LETTERS (ISA-5.1)", False), ("   4 = PLANT AREA,  021 = LOOP NUMBER", False),
           ("EQUIPMENT:  R - 4 01", False), ("   R = TYPE,  4 = AREA,  01 = NUMBER", False), ("VALVE:  XV - 4 022  (PREFIX = VALVE TYPE)", False),
           ("LINE:  6\" - SG - 4031 - CS1 - N", False), ("   SIZE - SERVICE - SEQUENCE - SPEC - INSULATION", False), ("   NB = NOMINAL BORE IN MM", False), ("MOTOR:  M - 01", False)]
    kt = "".join(f'<text class="{"hd" if h else "l"}" x="{kx}" y="{by + 12 + i * 11}" style="font-size:{7.5 if h else 6}px">{t.replace(chr(34), "&quot;")}</text>' for i, (t, h) in enumerate(key))
    notes = ["NOTES", "1. DRAFT GENERATED BY RULES FROM THE STREAM TABLE.", "    NOT REVIEWED. NO HAZOP. NOT FOR CONSTRUCTION.",
             "2. INSTRUMENTS AND TAGS: ISA-5.1 CONVENTIONS.", "    EQUIPMENT OUTLINES: COMMON ISO 10628 / PIP PRACTICE.",
             "3. LINE SIZES ARE VELOCITY-BASED ESTIMATES.", "4. FAIL POSITIONS, SET POINTS, NOZZLES, DRAINS, VENTS:", "    PROPOSED BY RULES - SEE SCHEDULES SHEET -02."]
    nt = "".join(f'<text class="{"hd" if i == 0 else "l"}" x="{kx}" y="{by + 142 + i * 10}" style="font-size:{7.5 if i == 0 else 5.8}px">{t}</text>' for i, t in enumerate(notes))

    tb = title_block(SW, SH, M2, title, rev, streams_file, company=company, drawing_no=drawing_no,
                     designed=designed, drawn=drawn, checked=checked, approved=approved)
    css = ('*{fill:none;stroke:#000;stroke-width:1;stroke-linejoin:round;stroke-linecap:round}'
           'text{stroke:none;fill:#000;font-family:ISOCPEUR,Romans,Simplex,"Arial Narrow","Liberation Sans Narrow",Arial,Helvetica,sans-serif;text-anchor:middle;font-size:6px}'
           '.w{fill:#fff}.f{fill:#000}.eq *{stroke-width:2.2;fill:#fff}.eq text{fill:#000;stroke:none}'
           '.proc{stroke-width:2.2}.solids{stroke-width:2.2;stroke:#7a4a1e;stroke-dasharray:9 3}.util{stroke-width:1.1;stroke:#1f5fa8}'
           '.proch{fill:#000}.solidsh{fill:#7a4a1e;stroke:#7a4a1e}.utilh{fill:#1f5fa8;stroke:#1f5fa8}'
           '.thin{stroke-width:.6}.sig{stroke-width:.7;stroke-dasharray:4 2.5}'
           '.tag{font-size:7.5px;font-weight:bold}.ln{font-size:6px}.l{font-size:6.4px;text-anchor:start}.c{text-anchor:middle}.s{font-size:6px}'
           '.hit{stroke:transparent;stroke-width:9;fill:none}.hitbox{fill:transparent;stroke:none}'
           '.rmk text,.rev text{text-anchor:middle;stroke:none}'
           '.logo .f{stroke-width:.2}.logo-l{stroke-width:.3}.tt{font-size:11px;font-weight:bold}.hd{font-size:7.5px;font-weight:bold;text-anchor:start}.hd.c{text-anchor:middle}.z{font-size:8px}')
    ox = (SW - W) / 2
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SW:.0f} {SH:.0f}" width="{SW:.0f}" height="{SH:.0f}"><style>{css}</style>'
           f'<rect x="0" y="0" width="{SW:.0f}" height="{SH:.0f}" style="fill:#fff;stroke:none"/>{"".join(zone)}<path d="M{M2} {by - 6}H{SW - M2}"/>'
           f'<g transform="translate({ox:.1f},{PAD})">{"".join(out)}{"".join(sig)}{"".join(top)}</g>'
           f'{"".join(tbl)}{tbl_ll}{leg}{bub}{val}{kt}{nt}{tb}</svg>')
    out_dir = Path(out_dir)
    out_dir.mkdir(exist_ok=True)
    (out_dir / "pid_sheet.svg").write_text(svg, encoding="utf-8")
    # drawing zone of every item (12 columns x 8 rows, as on the border), for the BOM and traceability
    M2 = 26

    def zone_of(x, y):
        X, Y = x + ox, y + PAD
        c = int((X - M2) // ((SW - 2 * M2) / 12))
        r = int((Y - M2) // ((SH - 2 * M2) / 8))
        return f"{'ABCDEFGH'[7 - r]}-{12 - c}" if 0 <= c < 12 and 0 <= r < 8 else "-"
    zones = {k: zone_of(*v) for k, v in anchors.items()}
    import json as _json
    (out_dir / "sheet_zones.json").write_text(_json.dumps(zones, indent=0), encoding="utf-8")
    html = (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>P&amp;ID sheet - {title}</title>'
            f'<style>@page{{size:{SW + 2:.0f}pt {SH + 2:.0f}pt;margin:0}}html,body{{margin:0;background:#fff;overflow:hidden}}svg{{display:block;width:100%;height:auto}}</style></head><body>{svg}</body></html>')
    (out_dir / "pid_sheet.html").write_text(html, encoding="utf-8")
    return svg, (SW, SH), dict(equipment=len(eq), lines=len(lines), instruments=len(inst), valves=len(valves), remarks_flagged=flagged,
                               changed=len(changed), zones=zones)


def _table(x, y, title, cols, rows, fs=5.4, rh=8.2):
    """cols: list of (header, width, key or callable). Returns (svg, height)."""
    e_ = lambda v: str(v).replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
    w = sum(c[1] for c in cols)
    h = 10 + rh * len(rows) + 3
    o = [f'<text class="hd" x="{x}" y="{y - 4}">{e_(title)}</text><rect class="w" x="{x}" y="{y}" width="{w}" height="{h}"/><path class="thin" d="M{x} {y + 10}h{w}"/>']
    xx = x
    for i, (hd, cw, _) in enumerate(cols):
        if i:
            o.append(f'<path class="thin" d="M{xx} {y}v{h}"/>')
        o.append(f'<text x="{xx + cw / 2}" y="{y + 7.2}" style="font-size:5px;font-weight:bold">{e_(hd)}</text>')
        xx += cw
    for j, r in enumerate(rows):
        yy = y + 10 + rh * (j + 1) - 2
        xx = x
        for hd, cw, k in cols:
            v = k(r) if callable(k) else r.get(k, "")
            v = "-" if v is None or (isinstance(v, float) and math.isnan(v)) or str(v) in ("", "nan") else (f"{v:g}" if isinstance(v, float) else str(v))
            mc = int(cw / (fs * 0.5))
            v = v if len(v) <= mc else v[:mc - 1] + "."
            o.append(f'<text class="l" x="{xx + 2}" y="{yy}" style="font-size:{fs}px">{e_(v.upper())}</text>')
            xx += cw
    return "".join(o), h + 14


def draw_schedules(streams_file, title, out_dir, company="", drawing_no="", rev=None, **kw):
    """Sheet 2: instrument set points and alarms, valve fail positions, nozzle schedule, drains and vents (company frame)."""
    import engineering
    import make_legend_sheet as mls
    import workflow
    G, eq, lines, inst, valves = p.build(streams_file)
    T = engineering.all_tables(streams_file, G, eq, lines, inst, valves)
    rev = rev or workflow.revision_letter(streams_file)
    SW_, SH_ = mls.SW, mls.SH
    drg = (drawing_no or company_settings().get("drawing_no", "PID")) + "-02"
    fr, _ = mls.frame("SCHEDULES", drawing_no=drg, rev=rev, company=company)
    fr = fr.replace("SYMBOLS AND LEGEND", (title + " - SCHEDULES").upper().replace("&", "&amp;"))
    parts = []
    x, y = 40, 60
    sp = T["setpoints"].to_dict("records")
    s_, h_ = _table(x, y, "INSTRUMENT SET POINTS, ALARMS AND TRIPS (PROPOSED - CONFIRM IN HAZOP / SIL)",
                    [("TAG", 40, "tag"), ("SERVICE", 150, "description"), ("LOCATION", 72, "location"), ("UNIT", 34, "unit"), ("NORMAL", 32, "normal"),
                     ("SP", 30, "set_point"), ("L", 30, "alarm_low"), ("H", 46, "alarm_high"), ("LL", 24, "trip_low"), ("HH", 30, "trip_high"),
                     ("RANGE", 64, "range")], sp)
    parts.append(s_)
    y2 = y + h_ + 8
    vs = T["valve_schedule"].to_dict("records")
    s_, h2 = _table(x, y2, "VALVE SCHEDULE - FAIL POSITIONS", [("TAG", 40, "tag"), ("TYPE", 92, "type"), ("LINE", 76, "line"), ("SIZE", 28, "size"),
                                                               ("FAIL", 26, "fail_position"), ("REASON", 150, "reason"), ("BODY MOC", 50, "body_moc")], vs)
    parts.append(s_)
    y3 = y2 + h2 + 8
    dv = T["drains_vents"].to_dict("records")
    s_, h3 = _table(x, y3, "DRAINS AND VENTS", [("ITEM", 76, "item"), ("KIND", 58, "kind"), ("SIZE", 26, "size"), ("VALVE / END", 150, "valve"),
                                               ("ROUTED TO", 110, "to")], dv)
    parts.append(s_)
    nz = T["nozzles"].to_dict("records")
    ncol = [("EQUIP.", 36, "equipment"), ("NOZ.", 22, "nozzle"), ("PURPOSE", 96, "purpose"), ("SIZE", 44, "size"), ("RATING", 34, "rating"),
            ("CONNECTS TO", 76, "connects_to")]
    per = max(1, int((SH_ - 300 - 60) / 8.2))
    xn = x + 572
    for k in range(0, len(nz), per):
        s_, _ = _table(xn, y, "NOZZLE SCHEDULE" + (" (CONT.)" if k else ""), ncol, nz[k:k + per])
        parts.append(s_)
        xn += sum(c[1] for c in ncol) + 14
    notes = ["NOTES", "1. VALUES PROPOSED BY RULES FROM THE ASPEN STREAM RESULTS; ENGINEERING REVIEW, HAZOP AND SIL STUDY TO CONFIRM.",
             "2. RELIEF VALVE SIZE AND SET BY API 520 / 521 CALCULATION. CLEAN DP FOR PDI ALARMS MEASURED AT COMMISSIONING.",
             "3. ALL NOZZLES 150# RF UNLESS NOTED. INSTRUMENT, DRAIN AND VENT CONNECTIONS SOCKET-WELD IN HAZARDOUS SERVICE."]
    parts.append("".join(f'<text class="{"hd" if i == 0 else "l"}" x="{40}" y="{SH_ - 120 + i * 10}" style="font-size:{7 if i == 0 else 5.6}px">{n}</text>' for i, n in enumerate(notes)))
    css = ('*{fill:none;stroke:#000;stroke-width:1;stroke-linejoin:round}text{stroke:none;fill:#000;font-family:ISOCPEUR,Romans,"Arial Narrow",Arial,sans-serif;text-anchor:middle;font-size:6px}'
           '.w{fill:#fff}.f{fill:#000}.logo .f{stroke-width:.2}.logo-l{stroke-width:.3}.thin{stroke-width:.6}.l{text-anchor:start}.c{text-anchor:middle}.hd{font-size:7.5px;font-weight:bold;text-anchor:start}'
           '.tt{font-size:11px;font-weight:bold}.z{font-size:8px}')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SW_} {SH_}" width="{SW_}" height="{SH_}"><style>{css}</style>'
           f'<rect width="{SW_}" height="{SH_}" style="fill:#fff;stroke:none"/>{fr}{"".join(parts)}</svg>')
    out_dir = Path(out_dir)
    out_dir.mkdir(exist_ok=True)
    (out_dir / "pid_schedules.svg").write_text(svg, encoding="utf-8")
    (out_dir / "pid_schedules.html").write_text(f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>Schedules</title><style>@page{{size:{SW_ + 2}pt {SH_ + 2}pt;margin:0}}'
                                                f'html,body{{margin:0}}svg{{display:block;width:100%;height:auto}}</style></head><body>{svg}</body></html>', encoding="utf-8")
    engineering.to_excel(T, out_dir / "engineering_schedules.xlsx")
    return svg


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--streams", default="streams_aspen_10tpd.csv")
    ap.add_argument("--out", default="out_aspen_10tpd")
    ap.add_argument("--title", default="Aspen 10 TPD case")
    ap.add_argument("--rows", type=int, default=2, help="fold the process train into this many rows")
    ap.add_argument("--company", default="")
    ap.add_argument("--drawing-no", default="PID-001")
    ap.add_argument("--designed", default="")
    a = ap.parse_args()
    _, size, counts = draw(a.streams, a.title, HERE / a.out, a.rows, a.company, a.drawing_no, a.designed)
    print("sheet", [round(s) for s in size], counts)
