"""
pfd_to_pid.py - simple rule-based PFD -> draft P&ID generator.

Inputs  (data/):  equipment.csv, streams.csv   (the PFD, digitised as tables)
Outputs (out/):   pid.dot / pid.svg / pid.pdf  (draft P&ID drawing)
                  PID_deliverables.xlsx        (equipment, line list, instruments, valves, BOM)
                  knowledge_graph.json         (assets + connections, for dashboards / LLM agents)

Run:    python pfd_to_pid.py
        python pfd_to_pid.py --streams streams_gasifier_mode.csv --out out_gasifier
        python pfd_to_pid.py --impact FV-4002      (what is downstream of this asset?)

The output is a DRAFT for engineering review. It is not a HAZOP-reviewed P&ID.
"""
import argparse
import json
import math
import shutil
import subprocess
from pathlib import Path

import networkx as nx
import pandas as pd

HERE = Path(__file__).parent
DATA, OUT = HERE / "data", HERE / "out"

# ----------------------------------------------------------------------------
# 1. RULES  (edit these tables to change what the generator adds)
# ----------------------------------------------------------------------------
# Instruments added per equipment type: (ISA letters, description, location, drives equipment?)
EQUIPMENT_RULES = {
    "hopper": [("LSH", "High level switch", "FIELD", False), ("LSL", "Low level switch", "FIELD", False)],
    "screw_heated": [("SIC", "Screw speed control (VFD)", "DCS", True),
                     ("TIC", "Band heater temperature control", "DCS", True),
                     ("TSHH", "Heater over-temperature trip", "SIS", True)],
    "electric_heater_vessel": [("TIC", "Shot temperature control (inductive heater)", "DCS", True),
                               ("TSHH", "Over-temperature trip", "SIS", True),
                               ("LSL", "Low shot level switch", "FIELD", False)],
    "auger_reactor": [("TI", "Zone temperatures T1-T4 (4 elements)", "DCS", False),
                      ("TIC", "Reactor heater temperature control", "DCS", True),
                      ("SIC", "Auger speed control (VFD)", "DCS", True),
                      ("PI", "Reactor pressure", "DCS", False),
                      ("PSHH", "High pressure trip", "SIS", True)],
    "cyclone": [("PDI", "Cyclone pressure drop", "DCS", False)],
    "condenser": [("TIC", "Vapour outlet temperature control (cooling water valve)", "DCS", False),
                  ("LG", "Level gauge, condensate boot", "FIELD", False)],
    "tank": [("LI", "Tank level", "DCS", False), ("LSH", "High level switch", "FIELD", False)],
    "bin": [("LSH", "High level switch", "FIELD", False)],
    "blower": [("PIC", "Suction pressure control (blower VFD)", "DCS", True),
               ("FI", "Discharge flow", "DCS", False)],
    "flare": [("BSL", "Pilot flame failure switch", "SIS", False)],
    "cooling_tower": [("LIC", "Basin level control (make-up)", "DCS", True), ("TI", "Supply temperature", "DCS", False)],
    "solids_cooler": [("TI", "Solids outlet temperature", "DCS", False)],
    "solids_separator": [],
    "heat_exchanger": [("TIC", "Outlet temperature control", "DCS", True)],
    "gasifier": [("TI", "POX zone temperature", "DCS", False),
                 ("TI", "Char bed temperature", "DCS", False),
                 ("PDI", "Char bed pressure drop", "DCS", False),
                 ("PI", "Gasifier pressure", "DCS", False),
                 ("TSHH", "High temperature trip (closes oxidant XV)", "SIS", False),
                 ("PSV", "Pressure safety valve", "FIELD", False)],
    "gas_cooler": [("TIC", "Gas outlet temperature control (cooling water valve), keep above dew point", "DCS", False),
                   ("TI", "Gas inlet temperature", "DCS", False)],
    "bag_filter": [("PDIC", "Filter pressure drop, starts nitrogen back-flush", "DCS", True),
                   ("TI", "Filter temperature", "DCS", False),
                   ("TSHH", "High temperature trip (protects filter bags)", "SIS", True)],
    "filter": [("PDI", "Filter pressure drop", "DCS", False)],
    "knockout_drum": [("LIC", "Condensate level control (drain valve)", "DCS", False),
                      ("LSHH", "High-high level trip (protects the engine from liquid carry-over)", "SIS", False),
                      ("TI", "Gas outlet temperature", "DCS", False)],
    "stack": [], "air_intake": [],
    "package": [],  # vendor package: instruments by vendor
}

