"""
engineering.py - engineering data the PFD does not give, defined by rules from the stream data and editable by people.

For every case it produces:
  line_designation()   line number, size, service, from / to, pipe class and MOC, insulation (code, purpose, thickness),
                       operating and design temperature / pressure, test pressure, end connection (flanged / socket-weld /
                       threaded), valves on the line, drains and vents
  valve_schedule()     fail position (FC / FO / FL) with the reason, body MOC, end connection, actuator
  setpoints()          normal value from the simulation, set point, alarm and trip limits, instrument range
  nozzles()            nozzle schedule per equipment item: process, instrument, drain, vent, relief and manway nozzles
  drains_vents()       equipment and line drains and vents with size, valve and end
Rules are written below in plain tables so an engineer can change them. Values that people change are kept in
data/engineering_<stem>.csv (item, field, value, reason, by, status) and win over the rules. With an AI key the
copilot can propose values for items still "TBD"; proposals stay "proposed" until an engineer approves them.

All values are preliminary design-basis proposals for review (HAZOP, SIL, relief and stress studies decide the final
values). Rules follow common practice (ASME B31.3 for piping, ISA-5.1 tagging, API 520/521 for relief), not a project spec.
"""
import json
import math
import os
import re
from pathlib import Path

import pandas as pd

import pfd_to_pid as p
import workflow

DATA = Path(__file__).parent / "data"
ATM = 1.01325

# ------------------------------------------------------------------ pipe classes, MOC and abbreviations
PIPE_CLASSES = {
    "CS1": dict(moc="CS", pipe="ASTM A106 Gr.B seamless", fittings="ASTM A234 WPB / A105", rating="150# RF", t_max=200, corrosion_mm=1.5,
                use="Dry, non-corrosive: combustion air, cooling water, nitrogen, instrument air"),
    "SS1": dict(moc="SS316L-OC", pipe="ASTM A312 TP316L, oxygen-cleaned", fittings="ASTM A403 WP316L / A182 F316L", rating="150# RF", t_max=450,
                corrosion_mm=0.0, use="Oxidant O2 + CO2 (45:55). Clean for oxygen service; limit velocity at impingement points; "
                                      "consider Monel 400 / Inconel 625 at valves and tees above 200 C"),
    "SS2": dict(moc="SS316L", pipe="ASTM A312 TP316L", fittings="ASTM A403 WP316L / A182 F316L", rating="150# RF", t_max=425, corrosion_mm=0.0,
                use="Wet / acidic syngas and condensate (CO2, H2S traces, tars), and 200-425 C service"),
    "SS3": dict(moc="SS310S", pipe="ASTM A312 TP310S", fittings="ASTM A403 WP310S / A182 F310", rating="150# RF", t_max=900, corrosion_mm=0.0,
                use="Hot raw syngas, pyrolysis gas and exhaust 425-900 C (oxidation and carburisation resistance)"),
}
MOC_ABBREVIATIONS = {
    "CS": "Carbon steel", "SS316L": "Stainless steel 316L (low carbon)", "SS316L-OC": "Stainless steel 316L, cleaned for oxygen service",
    "SS310S": "Stainless steel 310S (25Cr-20Ni, high temperature)", "RL-CS": "Refractory-lined carbon steel", "MW": "Mineral wool insulation",
    "CaSi": "Calcium silicate insulation", "RF": "Raised face flange", "SW": "Socket-weld", "BW": "Butt-weld", "NPT": "Threaded (taper pipe thread)",
    "NB": "Nominal bore (mm)", "FC": "Fail closed", "FO": "Fail open", "FL": "Fail locked (stays in last position)",
    "OC": "Oxygen-cleaned", "DT / DP": "Design temperature / design pressure", "HT": "Hydrostatic test",
}
SERVICE_NAMES = {"BM": "Biomass", "CH": "Char", "PG": "Pyrolysis gas", "SG": "Syngas", "OXC": "Oxidant (O2 + CO2)", "OX": "Oxygen", "AIR": "Air",
                 "EG": "Exhaust gas", "CD": "Condensate", "AS": "Ash", "CWS": "Cooling water supply", "CWR": "Cooling water return", "N2": "Nitrogen",
                 "PV": "Pyrolysis vapour", "NCG": "Non-condensable gas", "BO": "Bio-oil", "WW": "Waste water"}
