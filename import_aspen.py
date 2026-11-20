"""
import_aspen.py - read an Aspen Plus HMBD workbook and build the P&ID stream table from it.

Reads   data/aspen_10tpd_hmbd.xlsx   (sheets "Stream results" and "Energy Balance-10TPD")
        data/aspen_map_10tpd.csv     (which Aspen stream is which P&ID line: from, to, service, hints)
Writes  data/aspen_10tpd_stream_results.csv   every Aspen stream: T, P, flows, density, enthalpy, composition
        data/aspen_10tpd_energy.csv           the energy balance table as item / value / unit
        data/streams_aspen_10tpd.csv          the stream table used by pfd_to_pid.py
        data/duties_aspen_10tpd.csv           heat duties and power per equipment tag
Run:    python import_aspen.py
"""
import argparse
from pathlib import Path

import openpyxl
import pandas as pd

DATA = Path(__file__).parent / "data"
KW_PER_GCAL_H = 1162.2
ROWS = {"Temperature": "temp_C", "Pressure": "press_bar", "Mass Vapor Fraction": "vapor_frac", "Mass Solid Fraction": "solid_frac",
        "Mass Density": "density_kgm3", "Enthalpy Flow": "enthalpy_Gcal_h", "Mass Flows": "mass_kgh", "Volume Flow": "vol_m3h"}
# energy-balance label -> (equipment tag, short name, sign note)
DUTY_LABELS = [
    ("Dryer + decomposer + pyrolyzer demand", "SC-101 + R-101", "Heat demand of dryer and pyrolyser (supplied from E-501)"),
    ("Gasifier reactors net (OXID + RED", "R-401", "Net heat of gasifier reactors (negative = released)"),
    ("B10 oxidant preheater", "E-401", "Oxidant pre-heater duty (syngas to oxidant)"),
    ("B2 syngas cooler", "E-402", "Syngas cooler duty (negative = removed)"),
    ("B5 water knock-out", "V-401", "Knock-out cooling duty (negative = removed)"),
    ("B16 exhaust heat exchanger", "E-501", "Exhaust heat recovered (negative = removed from exhaust)"),
    ("Net shaft work (turbine - compressor)", "PK-501", "Engine net shaft work"),
    ("Mechanical losses (enthalpy drop - net work)", "PK-501", "Engine mechanical losses"),
    ("Ash cooler heat removed", "TK-403", "Ash cooling"),
]


def read_streams(wb):
    ws = wb["Stream results"]
    rows = list(ws.iter_rows(values_only=True))
    names = [str(n).rstrip(".") for n in rows[0][2:] if n is not None]
    n = len(names)
    end = next(i for i, r in enumerate(rows) if r[0] and str(r[0]).startswith("MIXED Substream"))   # use the Total Stream block only
    out = {"stream": names, "from_block": [x or "" for x in rows[2][2:2 + n]], "to_block": [x or "" for x in rows[3][2:2 + n]]}
    section, comps = None, []
    for r in rows[4:end]:
        label = r[0]
        if label is None:
            continue
        vals = list(r[2:2 + n])
        if label in ROWS and ROWS[label] not in out:
            out[ROWS[label]] = vals
            section = "mass" if label == "Mass Flows" else None
        elif label == "Mass Fractions":
            section = None
        elif section == "mass" and r[1] == "kg/hr":
            out["kgh_" + label] = vals
            comps.append(label)
    df = pd.DataFrame(out)
    df["enthalpy_kW"] = (df.enthalpy_Gcal_h * KW_PER_GCAL_H).round(1)

    def comp(row):
        parts = [(c, row["kgh_" + c] / row.mass_kgh * 100) for c in comps if row.mass_kgh and row["kgh_" + c]]
        return ", ".join(f"{c} {p:.1f}%" for c, p in sorted(parts, key=lambda x: -x[1]) if p >= 0.5)
    df["composition_mass"] = df.apply(comp, axis=1)
    return df


