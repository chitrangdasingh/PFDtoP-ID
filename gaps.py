"""
gaps.py - open challenges and gaps of the digitised P&ID, from fixed knowledge and from the live state of the case.
"""
import pandas as pd

import workflow

STATIC = [
    ("Process data", "high", "Aspen model is idealised (equilibrium reactors, full tar cracking and char conversion, no heat loss, no pressure drop, "
     "engine at lambda about 2.8)", "Send the Aspen feedback workbook to the model owner; re-run with the corrections", "Aspen engineer"),
    ("Process data", "high", "The engineer's case file (various feeds, 4-11-26) gives for the 45:55 O2:CO2 case an adiabatic oxidation zone of 1610 C and about 1369 C "
     "with 15 % heat loss, while the HMBD used here fixes OXID at 1100 C with no heat loss", "Agree one heat-loss basis and use it in both", "Aspen engineer"),
    ("Process data", "medium", "Cooling water flow and temperatures are assumed (32 to 42 C from the 208.5 kW duty)", "Confirm utility conditions", "Utilities"),
    ("Process data", "medium", "No trace species (H2S, COS, NH3, HCN, HCl) in the stream results", "Add components and the biomass ultimate analysis", "Aspen engineer"),
    ("Drawing", "high", "Start-up / engine-trip flare or vent line is not in the PFD, so it is not on the P&ID", "Add flare branch with automatic valve upstream of XV-4036", "Process"),
    ("Drawing", "high", "Utility headers (nitrogen purge, instrument air, cooling water header, power) are not drawn as headers", "Add a utility P&ID sheet", "Process"),
    ("Drawing", "medium", "Relief valve sizes and set pressures are proposals (design pressure); no relief study", "API 520 / 521 relief and deflagration study", "Safety"),
    ("Drawing", "medium", "Equipment nozzle orientations and elevations are not defined; nozzles are a schedule only", "Vendor data and layout", "Mechanical"),
    ("Safety", "high", "No HAZOP or SIL study yet; set points, trips and fail positions are rule proposals", "Run HAZOP on this revision; SIL for TSHH-4006, LSHH-4015, AI-4038 trips", "Safety"),
    ("Standards", "medium", "Symbols follow ISA-5.1 / ISO 10628 practice from general knowledge, not a licensed company symbol library", "Check against the company legend", "Drafting"),
    ("CAD", "medium", "DXF is a flat drawing (lines, text, solids) without AutoCAD blocks or attributes; not a Plant 3D smart P&ID", "Map symbols to company blocks if needed", "Drafting"),
    ("DEXPI", "medium", "The DEXPI-style export is not validated against the DEXPI schema; pyDEXPI not run here", "Validate with pyDEXPI / DEXPI tools", "Digital"),
    ("AI model", "medium", "P&ID suggestion model is small; pre-training on the PID2Graph dataset must be run on a PC with internet (fetch_pid2graph.py)",
     "Run fetch_pid2graph.py and compare accuracy", "Digital"),
    ("Copilot", "low", "Without an AI key the copilot uses a trained intent model and the knowledge base; free-form reasoning needs the key",
     "Add ANTHROPIC_API_KEY in Streamlit secrets; add plant documents to the knowledge base", "Digital"),
    ("Aspen link", "medium", "Live link to Aspen Plus works only on a Windows PC with Aspen and pywin32 (COM); it is untested here", "Test aspen_link.py on the engineer's PC", "Digital"),
    ("Digital twin", "medium", "No live data feed yet; the twin tab uses sliders. Signal list and node ids are ready for OPC UA / historian mapping",
     "Connect read_live() in twin.py to the plant historian", "Automation"),
]


def table(case, G=None, eng=None, model_disagreements=None):
    rows = [dict(area=a, severity=s, gap=g, next_step=n, owner=o, source="known") for a, s, g, n, o in STATIC]
    if eng is not None:
        sp = eng["setpoints"]
        tbd = sp[(sp.set_point.astype(str) == "") & (sp.alarm_high.astype(str) == "") & (sp.trip_high.astype(str) == "") & (sp.alarm_low.astype(str) == "")]
        if len(tbd):
            rows.append(dict(area="Engineering data", severity="medium", gap=f"{len(tbd)} instruments without set point or alarm: {', '.join(tbd.tag[:10])}",
                             next_step="Define in the Engineering data tab (or ask the copilot with an AI key to propose)", owner="Control", source="live"))
        ld = eng["line_designation"]
        tbd_l = ld[ld["size"].astype(str).str.startswith("TBD")]
        if len(tbd_l):
            rows.append(dict(area="Engineering data", severity="medium", gap=f"{len(tbd_l)} lines without size", next_step="Add flow data", owner="Process", source="live"))
    rv = workflow.reviews(case)
    if len(rv[rv.status == "open"]):
        rows.append(dict(area="Review", severity="medium", gap=f"{len(rv[rv.status == 'open'])} open review remark(s)", next_step="Close in the Review remarks tab",
                         owner="Reviewers", source="live"))
    cr = workflow.corrections(case)
    if len(cr[cr.status == "proposed"]):
        rows.append(dict(area="Review", severity="medium", gap=f"{len(cr[cr.status == 'proposed'])} proposed correction(s) waiting for approval",
                         next_step="Approve or reject in the Corrections tab", owner="Engineer", source="live"))
    if model_disagreements:
        rows.append(dict(area="AI model", severity="low", gap=f"{model_disagreements} place(s) where the AI model disagrees with the drawing",
                         next_step="Look at them in the AI P&ID model tab", owner="Engineer", source="live"))
    order = {"high": 0, "medium": 1, "low": 2}
    return pd.DataFrame(rows).sort_values(by="severity", key=lambda s: s.map(order), kind="stable")