HAZARDOUS = {"SG", "PG", "PV", "NCG", "OXC", "OX", "EG", "BO", "CD"}    # flammable, toxic (CO), oxidising, or condensate saturated with syngas
LINE_NOMENCLATURE = 'SIZE - SERVICE - SEQUENCE - PIPE CLASS - INSULATION,  e.g.  6"-SG-4031-SS2-N  (sequence: first digit = plant area)'

# ------------------------------------------------------------------ insulation
INSULATION_CODES = {"H": "Heat conservation", "P": "Personnel protection", "N": "None"}
INS_THICKNESS = [(150, 40), (250, 50), (350, 65), (450, 80), (550, 100), (650, 115), (750, 130), (900, 150)]   # (max C, mm mineral wool)


def ins_thickness(t):
    if t is None or pd.isna(t) or t <= 60:
        return 0
    return next((mm for tmax, mm in INS_THICKNESS if t <= tmax), 180)


# ------------------------------------------------------------------ helpers
def size_in(size):
    try:
        return float(str(size).replace('"', ""))
    except ValueError:
        return None


def barg(press_bar_abs):
    return None if press_bar_abs is None or pd.isna(press_bar_abs) else round(press_bar_abs - ATM, 3)


def design_conditions(t, p_abs, service):
    """Design T: operating + 30 C (rounded up to 5 C). Design P: flammable gas systems near atmosphere 3.5 bar g
    (deflagration margin, typical for small gasifiers - confirm with a deflagration study); otherwise max(op + 1 bar, 1.1 x op)."""
    dt = None if t is None or pd.isna(t) else int(math.ceil((t + 30) / 5) * 5)
    pg = barg(p_abs)
    if pg is None:
        return dt, None
    if service in HAZARDOUS:
        dp = max(3.5, round(1.1 * pg, 1), round(pg + 1, 1))
    else:
        dp = max(round(pg + 1, 1), round(1.1 * pg, 1), 3.5 if service in ("CWS", "CWR") else 1.0)
    return dt, dp


def end_connection(size, service, spec):
    s = size_in(size)
    if s is None:
        return "Flanged (solids chute / per vendor)"
    if s <= 1.5:
        if service in HAZARDOUS or spec == "SS1":
            return "Socket-weld (SW) + flanged at valves; no threads (flammable / toxic / oxidant)"
        return "Threaded (NPT), seal-welded above 200 C"
    return "Butt-weld (BW) with RF flanges at equipment, valves and instruments"


# ------------------------------------------------------------------ overrides (people and approved AI proposals)
OV_COLS = ["item", "field", "value", "reason", "by", "status"]


def overrides(case):
    f = DATA / f"engineering_{workflow.stem_of(case)}.csv"
    if not f.exists():
        return pd.DataFrame(columns=OV_COLS)
    df = pd.read_csv(f, dtype=str).fillna("")
    for c in OV_COLS:
        if c not in df:
            df[c] = ""
    return df[OV_COLS]


def set_override(case, item, field, value, reason, by, status="applied"):
    df = overrides(case)
    df = df[~((df["item"] == item) & (df["field"] == field) & (df["status"] == status))]
    df.loc[len(df)] = [item, field, str(value), reason, by or "-", status]
    df.to_csv(DATA / f"engineering_{workflow.stem_of(case)}.csv", index=False)
    workflow.log(case, by, f"engineering value {status}", f"{item} {field} = {value} ({reason})")


def _apply(df, case, key):
    ov = overrides(case)
    ov = ov[ov.status == "applied"]
    if not len(ov) or not len(df):
        return df
    df = df.copy()
    df["set_by"] = df.get("set_by", "rule")
    for r in ov.itertuples():
        m = df[key].astype(str) == r.item
        if m.any() and r.field in df.columns:
            df.loc[m, r.field] = r.value
            df.loc[m, "set_by"] = f"{r.by}: {r.reason}"
    return df