# In-line items added per stream hint (streams.csv column `pid_hints`, separated by ';')
#   code: (kind, description, glyph on drawing)
HINTS = {
    "ISO": ("valve", "Manual isolation valve", "⋈"),
    "CHK": ("valve", "Check valve", "⧓"),
    "PCV": ("valve", "Self-acting pressure regulator", "⋈"),
    "XV": ("valve", "On/off shutdown valve", "⋈"),
    "TV": ("valve", "Temperature control valve", "⋈"),
    "LV": ("valve", "Level control valve", "⋈"),
    "FIC": ("loop", "Flow control loop (FT + FIC + FV)", "⋈"),
    "SG": ("valve", "Slide gate (solids)", "▬"),
    "RV": ("valve", "Rotary airlock valve (solids)", "✲"),
    "FA": ("fitting", "Flame arrestor", "▣"),
    "TI": ("inst", "Line temperature", ""),
    "AI": ("inst", "Gas analyser (composition)", ""),
}
VALVE_PREFIX = {"ISO": "HV", "CHK": "CK", "PCV": "PCV", "XV": "XV", "TV": "TV", "LV": "LV", "SG": "SG", "RV": "RV", "FA": "FA"}

# Preliminary line sizing assumptions (replace with real stream properties when available)
GAS_MW = {"PV": 60, "PG": 30, "NCG": 30, "OX": 32, "AIR": 29, "OXC": 38, "SG": 18.7, "EG": 29, "N2": 28}   # kg/kmol, assumed
GAS_VELOCITY, LIQ_VELOCITY, LIQ_DENSITY = 20.0, 1.5, 1000.0              # m/s, m/s, kg/m3
MIN_PROCESS_ID = 26.6        # smallest process line 1" (mechanical strength, plugging); instrument and drain connections are separate
NPS = [(0.5, 15.8), (0.75, 20.9), (1, 26.6), (1.5, 40.9), (2, 52.5), (3, 77.9),
       (4, 102.3), (6, 154.1), (8, 202.7), (10, 254.5), (12, 303.2), (14, 333.3), (16, 381.0), (18, 428.7),
       (20, 477.8), (24, 574.6), (30, 730.0), (36, 876.0)]               # (inch, approx. standard-wall ID mm)


# Equipment types that have an electric motor drive (listed in the motor list and BOM, drawn on the sheet)
MOTOR_TYPES = {"screw_heated": "Screw feeder drive (motor + gearbox)", "auger_reactor": "Auger drive (motor + gearbox)", "blower": "Blower drive",
               "solids_separator": "Separator drive", "cooling_tower": "Cooling tower fan drive"}
NB = {0.5: 15, 0.75: 20, 1: 25, 1.5: 40, 2: 50, 3: 80, 4: 100, 6: 150, 8: 200, 10: 250, 12: 300, 14: 350, 16: 400, 18: 450, 20: 500, 24: 600, 30: 750, 36: 900}


def to_nb(size):
    """'6"' -> '150 NB' (nominal bore in mm). Returns '' when the size is not a plain inch value."""
    try:
        return f"{NB[float(str(size).replace(chr(34), ''))]} NB"
    except (ValueError, KeyError):
        return ""


EXTRA_STREAM_COLS = ["vol_m3h", "density_kgm3", "enthalpy_kW", "composition"]   # carried through when the stream table has them


