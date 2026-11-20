"""
bom.py - bill of material creation from the generated P&ID.

Quantities and tags always come from the current P&ID; what people add (pipe lengths, unit costs, vendor, notes) is
kept in data/bom_<stem>.csv and data/pipe_lengths_<stem>.csv, so it survives regeneration.
Piping is counted in metres once lengths are entered per line; everything else by number of items.
The Excel export keeps formulas (total = quantity x unit cost), so it recalculates when edited.
"""
from pathlib import Path

import pandas as pd

import pfd_to_pid as p
import workflow

DATA = Path(__file__).parent / "data"
EDIT_COLS = ["unit_cost", "currency", "vendor", "notes"]
KEY = ["category", "item", "size_spec"]


def _spec_names():
    leg = pd.read_csv(DATA / "symbols_legend.csv")
    return dict(leg[leg.category == "Pipe spec"][["code", "meaning"]].values)


def pipe_lengths(case, lines):
    f = DATA / f"pipe_lengths_{workflow.stem_of(case)}.csv"
    old = pd.read_csv(f, dtype={"line_no": str}) if f.exists() else pd.DataFrame(columns=["line_no", "length_m"])
    base = lines[lines.phase != "solid"][["line_no", "pfd_stream", "size", "service"]].copy()
    base = base.merge(old[["line_no", "length_m"]], on="line_no", how="left")
    base["length_m"] = pd.to_numeric(base["length_m"], errors="coerce").fillna(0.0)
    return base


def save_pipe_lengths(case, df, by=""):
    df[["line_no", "length_m"]].to_csv(DATA / f"pipe_lengths_{workflow.stem_of(case)}.csv", index=False)
    workflow.log(case, by, "pipe lengths edited", f"{int((df.length_m > 0).sum())} lines with length")


def create(case, G, eq, lines, inst, valves, zones=None):
    """BOM rows: category, item, size_spec, material, qty, unit, tags, zones + the editable columns."""
    zones = zones or {}
    specs = _spec_names()
    spec_of_line = {r.line_no: (r.line_no.split("-")[-2] if r.line_no.count("-") >= 4 else "") for r in lines.itertuples()}
    rows = []
    for e in eq.itertuples():
        rows.append(dict(category="Equipment", item=e.name, size_spec=e.type, material="per equipment datasheet", qty=1, unit="no.", tags=e.tag,
                         zones=zones.get(e.tag, "")))
    motors = pd.DataFrame(G.graph.get("motors", []))
    if len(motors):
        for d, g in motors.groupby("description"):
            rows.append(dict(category="Motor", item=d, size_spec="kW to be defined", material="-", qty=len(g), unit="no.", tags=", ".join(g.tag),
                             zones=", ".join(sorted({zones.get(t, "") for t in g.tag} - {""}))))
    if len(valves):
        v = valves.copy()
        v["spec"] = v.line.map(spec_of_line).fillna("")
        for (t, sz, sp), g in v.groupby(["type", "size", "spec"], dropna=False):
            nb = p.to_nb(sz)
            rows.append(dict(category="Valve / fitting", item=t, size_spec=f"{sz} ({nb})" if nb else str(sz), material=f"to pipe class {sp}: {specs.get(sp, '-')}" if sp else "-",
                             qty=len(g), unit="no.", tags=", ".join(g.tag), zones=", ".join(sorted({zones.get(t_, "") for t_ in g.tag} - {""}))))
    if len(inst):
        for f, g in inst[inst.function != "PSV"].groupby("function"):
            where = g.location.map({"FIELD": "field", "DCS": "DCS", "SIS": "safety system"}).iloc[0]
            rows.append(dict(category="Instrument", item=f"{f} ({where})", size_spec="range to be defined", material="-", qty=len(g), unit="no.",
                             tags=", ".join(g.tag), zones=", ".join(sorted({zones.get(t, "") for t in g.tag} - {""}))))
    pl = pipe_lengths(case, lines)
    pl["spec"] = pl.line_no.map(spec_of_line)
    for (sz, sp), g in pl.groupby(["size", "spec"]):
        nb = p.to_nb(sz)
        length = float(g.length_m.sum())
        rows.append(dict(category="Piping", item=f"Pipe {sz}" + (f" ({nb})" if nb else ""), size_spec=f"{sz}, class {sp}", material=specs.get(sp, "-"),
                         qty=round(length, 1) if length else len(g), unit="m" if length else "lines (enter lengths)", tags=", ".join(g.line_no),
                         zones=", ".join(sorted({zones.get(t, "") for t in g.line_no} - {""}))))
    for svc, g in lines[lines.phase == "solid"].groupby("service"):
        rows.append(dict(category="Solids handling", item=f"Chute / conveyor, service {svc}", size_spec="-", material="-", qty=len(g), unit="no.",
                         tags=", ".join(g.line_no), zones=", ".join(sorted({zones.get(t, "") for t in g.line_no} - {""}))))
    bom = pd.DataFrame(rows)
    f = DATA / f"bom_{workflow.stem_of(case)}.csv"
    if f.exists():                                   # people's additions, matched on category / item / size
        old = pd.read_csv(f, dtype=str).fillna("")
        bom = bom.merge(old[KEY + EDIT_COLS], on=KEY, how="left")
    for c in EDIT_COLS:
        if c not in bom:
            bom[c] = ""
    bom["unit_cost"] = pd.to_numeric(bom["unit_cost"], errors="coerce")
    bom["currency"] = bom["currency"].replace("", None).fillna("INR")
    bom["total"] = (bom["qty"].where(bom["unit"].isin(["no.", "m"]), 0) * bom["unit_cost"]).round(2)
    bom.insert(0, "pos", range(1, len(bom) + 1))
    return bom