# ------------------------------------------------------------------ tables
def line_designation(case, lines, valves, G=None):
    rows = []
    for r in lines.itertuples():
        spec = r.line_no.split("-")[-2] if r.line_no.count("-") >= 4 else ""
        ins = r.line_no.split("-")[-1] if r.line_no.count("-") >= 4 else "N"
        pc = PIPE_CLASSES.get(spec, {})
        t = None if pd.isna(r.temp_C) else float(r.temp_C)
        dt, dp = design_conditions(t, r.press_bar, r.service)
        vl = valves[valves.line == r.line_no]
        dv = []
        if r.phase != "solid":
            if r.phase == "liquid" or r.service in ("SG", "PG", "EG", "CD") and (t is None or t < 120):
                dv.append('LP drain 3/4" + valve')
            if r.phase == "liquid":
                dv.append('HP vent 3/4" + valve')
            if r.service in ("SG", "PG") and t is not None and t > 120:
                dv.append('N2 purge 1" + check valve')
        rows.append(dict(
            line_no=r.line_no, size=r.size, nb=p.to_nb(r.size), service=r.service, service_name=SERVICE_NAMES.get(r.service, r.service),
            phase=r.phase, **{"from": getattr(r, "_5", None) if False else lines.loc[r.Index, "from"]}, to=r.to,
            pipe_class=spec or "-", moc=pc.get("moc", "per equipment (solids)" if r.phase == "solid" else "-"),
            material_spec=pc.get("pipe", "-"), rating=pc.get("rating", "-"),
            insulation=ins if r.phase != "solid" else "-", insulation_purpose=INSULATION_CODES.get(ins, "-") if r.phase != "solid" else "-",
            insulation_mm=ins_thickness(t) if ins in ("H", "P") else 0, insulation_material="MW" if ins in ("H", "P") else "-",
            op_temp_C=t, op_press_barg=barg(r.press_bar), design_temp_C=dt, design_press_barg=dp,
            test_press_barg=round(1.5 * dp, 1) if dp and r.phase != "solid" else None,
            end_connection=end_connection(r.size, r.service, spec) if r.phase != "solid" else "Flanged chute",
            valves_on_line=", ".join(f"{v.tag} ({v.type})" for v in vl.itertuples()) or "-", valve_tags=", ".join(vl.tag) or "-",
            drains_vents="; ".join(dv) or "-", flow_kgh=r.flow_kgh, set_by="rule"))
    return _apply(pd.DataFrame(rows), case, "line_no")


FAIL_RULES = [   # (valve prefix, service set or None, fail position, reason)
    ("XV", {"OXC", "OX"}, "FC", "Cuts oxidant to the gasifier on trip or air / power failure"),
    ("XV", {"SG", "PG", "NCG"}, "FC", "Isolates flammable / toxic gas on trip"),
    ("XV", None, "FC", "Shutdown valve: closes to the safe state"),
    ("FV", {"OXC", "OX"}, "FC", "No oxidant flow on failure (prevents temperature run-away)"),
    ("FV", None, "FC", "Flow stops on failure"),
    ("LV", {"CD", "WW"}, "FC", "Drain closes so gas cannot blow through to the drain; high level protected by LSHH"),
    ("LV", None, "FC", "Level valve closes on failure"),
    ("TV", {"CWS", "CWR"}, "FO", "Full cooling water flow on failure (safe side for the cooler)"),
    ("TV", None, "FO", "Opens on failure"),
    ("PCV", None, "-", "Self-acting regulator (no air supply); fails per spring setting"),
    ("SG", None, "FC", "Slide gate closes on loss of air (spring return)"),
    ("RV", None, "STOP", "Rotary valve stops with its motor; gives a gas seal when stopped"),
    ("HV", None, "-", "Manual valve"), ("CK", None, "-", "Check valve (self-acting)"), ("FA", None, "-", "Passive element"),
]


def valve_schedule(case, valves, lines, G=None):
    svc = dict(zip(lines.line_no, lines.service))
    ld = line_designation(case, lines, valves)
    cls = dict(zip(ld.line_no, ld.pipe_class))
    moc = dict(zip(ld.line_no, ld.moc))
    endc = dict(zip(ld.line_no, ld.end_connection))
    fo = (G.graph.get("fail_override", {}) if G is not None else {})
    rows = []
    for v in valves.itertuples():
        pre = re.match(r"[A-Z]+", v.tag).group()
        s = svc.get(v.line, "")
        pos, why = next(((fp, rs) for pr, ss, fp, rs in FAIL_RULES if pr == pre and (ss is None or s in ss)), ("-", ""))
        src = "rule"
        if v.tag in fo:
            pos, why, src = fo[v.tag][0], f"set by correction {fo[v.tag][1]}", f"correction {fo[v.tag][1]}"
        act = {"XV": "Pneumatic on/off, spring return, solenoid, limit switches ZSO/ZSC", "FV": "Pneumatic diaphragm, positioner", "TV": "Pneumatic diaphragm, positioner",
               "LV": "Pneumatic diaphragm, positioner", "SG": "Pneumatic cylinder, spring return", "RV": "Electric motor + gearbox", "PCV": "Self-acting (spring + diaphragm)"}.get(pre, "Manual / none")
        rows.append(dict(tag=v.tag, type=v.type, line=v.line, size=v.size, nb=p.to_nb(v.size), service=s, fail_position=pos, reason=why, actuator=act,
                         body_moc=moc.get(v.line, "-"), pipe_class=cls.get(v.line, "-"), end_connection=endc.get(v.line, "-"), set_by=src))
    return _apply(pd.DataFrame(rows), case, "tag")


