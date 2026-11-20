"""
aspen_link.py - bring Aspen Plus results into the P&ID tool.

Three ways in, all writing the same file data/<case>_stream_results.csv (one row per Aspen stream):
  1. HMBD workbook (.xlsx)       import_aspen.py reads the "Stream results" and energy sheets (works anywhere).
  2. Stream summary CSV          exported from Aspen (Stream Summary -> Send to Excel -> save as CSV); read_summary_csv().
  3. Live link to Aspen Plus     live_streams(): opens the .bkp / .apw through the Aspen Plus COM interface, optionally
                                 runs it, and reads every stream. Needs Windows, a licensed Aspen Plus and pywin32
                                 (pip install pywin32). It cannot run on Streamlit Cloud; run it on the engineer's PC:
                                     python aspen_link.py --bkp "C:\\models\\10TPD.bkp" --case aspen_10tpd --run
                                 then commit the CSV, or point the app at a shared folder.
After any of these, run import_aspen.py (or the app's "Re-build P&ID from results" button) to refresh the stream table.

Also: watch_folder() lists HMBD workbooks / CSVs dropped in data/aspen_inbox/ so the app can import the newest one.
"""
import argparse
import datetime
from pathlib import Path

import pandas as pd

DATA = Path(__file__).parent / "data"
INBOX = DATA / "aspen_inbox"
KW_PER_GCAL_H = 1162.2
COLUMNS = ["stream", "from_block", "to_block", "temp_C", "press_bar", "vapor_frac", "solid_frac", "density_kgm3", "enthalpy_Gcal_h", "mass_kgh", "vol_m3h"]

# Aspen Plus variable tree paths (stream output). MIXED substream; CISOLID / NC flows are read from their own substreams.
PATHS = {"temp_C": r"\Data\Streams\{s}\Output\TEMP_OUT\MIXED", "press_bar": r"\Data\Streams\{s}\Output\PRES_OUT\MIXED",
         "mass_kgh": r"\Data\Streams\{s}\Output\MASSFLMX\MIXED", "vol_m3h": r"\Data\Streams\{s}\Output\VOLFLMX\MIXED",
         "enthalpy_Gcal_h": r"\Data\Streams\{s}\Output\HMX_FLOW\MIXED", "vapor_frac": r"\Data\Streams\{s}\Output\VFRAC_OUT\MIXED",
         "density_kgm3": r"\Data\Streams\{s}\Output\RHOMX_MASS\MIXED"}
COMP_PATH = r"\Data\Streams\{s}\Output\MASSFLOW\{sub}\{c}"
PORTS = r"\Data\Streams\{s}\Ports"


def live_streams(bkp, run=False, visible=False, substreams=("MIXED", "CISOLID", "NC")):
    """Read every stream from an Aspen Plus file through COM. Units are those set in the Aspen file: set the report
    unit set to METCBAR (C, bar, kg/hr, cum/hr, Gcal/hr) so the numbers match the columns of the CSV."""
    import win32com.client as win32          # Windows + pywin32 only
    aspen = win32.Dispatch("Apwn.Document")
    aspen.InitFromArchive2(str(Path(bkp).resolve()))
    aspen.Visible = visible
    if run:
        aspen.Engine.Run2()
    tree = aspen.Tree
    comps = [e.Name for e in tree.FindNode(r"\Data\Components\Specifications\Input\TYPE").Elements]
    blocks = {}
    for b in tree.FindNode(r"\Data\Blocks").Elements:          # stream -> (from block, to block)
        try:
            for port in tree.FindNode(rf"\Data\Blocks\{b.Name}\Ports").Elements:
                for st in port.Elements:
                    blocks.setdefault(st.Name, ["", ""])
                    blocks[st.Name][0 if "OUT" in port.Name.upper() or port.Name.upper().startswith("P(OUT") else 1] = b.Name
        except Exception:
            pass
    rows = []
    for s in tree.FindNode(r"\Data\Streams").Elements:
        name = s.Name
        row = dict(stream=name, from_block=blocks.get(name, ["", ""])[0], to_block=blocks.get(name, ["", ""])[1])
        for col, path in PATHS.items():
            node = tree.FindNode(path.format(s=name))
            row[col] = None if node is None else node.Value
        for sub in substreams:
            for c in comps:
                node = tree.FindNode(COMP_PATH.format(s=name, sub=sub, c=c))
                if node is not None and node.Value:
                    row[f"kgh_{c}"] = row.get(f"kgh_{c}", 0) + float(node.Value)
        rows.append(row)
    aspen.Close()
    df = pd.DataFrame(rows)
    if "enthalpy_Gcal_h" in df:
        df["enthalpy_kW"] = (df.enthalpy_Gcal_h.astype(float) * KW_PER_GCAL_H).round(1)
    return df


