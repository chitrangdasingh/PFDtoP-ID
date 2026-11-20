"""
twin.py - make the smart P&ID ready as the input of a digital twin.

twin_model()   one JSON document: asset hierarchy (plant / area / unit / equipment, ISA-95 style), every line with its
               engineering data, every signal point (instrument tag -> process value with unit, range, Aspen baseline,
               set point, alarm and trip limits, OPC UA-style node id), valve states with fail positions, relations from
               the knowledge graph, and the Aspen baseline per stream. A historian / SCADA / twin platform maps its tags to
               the "node_id" fields.
signal_list()  the same signals as a flat table (CSV) for PLC / DCS / historian configuration (I/O type, unit, range, limits).
evaluate()     live rules: compares live (or simulated) values with the Aspen baseline and returns alerts and the
               gap-bridging question to ask the operator. Values can come from sliders in the app today and from an
               OPC UA / MQTT / historian feed later (read_live() is the place to plug that in).
"""
import datetime
import json
import re

import pandas as pd

import engineering as ENGR

IO_TYPE = {"T": "AI", "P": "AI", "F": "AI", "L": "AI", "A": "AI", "S": "AI", "D": "AI", "B": "DI"}
AREAS = {"1": "Feed and pyrolysis", "2": "Pyrolysis products", "3": "Utilities", "4": "Gasification and gas cleaning", "5": "Power generation"}


def _io(fn):
    if fn == "PSV":
        return "-"
    if fn.endswith(("SHH", "SLL", "SH", "SL")) or fn.startswith("LS") or fn.startswith("B"):
        return "DI"
    if "C" in fn[1:]:
        return "AI + AO (loop)"
    return IO_TYPE.get("D" if fn.startswith("PD") else fn[0], "AI")


def signal_list(case, G, eq, lines, inst, valves, tables=None):
    T = tables or ENGR.all_tables(case, G, eq, lines, inst, valves)
    sp = T["setpoints"]
    rows = []
    for r in sp.itertuples():
        area = re.search(r"-(\d)", r.tag)
        a = area.group(1) if area else "0"
        rows.append(dict(tag=r.tag, io=_io(r.function), description=r.description, asset=r.location, unit=r.unit, range=r.range,
                         aspen_baseline=r.normal, set_point=r.set_point, alarm_low=r.alarm_low, alarm_high=r.alarm_high, trip_low=r.trip_low, trip_high=r.trip_high,
                         node_id=f"ns=2;s=Plant/Area{a}00/{r.location}/{r.tag}/PV".replace('"', "in"), historian_tag=f"{r.tag}.PV"))
    for v in T["valve_schedule"].itertuples():
        if v.fail_position in ("FC", "FO", "FL", "STOP"):
            area = re.search(r"-(\d)", v.tag)
            a = area.group(1) if area else "0"
            io = "DO + DI (ZSO/ZSC)" if v.tag.startswith(("XV", "SG")) else ("DO (motor)" if v.tag.startswith("RV") else "AO + AI (position)")
            rows.append(dict(tag=v.tag, io=io, description=f"{v.type}, fails {v.fail_position}", asset=v.line, unit="% open" if io.startswith("AO") else "open / closed",
                             range="0-100" if io.startswith("AO") else "", aspen_baseline=None, set_point="", alarm_low="", alarm_high="", trip_low="", trip_high="",
                             node_id=f"ns=2;s=Plant/Area{a}00/{v.line}/{v.tag}/POS".replace('"', "in"), historian_tag=f"{v.tag}.POS"))
    return pd.DataFrame(rows)