ZONE_STREAMS = [("POX", "OXI-PD"), ("high temperature trip", "OXI-PD"), ("char bed", "S1"), ("mixer", "R-PD")]   # gasifier zones -> Aspen stream


def _zone_temp(case, description):
    f = DATA / f"{workflow.stem_of(case)}_stream_results.csv"
    if not f.exists():
        return None
    res = pd.read_csv(f, dtype={"stream": str}).set_index("stream")
    for word, stream in ZONE_STREAMS:
        if word.lower() in str(description).lower() and stream in res.index:
            return float(res.temp_C[stream]), stream
    return None


def _op(case_lines, item, eq_type=None):
    """Operating values (T, P abs, flow) at an instrument location: its line, or the equipment's outlet lines.
    For a heat exchanger temperature controller the controlled side is the heated oxidant / air / water outlet."""
    L = case_lines
    if eq_type == "heat_exchanger":
        cold = L[(L["from"] == item) & L.service.isin(["OXC", "OX", "AIR", "CWR", "N2"])]
        if len(cold):
            r = cold.iloc[0]
            return r.temp_C, r.press_bar, r.flow_kgh
    if item in set(L.line_no):
        r = L[L.line_no == item].iloc[0]
        return r.temp_C, r.press_bar, r.flow_kgh
    out = L[L["from"] == item]
    if not len(out):
        out = L[L.to == item]
    t = out.temp_C.max() if len(out) else None
    return t, (out.press_bar.max() if len(out) else None), (out.flow_kgh.sum() if len(out) else None)


def setpoints(case, inst, lines, eq=None):
    rows = []
    for i in inst.itertuples():
        f = i.function
        etype = dict(zip(eq.tag, eq.type)).get(i.attached_to) if eq is not None else None
        t, pa, flow = _op(lines, i.attached_to, etype if "C" in f[1:] else None)
        zone = _zone_temp(case, i.description) if f[0] == "T" else None
        if zone:
            t = zone[0]
        var = "D" if f.startswith("PD") else f[0]
        row = dict(tag=i.tag, function=f, description=i.description, location=i.attached_to, unit="", normal=None, set_point="", alarm_low="", alarm_high="",
                   trip_low="", trip_high="", range="", basis="", set_by="rule")
        rnd = lambda x: None if x is None or pd.isna(x) else round(float(x), 1)
        if var == "T":
            row.update(unit="C", normal=rnd(t))
            if t is not None and not pd.isna(t):
                d = max(25, 0.05 * t)
                row.update(range=f"0-{int(math.ceil(t * 1.5 / 50) * 50)}", alarm_high=rnd(t + d),
                           basis=f"simulation temperature (Aspen stream {zone[1]})" if zone else "simulation temperature at this point")
                if "C" in f[1:]:
                    row.update(set_point=rnd(t), alarm_low=rnd(t - d))
                if f.endswith("HH"):
                    row.update(trip_high=rnd(t + max(50, 0.1 * t)), alarm_high="", basis="trip = normal + max(50 C, 10 %)")
            else:
                row.update(basis="no simulation temperature - TBD")
        elif var == "P":
            pg = barg(pa)
            if f == "PSV":
                _, dp = design_conditions(t, pa, "SG")
                row.update(unit="bar g", normal=pg, set_point=dp, basis="set at design pressure (API 520/521 relief study to confirm size and set)")
            else:
                row.update(unit="mbar g" if pg is not None and abs(pg) < 0.5 else "bar g", normal=None if pg is None else round(pg * (1000 if abs(pg) < 0.5 else 1), 0))
                if f.endswith("HH"):
                    row.update(trip_high=100 if pg is not None and abs(pg) < 0.5 else rnd(1.1 * pg), basis="near-atmospheric system: trip at +100 mbar g (confirm in HAZOP)")
                else:
                    row.update(range="-50 to +200 mbar g" if pg is not None and abs(pg) < 0.5 else "", alarm_high=50 if pg is not None and abs(pg) < 0.5 else "",
                               basis="near-atmospheric gasifier")
        elif var == "D":
            row.update(unit="mbar", alarm_high="1.5 x clean dP", basis="clean pressure drop measured at commissioning; alarm at 1.5 x, back-flush / clean at 2 x")
        elif var == "L":
            row.update(unit="%", range="0-100 %")
            if "C" in f[1:]:
                row.update(set_point=50, alarm_low=30, alarm_high=70, basis="level control band")
            if f.endswith("HH"):
                row.update(trip_high=85, basis="trip before liquid can reach the gas outlet")
            elif f.endswith("SH") or f == "LSH":
                row.update(alarm_high=80, basis="high level switch")
            elif f.endswith("SL") or f == "LSL":
                row.update(alarm_low=20, basis="low level switch")
        elif var == "F":
            row.update(unit="kg/h", normal=rnd(flow))
            if flow is not None and not pd.isna(flow):
                row.update(range=f"0-{int(math.ceil(flow * 1.3 / 10) * 10)}", alarm_low=rnd(0.8 * flow), alarm_high=rnd(1.1 * flow), basis="simulation flow; low 80 %, high 110 %")
                if "C" in f[1:]:
                    row.update(set_point=rnd(flow))
        elif var == "S":
            row.update(unit="% rated", set_point=100, alarm_low=20, basis="speed set by feed rate; low speed = possible jam")
        elif var == "A":
            row.update(unit="vol % (dry)", range="O2 0-5 %, CO 0-50 %, H2 0-50 %, CH4 0-10 %", alarm_high="O2 1.0", trip_high="O2 1.5",
                       basis="oxygen in syngas means bed channelling or air ingress; trip closes the oxidant valve")
        elif var == "B":
            row.update(unit="on / off", basis="flame failure trips the gas supply")
        rows.append(row)
    return _apply(pd.DataFrame(rows), case, "tag")