def save(case, bom, by=""):
    bom[KEY + EDIT_COLS].to_csv(DATA / f"bom_{workflow.stem_of(case)}.csv", index=False)
    workflow.log(case, by, "BOM edited", f"{int(pd.to_numeric(bom.unit_cost, errors='coerce').notna().sum())} items priced")


def to_excel(bom, path, title):
    """Excel BOM with live formulas for line totals and the grand total."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "BOM"
    cols = ["pos", "category", "item", "size_spec", "material", "qty", "unit", "unit_cost", "currency", "total", "tags", "zones", "vendor", "notes"]
    ws.append([title])
    ws["A1"].font = Font(name="Arial", bold=True, size=12)
    ws.append(["Quantities from the generated P&ID (draft). Enter unit costs in column H (blue); totals are formulas."])
    ws.append([c.replace("_", " ").upper() for c in cols])
    for c in ws[3]:
        c.font = Font(name="Arial", bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F3864")
    for i, r in enumerate(bom.to_dict("records")):
        row = 4 + i
        vals = [r.get(c, "") for c in cols]
        vals[cols.index("unit_cost")] = None if pd.isna(r.get("unit_cost")) else r.get("unit_cost")
        vals[cols.index("total")] = f'=IF(OR(G{row}="no.",G{row}="m"),F{row}*H{row},0)'
        ws.append(vals)
        ws.cell(row=row, column=8).font = Font(name="Arial", color="0000FF")
    last = 3 + len(bom)
    ws.append([])
    ws.append(["", "", "", "", "", "", "", "", "TOTAL", f"=SUM(J4:J{last})"])
    ws.cell(row=last + 2, column=9).font = Font(name="Arial", bold=True)
    ws.cell(row=last + 2, column=10).font = Font(name="Arial", bold=True)
    widths = [5, 16, 40, 22, 40, 8, 18, 12, 9, 14, 40, 14, 18, 24]
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(65 + i)].width = w
    for row in ws.iter_rows(min_row=4):
        for c in row:
            if c.font.color is None or c.font.color.rgb in (None, "FF000000"):
                c.font = Font(name="Arial", color=c.font.color.rgb if c.font.color else None)
            c.alignment = Alignment(vertical="top", wrap_text=True)
    ws.freeze_panes = "A4"
    wb.save(path)