def twin_model(case, model, G, eq, lines, inst, valves, results=None):
    T = ENGR.all_tables(case, G, eq, lines, inst, valves)
    sig = signal_list(case, G, eq, lines, inst, valves, T)
    hier = {}
    for e in eq.itertuples():
        a = str(int(e.area))[0]
        hier.setdefault(f"Area{a}00", dict(name=AREAS.get(a, f"Area {a}00"), equipment=[]))["equipment"].append(
            dict(tag=e.tag, name=e.name, type=e.type, nozzles=T["nozzles"][T["nozzles"].equipment == e.tag].drop(columns="equipment").to_dict("records"),
                 signals=list(sig[sig.asset == e.tag].tag)))
    clean = lambda df: json.loads(df.to_json(orient="records"))
    doc = dict(
        schema="pfd2pid-digital-twin/1.0", generated=datetime.datetime.now().isoformat(timespec="seconds"), case=case,
        revision=model.get("revision"), status="draft for review - values are proposals until HAZOP / SIL",
        plant=dict(name="Two-stage biomass gasification plant", capacity="10 t/day biomass", hierarchy=hier),
        lines=clean(T["line_designation"]), valves=clean(T["valve_schedule"]), signals=clean(sig),
        relations=model.get("relations", []),
        aspen_baseline=clean(results) if results is not None else [],
        rules=[dict(id=r[0], when=r[1], ask=r[3]) for r in RULES],
    )
    return doc, sig


# live rules: (id, condition text, test(values) -> bool, question to ask, action)
RULES = [
    ("GAP-01", "E-501 heat recovered > 165 kW and pyrolyser TIC-1007 < 460 C", lambda v: v.get("E501_kW", 151) > 165 and v.get("TIC-1007", 500) < 460,
     "Heat recovery is above the Aspen 151 kW while the pyrolyser runs cold. Has the feed moisture gone above 20 %, or is E-501 fouled?",
     "Check feed moisture; compare E-501 duty with the baseline"),
    ("GAP-02", "Char bed PDI-4004 > 35 mbar and POX TI-4002 > 1130 C", lambda v: v.get("PDI-4004", 20) > 35 and v.get("TI-4002", 1100) > 1130,
     "Bed pressure drop and POX temperature are both high. Increase CO2 share in the oxidant (e.g. 40:60) to avoid clinker, or inspect the grate?",
     "Risk of ash sintering / clinker"),
    ("GAP-03", "Cooler outlet TI-4032 > 55 C", lambda v: v.get("TI-4032", 50) > 55,
     "Syngas leaves E-402 above 55 C. Is cooling water flow normal, and is there tar in the V-401 drain?", "Fouling or loss of cooling"),
    ("SAFE-01", "Syngas O2 AI-4038 > 1.0 vol %", lambda v: v.get("AI-4038", 0.1) > 1.0,
     "Oxygen in the syngas is rising: is the bed channelling or has the feed bridged? At 1.5 % XV-4022 and XV-4036 trip.", "Prepare for trip, check feed"),
    ("SAFE-02", "POX TI-4002 > 1210 C", lambda v: v.get("TI-4002", 1100) > 1210,
     "POX zone above trip limit: TSHH-4006 closes XV-4022. Was FV-4021 flow above set point, or did the feed stop?", "Trip: oxidant isolated"),
    ("PDM-01", "Engine power < 365 kW at normal syngas flow", lambda v: v.get("engine_kW", 386) < 365,
     "Engine power is below the 386 kW baseline. When was the gas analyser AI-4038 calibrated, and is V-401 carrying liquid over?", "Check analyser and knock-out"),
    ("PDM-02", "Condensate flow < 45 kg/h", lambda v: v.get("WAT_kgh", 58) < 45,
     "Less condensate than the Aspen 58 kg/h: is water passing to the engine (V-401 level), or is the cooler under-cooling?", "Risk of liquid carry-over"),
]

LIVE_POINTS = [("TI-4002", "POX zone temperature", "C", 900, 1350, 1100), ("PDI-4004", "Char bed pressure drop", "mbar", 0, 80, 20),
               ("TI-4032", "Cooler outlet temperature", "C", 30, 120, 50), ("AI-4038", "O2 in syngas", "vol %", 0.0, 3.0, 0.1),
               ("TIC-1007", "Pyrolyser temperature", "C", 350, 600, 500), ("E501_kW", "Exhaust heat recovered", "kW", 50, 250, 151),
               ("engine_kW", "Engine shaft power", "kW", 0, 450, 386), ("WAT_kgh", "Condensate flow", "kg/h", 0, 120, 58)]


def read_live():
    """Placeholder for a live feed (OPC UA, MQTT, historian REST). Return a dict tag -> value, or None when not connected."""
    return None


def evaluate(values):
    return [dict(rule=r[0], condition=r[1], question=r[3], action=r[4]) for r in RULES if r[2](values)]