def read_energy(wb):
    ws = next(s for s in wb.worksheets if s.title.startswith("Energy Balance"))
    rows = []
    for r in ws.iter_rows(values_only=True):
        if r[0] is not None and str(r[0]).startswith("9."):      # the 8.5 TPD comparison block is saved separately
            break
        if r[0] is not None and isinstance(r[1], (int, float)):
            rows.append((r[0], r[1], r[2], r[3]))
    return pd.DataFrame(rows, columns=["item", "value", "unit", "note"]).drop_duplicates("item")


def read_reference(wb):
    """The '9. COMPARISON WITH 8.5 TPD case' block: item, reference value, this case, unit."""
    ws = next(x for x in wb.worksheets if x.title.startswith("Energy Balance"))
    rows, on = [], False
    for r in ws.iter_rows(values_only=True):
        if r[0] and str(r[0]).startswith("9."):
            on = True
        elif on and r[0] and isinstance(r[1], (int, float)) and isinstance(r[2], (int, float)):
            rows.append((r[0], r[1], r[2]))
        elif on and r[0] and str(r[0]).startswith("Notes"):
            break
    return pd.DataFrame(rows, columns=["item", "reference_8p5tpd", "this_case"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", default=str(DATA / "aspen_10tpd_hmbd.xlsx"))
    ap.add_argument("--case", default="aspen_10tpd")
    a = ap.parse_args()
    wb = openpyxl.load_workbook(a.xlsx, data_only=True)
    sr = read_streams(wb)
    sr.to_csv(DATA / f"{a.case}_stream_results.csv", index=False)
    en = read_energy(wb)
    en.to_csv(DATA / f"{a.case}_energy.csv", index=False)
    read_reference(wb).to_csv(DATA / f"{a.case}_reference.csv", index=False)

    S = sr.set_index("stream")
    rows = []
    for m in pd.read_csv(DATA / f"aspen_map_{a.case.split('_')[-1]}.csv", dtype=str).fillna("").itertuples():
        src = [s for s in m.aspen_streams.split("+") if s]
        r = dict(no=m.no, **{"from": m._3}, to=m.to, service=m.service, phase=m.phase, flow_kgh=None, temp_C=None, press_bar=None,
                 pid_hints=m.pid_hints, description=m.description, vol_m3h=None, density_kgm3=None, enthalpy_kW=None, composition="")
        if src:
            x = S.loc[src]
            r.update(flow_kgh=round(x.mass_kgh.sum(), 2), temp_C=round(float(x.temp_C.iloc[0]), 1), press_bar=float(x.press_bar.iloc[0]),
                     vol_m3h=round(x.vol_m3h.sum(), 3), enthalpy_kW=round(x.enthalpy_kW.sum(), 1),
                     density_kgm3=round(x.mass_kgh.sum() / x.vol_m3h.sum(), 4), composition=" | ".join(x.composition_mass))
        rows.append(r)
    st = pd.DataFrame(rows)
    st.to_csv(DATA / f"streams_{a.case}.csv", index=False)

    duties = []
    for key, tag, name in DUTY_LABELS:
        hit = en[en.item.str.startswith(key)]
        if len(hit):
            duties.append(dict(tag=tag, item=name, kW=round(float(hit.value.iloc[0]), 2)))
    pd.DataFrame(duties).to_csv(DATA / f"duties_{a.case}.csv", index=False)

    # mass balance per equipment, from the mapped table
    print(f"{len(sr)} Aspen streams, {len(st)} P&ID lines, {len(duties)} duties")
    bal = []
    for tag in sorted(set(st["from"]) | set(st.to)):
        i, o = st[st.to == tag].flow_kgh.sum(min_count=1), st[st["from"] == tag].flow_kgh.sum(min_count=1)
        if pd.notna(i) and pd.notna(o):
            bal.append((tag, round(i, 1), round(o, 1), round(o - i, 2)))
    print(pd.DataFrame(bal, columns=["equipment", "in_kgh", "out_kgh", "out_minus_in"]).to_string(index=False))


if __name__ == "__main__":
    main()