# ----------------------------------------------------------------------------
# 2. HELPERS
# ----------------------------------------------------------------------------
def size_line(s):
    """Velocity-based preliminary pipe size. Returns '' for solids or missing data."""
    if s.phase == "solid" or pd.isna(s.flow_kgh):
        return ""
    vol = getattr(s, "vol_m3h", None)
    if vol is not None and pd.notna(vol):          # real volume flow from the process simulation
        vel = GAS_VELOCITY if s.phase == "gas" else LIQ_VELOCITY
        d_mm = max(math.sqrt(4 * (vol / 3600) / vel / math.pi) * 1000, MIN_PROCESS_ID)
        return next((f'{n:g}"' for n, idm in NPS if idm >= d_mm), '>36"')
    if s.phase == "gas":
        if pd.isna(s.temp_C) or pd.isna(s.press_bar):
            return ""
        rho = s.press_bar * 1e5 * GAS_MW.get(s.service, 29) / 1000 / (8.314 * (s.temp_C + 273.15))
        vel = GAS_VELOCITY
    else:
        rho, vel = LIQ_DENSITY, LIQ_VELOCITY
    d_mm = max(math.sqrt(4 * (s.flow_kgh / 3600 / rho) / vel / math.pi) * 1000, MIN_PROCESS_ID)
    return next((f'{n:g}"' for n, idm in NPS if idm >= d_mm), '>36"')


WET_SERVICES = {"SG", "PG", "PV", "NCG", "CD", "BO", "WW"}      # wet / acidic / tarry gases and condensates


def pipe_spec(s):
    """Pipe class (see engineering.PIPE_CLASSES): SS1 oxidant (O2-clean SS316L), SS3 hot >425 C (SS310S),
    SS2 wet / acidic or 200-425 C (SS316L), CS1 dry non-corrosive <=200 C (carbon steel)."""
    t = None if pd.isna(s.temp_C) else s.temp_C
    if s.service in ("OX", "OXC"):
        return "SS1"
    if t is not None and t > 425:
        return "SS3"
    if s.service in WET_SERVICES or (t is not None and t > 200):
        return "SS2"
    return "CS1"


def insulation_code(s):
    """H = heat conservation (process must stay hot), P = personnel protection (>60 C, heat not needed), N = none."""
    t = None if pd.isna(s.temp_C) else s.temp_C
    if t is None or t <= 60:
        return "N"
    if s.service in ("PG", "PV", "OXC", "OX") or (s.service == "SG" and t > 300):   # keep above tar dew point / keep pre-heat
        return "H"
    return "P"