def read_summary_csv(path):
    """Aspen 'Stream Summary' exported as CSV: first column = property, then one column per stream."""
    try:
        raw = pd.read_csv(path, header=None, dtype=str, encoding="utf-8")
    except UnicodeDecodeError:                      # Excel on Windows saves CSV as cp1252 (degree sign)
        raw = pd.read_csv(path, header=None, dtype=str, encoding="cp1252")
    hdr = raw.index[raw.iloc[:, 0].astype(str).str.strip().str.lower().isin(["stream name", "streams", "stream"]) |
                    raw.iloc[:, 1].astype(str).str.strip().str.lower().eq("units")]
    h = hdr[0] if len(hdr) else 0
    names = [str(x).strip() for x in raw.iloc[h, 2:] if str(x).strip() not in ("", "nan")]
    want = {"From": "from_block", "To": "to_block", "Temperature": "temp_C", "Pressure": "press_bar", "Mass Flows": "mass_kgh",
            "Volume Flow": "vol_m3h", "Enthalpy Flow": "enthalpy_Gcal_h", "Mass Density": "density_kgm3", "Mass Vapor Fraction": "vapor_frac"}
    out = {n: dict(stream=n) for n in names}
    in_mass = False
    for i in range(h + 1, len(raw)):
        key = str(raw.iloc[i, 0]).strip() if str(raw.iloc[i, 0]).strip() not in ("", "nan") else str(raw.iloc[i, 1]).strip()
        if key in want:
            in_mass = key == "Mass Flows"
            for j, n in enumerate(names):
                out[n][want[key]] = raw.iloc[i, 2 + j]
        elif in_mass and key and key not in ("nan",):
            for j, n in enumerate(names):
                out[n][f"kgh_{key}"] = raw.iloc[i, 2 + j]
        elif key in ("Mole Flows", "Mass Fractions", "Mole Fractions"):
            in_mass = False
    df = pd.DataFrame(out.values())
    for c in df.columns:
        if c not in ("stream", "from_block", "to_block"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "enthalpy_Gcal_h" in df:
        df["enthalpy_kW"] = (df.enthalpy_Gcal_h * KW_PER_GCAL_H).round(1)
    return df


def compare(old, new, tol_pct=1.0):
    """Streams whose temperature, pressure or mass flow changed by more than tol_pct between two result sets."""
    o, n = old.set_index("stream"), new.set_index("stream")
    rows = []
    for s in sorted(set(o.index) | set(n.index)):
        if s not in o.index or s not in n.index:
            rows.append(dict(stream=s, change="added" if s in n.index else "removed"))
            continue
        for c in ("temp_C", "press_bar", "mass_kgh", "vol_m3h"):
            a, b = o.at[s, c] if c in o else None, n.at[s, c] if c in n else None
            try:
                a, b = float(a), float(b)
            except (TypeError, ValueError):
                continue
            if abs(b - a) > max(abs(a), 1e-9) * tol_pct / 100:
                rows.append(dict(stream=s, change=c, old=round(a, 3), new=round(b, 3), pct=round(100 * (b - a) / a, 2) if a else None))
    return pd.DataFrame(rows)


def watch_folder():
    INBOX.mkdir(exist_ok=True)
    files = sorted([f for f in INBOX.iterdir() if f.suffix.lower() in (".xlsx", ".csv")], key=lambda f: f.stat().st_mtime, reverse=True)
    return [(f.name, datetime.datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")) for f in files]


def save(df, case):
    f = DATA / f"{case}_stream_results.csv"
    if f.exists():
        (DATA / f"{case}_stream_results.previous.csv").write_bytes(f.read_bytes())
    df.to_csv(f, index=False)
    return f


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bkp", help="Aspen Plus .bkp or .apw file (live link, Windows only)")
    ap.add_argument("--csv", help="Aspen stream summary exported as CSV")
    ap.add_argument("--case", default="aspen_10tpd")
    ap.add_argument("--run", action="store_true", help="run the simulation before reading")
    a = ap.parse_args()
    df = live_streams(a.bkp, a.run) if a.bkp else read_summary_csv(a.csv)
    print("wrote", save(df, a.case), len(df), "streams")
