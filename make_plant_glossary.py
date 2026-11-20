"""
make_plant_glossary.py - glossary of every tag in one generated P&ID (default: the 8.5 TPD gasifier-mode master).
Run after pfd_to_pid.py:   python make_plant_glossary.py --out out_gasifier_full
Writes <out>/plant_glossary.xlsx
"""
import argparse
import re
from pathlib import Path

import pandas as pd
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

HERE = Path(__file__).parent
DATA = HERE / "data"
SYMBOL = {  # equipment type -> symbol file in symbols/
    "hopper": "equipment_hopper_bin.svg", "screw_heated": "equipment_screw_conveyor.svg", "electric_heater_vessel": "equipment_electric_heater.svg",
    "auger_reactor": "equipment_screw_conveyor.svg", "cyclone": "equipment_cyclone.svg", "condenser": "equipment_shell_and_tube_exchanger.svg",
    "tank": "equipment_atmospheric_tank.svg", "bin": "equipment_hopper_bin.svg", "blower": "equipment_roots_blower.svg", "flare": "equipment_flare.svg",
    "cooling_tower": "equipment_cooling_tower.svg", "solids_cooler": "equipment_shell_and_tube_exchanger.svg", "solids_separator": "equipment_filter_strainer.svg",
    "heat_exchanger": "equipment_shell_and_tube_exchanger.svg", "gasifier": "equipment_reactor_packed_bed.svg", "gas_cooler": "equipment_shell_and_tube_exchanger.svg",
    "bag_filter": "equipment_bag_house_filter.svg", "filter": "equipment_filter_strainer.svg", "package": "equipment_vendor_package.svg",
    "knockout_drum": "equipment_vertical_vessel.svg", "air_intake": "equipment_filter_strainer.svg", "stack": ""}
VALVE_SYMBOL = {"HV": "valves_gate_valve.svg", "CK": "valves_check_valve.svg", "PCV": "valves_pressure_regulator_self-acting.svg", "XV": "valves_on_off_valve_piston_actuator.svg",
                "FV": "valves_control_valve_diaphragm_actuator.svg", "LV": "valves_control_valve_diaphragm_actuator.svg", "TV": "valves_control_valve_diaphragm_actuator.svg", "PSV": "valves_pressure_safety_valve.svg",
                "SG": "valves_gate_valve.svg", "RV": "equipment_rotary_airlock_valve.svg", "FA": "valves_flame_arrestor.svg"}
PREFIX = {"R": "Reactor", "E": "Heat exchanger / heater / cooler", "TK": "Tank or bin", "H": "Hopper", "SC": "Screw conveyor", "CY": "Cyclone", "F": "Filter",
          "B": "Blower", "S": "Separator", "FL": "Flare", "CT": "Cooling tower", "PK": "Vendor package", "U": "Utility header", "V": "Vessel / drum", "ST": "Stack"}
FIRST = {"A": "Analysis", "B": "Burner / flame", "F": "Flow", "H": "Hand", "L": "Level", "P": "Pressure", "S": "Speed", "T": "Temperature", "X": "Unclassified (on/off)"}
NEXT = {"A": "Alarm", "C": "Control", "D": "Differential", "E": "Element", "G": "Gauge", "H": "High", "I": "Indicate", "L": "Low", "S": "Switch", "T": "Transmit", "V": "Valve"}