NOZZLE_TYPES = {"vessel": {"gasifier", "knockout_drum", "tank", "condenser", "gas_cooler", "heat_exchanger", "cyclone", "bag_filter", "filter",
                           "electric_heater_vessel", "auger_reactor", "hopper", "bin", "solids_cooler"}}
MANWAY = {"gasifier": '20"', "knockout_drum": '18"', "tank": '20"', "bag_filter": '18"', "hopper": '18"', "bin": '18"'}


def nozzles(case, eq, lines, inst):
    rows = []
    for e in eq.itertuples():
        n = 0

        def add(service, size, purpose, connects=""):
            nonlocal n
            n += 1
            rows.append(dict(equipment=e.tag, nozzle=f"N{n}", purpose=purpose, size=size, nb=p.to_nb(size), rating="150# RF", facing="RF",
                             service=service, connects_to=connects))
        for r in lines[lines.to == e.tag].itertuples():
            add(r.service, r.size if "solids" not in str(r.size) else "per vendor", "Inlet", r.line_no)
        for r in lines[lines["from"] == e.tag].itertuples():
            add(r.service, r.size if "solids" not in str(r.size) else "per vendor", "Outlet", r.line_no)
        if e.type in NOZZLE_TYPES["vessel"]:
            for i in inst[inst.attached_to == e.tag].itertuples():
                if i.function == "PSV":
                    add("relief", '2"', "Relief valve (size by relief study)", i.tag)
                elif i.function[0] == "L" and i.function not in ("LG",):
                    add("level", '2"', "Level instrument", i.tag)
                elif i.function[0] in "TPA" or i.function.startswith("PD"):
                    add("instrument", '1"' if i.function[0] in "PA" or i.function.startswith("PD") else '1" (thermowell)', "Instrument", i.tag)
                elif i.function == "LG":
                    add("level", '3/4"', "Level gauge", i.tag)
            if e.type not in ("hopper", "bin", "cyclone"):
                add("drain", '1"', "Drain (bottom), with valve and blind")
            add("vent", '1"', "Vent (top), with valve and blind")
            if e.type in ("gasifier", "cyclone", "knockout_drum", "gas_cooler", "bag_filter"):
                add("N2", '1"', "Nitrogen purge, with check valve")
            if e.type in MANWAY:
                add("access", MANWAY[e.type], "Manway / inspection")
    return _apply(pd.DataFrame(rows), case, "equipment")