class Tagger:
    """Loop numbers: area digit + 3-digit running number, e.g. area 400 -> 4001, 4002...
    With a register (dict key -> number) tags are stable: an item keeps its number when other items are added or removed,
    and numbers of deleted items are not reused."""
    def __init__(self, register=None):
        self.count = {}
        self.reg = register

    def next(self, area, key=None):
        if self.reg is None or key is None:
            self.count[area] = self.count.get(area, 0) + 1
            return f"{int(area) // 100}{self.count[area]:03d}"
        if key not in self.reg:
            d = str(int(area) // 100)
            nums = [int(v) for v in self.reg.values() if len(v) == 4 and v.startswith(d)]
            self.reg[key] = str(max(nums) + 1) if nums else f"{d}001"
        return self.reg[key]


# ----------------------------------------------------------------------------
# 3. BUILD THE P&ID MODEL (a graph)
# ----------------------------------------------------------------------------
def build(streams_file="streams.csv"):
    eq = pd.read_csv(DATA / "equipment.csv")
    st = pd.read_csv(DATA / streams_file, dtype={"no": str})
    duties = DATA / streams_file.replace("streams_", "duties_")
    if duties.exists() and duties.name != streams_file:      # heat duties / power from the process simulation
        d = pd.read_csv(duties)
        d = d.assign(tag=d.tag.str.split(" + ", regex=False)).explode("tag")
        txt = d.groupby("tag").apply(lambda g: "; ".join(f"{i}: {k:g} kW" for i, k in zip(g["item"], g["kW"])), include_groups=False)
        eq["duty"] = eq.tag.map(txt).fillna("")
    import workflow                                         # corrections recorded in the review workflow
    eq, st, info = workflow.apply(streams_file, eq, st)
    regf = DATA / ("tags_" + Path(streams_file).stem.replace("streams_", "") + ".csv")
    reg = dict(pd.read_csv(regf, dtype=str).values) if regf.exists() else {}
    before = dict(reg)
    out = build_frames(eq, st, case=streams_file, info=info, register=reg)
    if reg != before:                                       # new items got numbers: keep them for next time
        try:
            pd.DataFrame(sorted(reg.items()), columns=["key", "number"]).to_csv(regf, index=False)
        except OSError:
            pass
    return out


def _catalog():
    """Instrument letters -> (description, location, acts on its equipment), from the rule tables."""
    cat = {}
    for rules in EQUIPMENT_RULES.values():
        for letters, desc, loc, drives in rules:
            cat.setdefault(letters, (desc, loc, drives))
    return cat


def _instrument_entry(letters):
    cat = _catalog()
    if letters in cat:
        return (letters,) + cat[letters]
    loc = "SIS" if letters.endswith(("HH", "LL")) else ("FIELD" if letters.endswith(("G", "SH", "SL")) or letters in ("PSV",) else "DCS")
    drives = loc == "SIS" or (len(letters) > 2 and letters[-1] == "C")
    return (letters, f"{letters} (added by correction)", loc, drives)


def build_frames(eq, st, case=None, info=None, register=None):
    """Same as build(), but from DataFrames (used by make_dataset.py to generate many variants)."""
    info = info or {}
    case_name = case or "stream table"
    st = st.fillna({"pid_hints": ""})
    eq = eq[eq.tag.isin(set(st["from"]) | set(st["to"]))].reset_index(drop=True)   # only equipment in use
    area_of = dict(zip(eq.tag, eq.area))
    G, tagger = nx.MultiDiGraph(), Tagger(register)
    lines, instruments, valves = [], [], []

    def add_inst(letters, desc, loc, area, on, loop=None, source="", key=None):
        tag = f"{letters}-{loop or tagger.next(area, key)}"
        G.add_node(tag, kind="instrument", letters=letters, desc=desc, loc=loc, area=area, on=on, source=source)
        instruments.append(dict(tag=tag, function=letters, description=desc, location=loc, attached_to=on, area=area, source=source))
        return tag

    # equipment + their instruments
    for e in eq.itertuples():
        G.add_node(e.tag, kind="equipment", name=e.name, type=e.type, area=e.area, source="equipment table (data/equipment.csv)",
                   **({"duty": e.duty} if getattr(e, "duty", "") else {}))
        override = getattr(e, "instruments", "")
        if isinstance(override, str) and override.strip():
            rules = [_instrument_entry(x.strip()) for x in override.split(";") if x.strip()]
            src = f"correction {info.get('inst_source', {}).get(e.tag, '')}".strip() if e.tag in info.get("inst_source", {}) else "equipment table instrument list"
        else:
            rules = EQUIPMENT_RULES.get(e.type, [])
            src = f"rule set for equipment type '{e.type}' (EQUIPMENT_RULES)"
        seen = {}
        for letters, desc, loc, drives in rules:
            seen[letters] = seen.get(letters, 0) + 1
            t = add_inst(letters, desc, loc, e.area, e.tag, source=src, key=f"{e.tag}|{letters}|{seen[letters]}")
            if letters == "PSV":
                valves.append(dict(tag=t, type="Pressure safety valve", line=e.tag, size="TBD", area=e.area, source=src))
            if drives:   # controller / trip acts on the equipment
                G.add_edge(t, e.tag, kind="trip" if loc == "SIS" else "signal")
            else:        # measurement taken from the equipment
                G.add_edge(e.tag, t, kind="signal")

    def find(equip, letters):
        return next((n for n, d in G.nodes(data=True)
                     if d["kind"] == "instrument" and d["on"] == equip and d["letters"] == letters), None)

    # streams -> lines with in-line valves / loops
    for s in st.itertuples():
        area = area_of[s._2]  # _2 == 'from'
        src, dst = s._2, s.to
        seq = tagger.next(area, f"line|{s.no}")
        size = size_line(s)
        size_src = "velocity sizing from simulation volume flow" if getattr(s, "vol_m3h", None) is not None and not pd.isna(getattr(s, "vol_m3h", None)) \
            else ("velocity sizing, assumed gas properties" if size else "no flow data")
        ov = getattr(s, "size_override", "")
        if isinstance(ov, str) and ov.strip():
            size, size_src = ov.strip(), f"correction {info.get('size_source', {}).get(str(s.no), '')}".strip()
        item_src = f"correction {info['hint_source'][str(s.no)]}" if str(s.no) in info.get("hint_source", {}) else f"pid_hints of stream {s.no} ({case_name})"
        if s.phase == "solid":
            line_no = f"{s.service}-{seq}"
        else:
            ins = insulation_code(s)
            line_no = f'{size or "TBD"}-{s.service}-{seq}-{pipe_spec(s)}-{ins}'
        lines.append(dict(line_no=line_no, pfd_stream=s.no, service=s.service, phase=s.phase,
                          from_=src, to=dst, flow_kgh=s.flow_kgh, temp_C=s.temp_C, press_bar=s.press_bar,
                          size=size or ("n/a (solids)" if s.phase == "solid" else "TBD - data missing"),
                          description=s.description, source=f"{case_name}, stream {s.no}", size_source=size_src,
                          **{k: getattr(s, k) for k in EXTRA_STREAM_COLS if hasattr(s, k)}))
        chain = [src]
        occ = {}
        for code in [h.strip() for h in s.pid_hints.split(";") if h.strip()]:
            kind, desc, glyph = HINTS[code]
            occ[code] = occ.get(code, 0) + 1
            ikey = f"{s.no}|{code}|{occ[code]}"
            if code in ("TI", "AI"):
                t = add_inst(code, f"{desc}, line {line_no}", "DCS", area, line_no, source=item_src, key=ikey)
                tap = f"tap-{t}"
                G.add_node(tap, kind="tap", area=area)
                G.add_edge(tap, t, kind="signal")
                chain.append(tap)
                continue
            if code == "FIC":
                loop = tagger.next(area, ikey)
                ft = add_inst("FT", f"Flow transmitter, line {line_no}", "FIELD", area, line_no, loop, source=item_src)
                fic = add_inst("FIC", f"Flow controller, line {line_no}", "DCS", area, line_no, loop, source=item_src)
                vtag = f"FV-{loop}"
                tap = f"tap-{ft}"
                G.add_node(tap, kind="tap", area=area)
                G.add_edge(tap, ft, kind="signal")
                G.add_edge(ft, fic, kind="signal")
                G.add_edge(fic, vtag, kind="signal")
                chain.append(tap)
                desc = "Flow control valve"
            elif code in ("TV", "LV"):   # driven by the TIC / LIC on the upstream equipment
                tic = find(src, code[0] + "IC")
                vtag = f"{code}-{tic.split('-')[1]}" if tic else f"{code}-{tagger.next(area, ikey)}"
                if tic:
                    G.add_edge(tic, vtag, kind="signal")
            else:
                vtag = f"{VALVE_PREFIX[code]}-{tagger.next(area, ikey)}"
            if code == "XV":     # tripped by the destination's (or source's) safety switch
                trip = find(dst, "TSHH") or find(dst, "PSHH") or find(src, "PSHH") or find(src, "LSHH")
                if trip:
                    G.add_edge(trip, vtag, kind="trip")
            G.add_node(vtag, kind="valve", desc=desc, glyph=glyph, area=area, line=line_no, source=item_src)
            valves.append(dict(tag=vtag, type=desc, line=line_no, size=size or "TBD", area=area, source=item_src))
            chain.append(vtag)
        chain.append(dst)
        for a, b in zip(chain, chain[1:]):
            G.add_edge(a, b, kind="process", line=line_no, phase=s.phase, service=s.service, stream=s.no,
                       label=line_no if b == dst else "")
    motors = [dict(drives=e.tag, description=MOTOR_TYPES[e.type], area=e.area) for e in eq.itertuples() if e.type in MOTOR_TYPES]
    motors += [dict(drives=v["tag"], description="Rotary airlock drive (motor + gearbox)", area=v["area"]) for v in valves if v["tag"].startswith("RV-")]
    G.graph["motors"] = [dict(tag=f"M-{i + 1:02d}", **m) for i, m in enumerate(motors)]
    G.graph["fail_override"] = {k: v for k, v in info.get("fail_override", {}).items()}
    G.graph["applied_corrections"] = info.get("applied", [])
    G.graph["case"] = case
    return G, eq, pd.DataFrame(lines).rename(columns={"from_": "from"}), pd.DataFrame(instruments), pd.DataFrame(valves)


def bom_summary(G, eq, lines, inst, valves):
    """Bill of material grouped by item: one row per distinct item with its quantity and the tags it covers."""
    rows = [dict(category="Equipment", item=e.name, size_spec=e.type, qty=1, tags=e.tag) for e in eq.itertuples()]
    motors = pd.DataFrame(G.graph.get("motors", []))
    if len(motors):
        for d, g in motors.groupby("description"):
            rows.append(dict(category="Motor", item=d, size_spec="kW to be defined", qty=len(g), tags=", ".join(g.tag)))
    if len(valves):
        for (t, sz), g in valves.groupby(["type", "size"]):
            nb = to_nb(sz)
            rows.append(dict(category="Valve / fitting", item=t, size_spec=f"{sz} ({nb})" if nb else str(sz), qty=len(g), tags=", ".join(g.tag)))
    if len(inst):
        for f, g in inst[inst.function != "PSV"].groupby("function"):
            where = g.location.map({"FIELD": "field", "DCS": "DCS", "SIS": "safety system"}).iloc[0]
            rows.append(dict(category="Instrument", item=f"{f} ({where})", size_spec="range to be defined", qty=len(g), tags=", ".join(g.tag)))
    pipe = lines[lines.phase != "solid"]
    for (sz, svc), g in pipe.groupby(["size", "service"]):
        spec = g.line_no.iloc[0].split("-")[-2] if g.line_no.iloc[0].count("-") >= 4 else ""
        nb = to_nb(sz)
        rows.append(dict(category="Piping", item=f"Pipe, service {svc}", size_spec=f"{sz} ({nb}), spec {spec}" if nb else f"{sz}, spec {spec}",
                         qty=len(g), tags=", ".join(g.line_no)))
    for svc, g in lines[lines.phase == "solid"].groupby("service"):
        rows.append(dict(category="Solids transfer", item=f"Chute / conveyor, service {svc}", size_spec="n/a", qty=len(g), tags=", ".join(g.line_no)))
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# 4. DRAW  (Graphviz DOT; rendered to SVG/PDF if `dot` is installed)
# ----------------------------------------------------------------------------
AREA_NAMES = {100: "AREA 100 - FEED & PYROLYSIS", 200: "AREA 200 - CONDENSING & GAS HANDLING",
              300: "AREA 300 - SOLIDS HANDLING", 400: "AREA 400 - GASIFICATION", 500: "AREA 500 - POWER"}


def to_dot(G, title="all modes", rankdir="LR"):
    o = ['digraph PID {', f'rankdir={rankdir}; ' 'ratio=auto; ' 'nodesep=0.3; ranksep=0.6; newrank=true; fontname="DejaVu Sans"; labelloc=t; fontsize=22;',
         'label="DRAFT P&ID - 10 TPD Two-Stage Biomass Gasification, {title} (auto-generated from PFD, for review)";'.replace("{title}", title),
         'node [fontname="DejaVu Sans", fontsize=10]; edge [fontname="DejaVu Sans", fontsize=8];']
    for area in sorted({d["area"] for _, d in G.nodes(data=True)}):
        o.append(f'subgraph cluster_{area} {{ label="{AREA_NAMES.get(area, area)}"; style=dashed; color=gray50; fontsize=14;')
        for n, d in G.nodes(data=True):
            if d["area"] != area:
                continue
            if d["kind"] == "equipment":
                o.append(f'"{n}" [shape=box, style="filled,bold", fillcolor="#e8eef7", margin="0.25,0.18", '
                         f'label=<<b>{n}</b><br/>{d["name"].replace("&", "&amp;")}>];')
            elif d["kind"] == "instrument":
                shape = {"FIELD": "circle", "DCS": "Mcircle", "SIS": "Mdiamond"}[d["loc"]]
                color = "red" if d["loc"] == "SIS" else "black"
                letters, num = n.split("-")
                w = 1.0 if shape == "Mdiamond" else 0.72
                o.append(f'"{n}" [shape={shape}, fixedsize=true, width={w}, height={w}, fontsize=8, color={color}, '
                         f'label="{letters}\\n{num}", tooltip="{d["desc"].replace(chr(34), "in")}"];')
            elif d["kind"] == "valve":
                o.append(f'"{n}" [shape=plain, label=<<font point-size="20">{d["glyph"]}</font><br/>'
                         f'<font point-size="8">{n}</font>>, tooltip="{d["desc"]}"];')
            else:  # tap point on a line
                o.append(f'"{n}" [shape=point, width=0.07];')
        o.append("}")
    for a, b, d in G.edges(data=True):
        if d["kind"] == "process":
            color, pen = {"solid": ("#8b5a2b", 3)}.get(d["phase"], ("black", 2))
            if d["service"] in ("CWS", "CWR"):
                color, pen = "#1f6fb2", 1.2
            head = "normal" if G.nodes[b]["kind"] == "equipment" else "none"
            o.append(f'"{a}" -> "{b}" [color="{color}", penwidth={pen}, arrowhead={head}, label="{d["label"].replace(chr(34), chr(92) + chr(34))}"];')
        else:
            color = "red" if d["kind"] == "trip" else "gray30"
            o.append(f'"{a}" -> "{b}" [style=dashed, color={color}, arrowsize=0.6, weight=3];')
    o.append('legend [shape=plain, label=<<table border="1" cellborder="0" cellspacing="2"><tr><td><b>LEGEND (ISA-5.1 based)</b></td></tr>'
             '<tr><td align="left">○ field instrument   ⦵ DCS function   ◇ (red) safety function</td></tr>'
             '<tr><td align="left">⋈ valve   ⧓ check valve   ▬ slide gate   ✲ rotary airlock   ▣ flame arrestor</td></tr>'
             '<tr><td align="left">━ process (black)   ━ solids (brown)   ─ cooling water (blue)   ┄ signal   ┄ trip (red)</td></tr>'
             '<tr><td align="left">Line no.: size-service-seq-spec-insulation. See data/symbols_legend.csv</td></tr></table>>];')
    o.append("}")
    return "\n".join(o)


# ----------------------------------------------------------------------------
# 5. WHAT-IF: what does an asset feed / influence?
# ----------------------------------------------------------------------------
def impact(G, tag):
    if tag not in G:
        return f"{tag} not found."
    d = G.nodes[tag]
    proc = nx.MultiDiGraph([(a, b, x) for a, b, x in G.edges(data=True) if x["kind"] == "process"])
    start = [tag] if tag in proc else [b for _, b in G.out_edges(tag)]   # controller -> what it drives
    down, lines = set(), set()
    for s in start:
        if s in proc:
            reach = nx.descendants(proc, s) | {s}
            down |= {n for n in reach if G.nodes[n]["kind"] == "equipment"}
            lines |= {x["line"] for a, b, x in proc.edges(data=True) if a in reach}
    ctrl = [a for a, _, x in G.in_edges(tag, data=True) if x["kind"] in ("signal", "trip")]
    txt = [f"Asset: {tag} ({d.get('desc') or d.get('name')})"]
    if d.get("line"):
        txt.append(f"Installed on line: {d['line']}")
    if ctrl:
        txt.append(f"Controlled / tripped by: {', '.join(ctrl)}")
    txt.append(f"Downstream equipment that can be affected ({len(down - {tag})}): {', '.join(sorted(down - {tag})) or '-'}")
    txt.append(f"Downstream lines ({len(lines)}): {', '.join(sorted(lines)) or '-'}")
    txt.append("Note: this is connectivity only. Quantifying the effect (flow, temperature, syngas composition) "
               "needs valve Cv data and a process model.")
    return "\n".join(txt)


def scale_streams(st, factor):
    """Capacity what-if: scale every flow-proportional column. Valid only at unchanged ratios, temperatures and pressures."""
    out = st.copy()
    for c in ("flow_kgh", "vol_m3h", "enthalpy_kW"):
        if c in out:
            out[c] = out[c] * factor
    return out


# ----------------------------------------------------------------------------
# 6. MAIN
# ----------------------------------------------------------------------------
def generate(streams_file="streams.csv", out_name="out", rankdir="LR"):
    """Build the model and write every output into the folder `out_name`. Returns (G, eq, lines, inst, valves)."""
    global OUT
    OUT = HERE / out_name
    G, eq, lines, inst, valves = build(streams_file)
    OUT.mkdir(exist_ok=True)

    title = Path(streams_file).stem.replace("streams_", "").replace("_", " ").replace("streams", "all modes")
    (OUT / "pid.dot").write_text(to_dot(G, title, rankdir), encoding="utf-8")
    if shutil.which("dot"):
        for fmt in ("svg", "pdf", "png"):
            subprocess.run(["dot", f"-T{fmt}", str(OUT / "pid.dot"), "-o", str(OUT / f"pid.{fmt}")], check=True)
    else:
        print("Graphviz 'dot' not found - install Graphviz to render out/pid.dot (see README).")

    bom = pd.concat([
        eq.assign(category="Equipment", description=eq.name)[["category", "tag", "description", "area"]],
        valves.assign(category="Valve", description=valves.type + " | " + valves["size"] + " | line " + valves.line)[["category", "tag", "description", "area"]],
        inst.assign(category="Instrument", description=inst.description)[["category", "tag", "description", "area"]],
    ]).assign(qty=1)
    motors = pd.DataFrame(G.graph.get("motors", []), columns=["tag", "drives", "description", "area"])
    bom = pd.concat([bom, motors.assign(category="Motor", description=motors.description + " for " + motors.drives, qty=1)[["category", "tag", "description", "area", "qty"]]])
    with pd.ExcelWriter(OUT / "PID_deliverables.xlsx", engine="openpyxl") as xl:
        for name, df in [("Equipment", eq), ("Line list", lines), ("Instrument index", inst),
                         ("Valve list", valves), ("Motor list", motors), ("BOM", bom), ("BOM summary", bom_summary(G, eq, lines, inst, valves)),
                         ("Legend", pd.read_csv(DATA / "symbols_legend.csv"))]:
            df.to_excel(xl, sheet_name=name, index=False)
            ws = xl.sheets[name]
            for i, col in enumerate(df.columns):
                ws.column_dimensions[chr(65 + i)].width = min(60, max(12, int(df[col].astype(str).str.len().max() if len(df) else 0) + 2))

    kg = nx.node_link_data(G, edges="links")
    (OUT / "knowledge_graph.json").write_text(json.dumps(kg, indent=1, default=str), encoding="utf-8")
    return G, eq, lines, inst, valves


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--impact", help="asset tag to analyse, e.g. FV-4002")
    ap.add_argument("--streams", default="streams.csv", help="stream table in data/, e.g. streams_gasifier_mode.csv")
    ap.add_argument("--out", default="out", help="output folder")
    ap.add_argument("--rankdir", default="LR", choices=["LR", "TB"], help="drawing direction: LR (wide) or TB (tall)")
    args = ap.parse_args()
    if args.impact:
        print(impact(build(args.streams)[0], args.impact))
        return
    G, eq, lines, inst, valves = generate(args.streams, args.out, args.rankdir)
    print(f"Equipment {len(eq)} | lines {len(lines)} | instruments {len(inst)} | valves {len(valves)}")
    print(f"Outputs written to {OUT}")


if __name__ == "__main__":
    main()