def decode(letters):
    if letters == "PSV":
        return "Pressure + Safety + Valve"
    parts, rest = [FIRST.get(letters[0], letters[0])], letters[1:]
    for i, ch in enumerate(rest):
        if ch == "S" and i == 0 and rest[1:2] in ("H", "L"):
            parts.append("Switch")
        else:
            parts.append(NEXT.get(ch, ch))
    return " + ".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out_gasifier_full")
    ap.add_argument("--title", default="8.5 TPD gasifier mode (354 kg/h biomass)")
    ap.add_argument("--source", default="data/streams_gasifier_full.csv")
    ap.add_argument("--assumed", default="Syngas cooler, bag filter, condensing cooler, demister, roots blower, ash and dust bins, nitrogen and cooling water are assumed (Viking-style train), not on the PFD.")
    a = ap.parse_args()
    out = HERE / a.out
    x = pd.read_excel(out / "PID_deliverables.xlsx", sheet_name=None, dtype={"pfd_stream": str})
    legend = pd.read_csv(DATA / "symbols_legend.csv")
    svc = dict(legend[legend.category == "Service code"][["code", "meaning"]].values)
    svc.update({"CWS": "Cooling water supply", "CWR": "Cooling water return"})
    spec = dict(legend[legend.category == "Pipe spec"][["code", "meaning"]].values)

    eq = x["Equipment"]
    eq_s = pd.DataFrame({"Tag": eq.tag, "Name": eq.name if "name" in eq else eq["name"], "Tag prefix means": [PREFIX.get(t.split("-")[0], "") for t in eq.tag],
                         "Area": eq.area, "Model type": eq.type, "Symbol file": [SYMBOL.get(t, "") for t in eq.type]})
    eq_s["Name"] = eq["name"]
    ln = x["Line list"]

    def explain(no, phase):
        if phase == "solid":
            return "Solids transfer (no pipe size)"
        size, service, seq, sp, ins = (no.rsplit("-", 4) + [""] * 5)[:5]
        return f"size {size}; service {service}; sequence {seq}; spec {sp} ({spec.get(sp, '')}); insulation {'hot' if ins == 'H' else 'none'}"
    ln_s = pd.DataFrame({"Line number": ln.line_no, "PFD stream": ln.pfd_stream, "Service code": ln.service, "Service": [svc.get(s, "") for s in ln.service],
                         "From": ln["from"], "To": ln.to, "Flow kg/h": ln.flow_kgh, "Temp C": ln.temp_C, "Press bar": ln.press_bar, "Size": ln["size"],
                         "How to read the number": [explain(n, ph) for n, ph in zip(ln.line_no, ln.phase)], "Basis": ln.description})
    ins = x["Instrument index"]
    ins_s = pd.DataFrame({"Tag": ins.tag, "Letters": ins.function, "Letters mean": [decode(f) for f in ins.function], "Loop number": [t.split("-")[1] for t in ins.tag],
                          "Purpose": ins.description, "Location": ins.location.map({"FIELD": "Field", "DCS": "Control system (DCS)", "SIS": "Safety system"}),
                          "On": ins.attached_to})
    va = x["Valve list"]
    pre = [re.match(r"[A-Z]+", t).group() for t in va.tag]
    va_s = pd.DataFrame({"Tag": va.tag, "Prefix": pre, "Type": va.type, "On line / equipment": va.line, "Size": va["size"], "Symbol file": [VALVE_SYMBOL.get(q, "") for q in pre]})
    used_first = sorted({f[0] for f in ins.function})
    codes = ([("ISA letter used (first)", k, FIRST.get(k, "")) for k in used_first]
             + [("Service code used", k, svc.get(k, "")) for k in sorted(set(ln.service))]
             + [("Pipe spec used", k, v) for k, v in spec.items()]
             + [("Valve prefix used", k, d) for k, d in sorted(set(zip(pre, va.type)))]
             + [("Equipment prefix used", k, PREFIX.get(k, "")) for k in sorted({t.split("-")[0] for t in eq.tag})])
    codes_s = pd.DataFrame(codes, columns=["Group", "Code", "Meaning"])
    read = pd.DataFrame([("Plant", a.title), ("Source", f"Generated from the stream table {a.source} by pfd_to_pid.py"),
                         ("Status", "Draft. Instruments and valves come from generic rules; not reviewed, no HAZOP."),
                         ("Assumed items", a.assumed),
                         ("Counts", f"{len(eq)} equipment, {len(ln)} lines, {len(ins)} instruments, {len(va)} valves and fittings"),
                         ("Line sizes", "Velocity-based estimates (20 m/s gas, 1.5 m/s liquid). TBD = no flow data.")], columns=["Item", "Detail"])
    hf, bf = Font(name="Arial", bold=True, color="FFFFFF", size=10), Font(name="Arial", size=10)
    fill, thin = PatternFill("solid", fgColor="1F3864"), Side(style="thin", color="BFBFBF")
    with pd.ExcelWriter(out / "plant_glossary.xlsx", engine="openpyxl") as xl:
        for name, df in [("Read me", read), ("Equipment", eq_s), ("Lines", ln_s), ("Instruments", ins_s), ("Valves and fittings", va_s), ("Codes used", codes_s)]:
            df.to_excel(xl, sheet_name=name, index=False)
            ws = xl.sheets[name]
            for c in ws[1]:
                c.font, c.fill, c.alignment = hf, fill, Alignment(wrap_text=True, vertical="center")
            for row in ws.iter_rows(min_row=2):
                for c in row:
                    c.font, c.alignment, c.border = bf, Alignment(wrap_text=True, vertical="top"), Border(left=thin, right=thin, top=thin, bottom=thin)
            for i, col in enumerate(df.columns):
                ws.column_dimensions[chr(65 + i)].width = min(55, max(12, int(df[col].astype(str).str.len().max()) + 2, len(col) + 2))
            ws.freeze_panes = "A2"
    print("wrote", out / "plant_glossary.xlsx", {n: len(d) for n, d in [("eq", eq_s), ("lines", ln_s), ("inst", ins_s), ("valves", va_s), ("codes", codes_s)]})


if __name__ == "__main__":
    main()