def drains_vents(case, eq, lines):
    rows = []
    for e in eq.itertuples():
        if e.type in NOZZLE_TYPES["vessel"] and e.type not in ("hopper", "bin", "cyclone"):
            rows.append(dict(item=e.tag, kind="Drain", where="bottom of equipment", size='1"', valve="Gate valve + blind flange", end="Flanged",
                             to="closed drain / condensate tank" if e.type in ("knockout_drum", "gas_cooler", "condenser") else "open drain (cold only)"))
        if e.type in NOZZLE_TYPES["vessel"]:
            rows.append(dict(item=e.tag, kind="Vent", where="top of equipment", size='1"', valve="Gate valve + blind flange", end="Flanged",
                             to="safe location / flare header" if e.type in ("gasifier", "knockout_drum", "gas_cooler", "cyclone", "bag_filter") else "atmosphere"))
    for r in lines.itertuples():
        if r.phase == "solid":
            continue
        t = None if pd.isna(r.temp_C) else r.temp_C
        spec = r.line_no.split("-")[-2] if r.line_no.count("-") >= 4 else ""
        endc = "Threaded plug" if r.service in ("CWS", "CWR", "AIR", "N2") else "Socket-weld + blind flange"
        if r.phase == "liquid" or (r.service in ("SG", "PG", "EG", "CD") and (t is None or t < 120)):
            rows.append(dict(item=r.line_no, kind="Low-point drain", where="low points", size='3/4"', valve="Gate valve + " + endc.lower(), end=endc,
                             to="closed drain" if r.service in HAZARDOUS or r.service == "CD" else "open drain"))
        if r.phase == "liquid":
            rows.append(dict(item=r.line_no, kind="High-point vent", where="high points", size='3/4"', valve="Gate valve + " + endc.lower(), end=endc, to="atmosphere"))
    return _apply(pd.DataFrame(rows), case, "item")


def all_tables(case, G, eq, lines, inst, valves):
    return dict(line_designation=line_designation(case, lines, valves, G), valve_schedule=valve_schedule(case, valves, lines, G),
                setpoints=setpoints(case, inst, lines, eq), nozzles=nozzles(case, eq, lines, inst), drains_vents=drains_vents(case, eq, lines),
                pipe_classes=pd.DataFrame([dict(code=k, **v) for k, v in PIPE_CLASSES.items()]),
                abbreviations=pd.DataFrame(list(MOC_ABBREVIATIONS.items()), columns=["abbreviation", "meaning"]))


def to_excel(tables, path):
    with pd.ExcelWriter(path) as xw:
        for k, df in tables.items():
            df.to_excel(xw, sheet_name=k[:31], index=False)


# ------------------------------------------------------------------ AI proposals for open items (needs ANTHROPIC_API_KEY)
def propose_with_ai(case, tables, api_key=None, max_items=25):
    """Ask Claude to propose values for set points / alarms that are still empty. Returns the number of proposals saved
    (status 'proposed'; an engineer approves them in the app)."""
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("No AI key set.")
    import anthropic
    sp = tables["setpoints"]
    todo = sp[(sp.set_point.astype(str) == "") & (sp.alarm_high.astype(str) == "") & (sp.trip_high.astype(str) == "")].head(max_items)
    if not len(todo):
        return 0
    ctx = tables["line_designation"][["line_no", "service_name", "op_temp_C", "op_press_barg", "flow_kgh"]].to_csv(index=False)
    prompt = ("You are a process control engineer. For each instrument below propose set point and alarm / trip limits for a 10 t/day "
              "biomass gasifier (two-stage, downdraft char bed, O2+CO2 oxidant, gas engine). Use the line data. Answer ONLY with a JSON list of "
              '{"tag":..., "field": one of set_point|alarm_low|alarm_high|trip_low|trip_high, "value":..., "reason":...}.\n\nINSTRUMENTS\n'
              + todo[["tag", "function", "description", "location", "unit"]].to_csv(index=False) + "\nLINES\n" + ctx)
    rep = anthropic.Anthropic(api_key=api_key).messages.create(model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"), max_tokens=2000,
                                                               messages=[dict(role="user", content=prompt)])
    txt = "".join(b.text for b in rep.content if getattr(b, "type", "") == "text")
    items = json.loads(txt[txt.index("["): txt.rindex("]") + 1])
    n = 0
    for it in items:
        if it.get("tag") in set(todo.tag) and it.get("field") in ("set_point", "alarm_low", "alarm_high", "trip_low", "trip_high"):
            set_override(case, it["tag"], it["field"], it["value"], "AI proposal: " + str(it.get("reason", "")), "copilot", status="proposed")
            n += 1
    return n
