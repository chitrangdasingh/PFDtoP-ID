"""
make_smart_pid.py - the "smart P&ID": the drawing sheet made clickable, plus machine-readable handover files.

Click any equipment, line, valve, instrument or motor on the sheet to see its data: size and nominal bore, pipe class,
flow direction (from -> to), flow, velocity, temperature, pressure, density, composition, connected items, control
and trip links, and the consequences of its failure.

Handover files written next to the sheet:
  pid_model.json        every asset and line with all properties and relations (the full machine-readable model)
  pid.sfiles.txt        PFD and P&ID as SFILES 2.0-style strings
  pid_graph.graphml     the P&ID graph in the PID2Graph GraphML schema (label: general / valve / instrumentation),
                        with tags and properties as extra attributes
  pid_dexpi_style.xml   a DEXPI 1.3-style Proteus XML (equipment, piping network systems, instrumentation functions).
                        Written without validation; check it with pyDEXPI before relying on it (see DEPLOY.md).

Run:  python make_smart_pid.py --streams streams_aspen_10tpd.csv --out out_aspen_10tpd --title "Aspen 10 TPD case"
"""
import argparse
import json
import math
import re
from pathlib import Path
from xml.sax.saxutils import escape as _escape


def escape(v):
    return _escape(str(v), {'"': "&quot;"})

import networkx as nx
import pandas as pd

import failure as F
import make_pid_sheet as sheet
import pfd_to_pid as p
import sfiles
import workflow

HERE = Path(__file__).parent
ID_MM = dict(p.NPS)
FIRST = {"A": "Analysis", "B": "Burner / flame", "F": "Flow", "L": "Level", "P": "Pressure", "S": "Speed", "T": "Temperature", "X": "On/off", "H": "Hand"}
LATER = {"A": "alarm", "C": "control", "D": "differential", "E": "element", "G": "gauge", "H": "high", "I": "indicate", "L": "low", "S": "switch",
         "T": "transmit", "V": "valve"}


def clean(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return v.item() if hasattr(v, "item") else v


def decode_letters(L):
    if L == "PSV":
        return "Pressure safety valve"
    words = [FIRST.get(L[0], L[0])] + [LATER.get(c, c) for c in L[1:]]
    return " ".join(words[:1]) + " " + " ".join(words[1:])


def velocity(size, vol):
    try:
        d = ID_MM[float(str(size).replace('"', ""))] / 1000
        return round(vol / 3600 / (math.pi / 4 * d * d), 1)
    except (KeyError, ValueError, TypeError):
        return None


def build_model(case, zones=None):
    """All assets, lines and relations of one case as plain dictionaries (the handover model), with traceability."""
    G, eq, lines, inst, valves = p.build(case)
    zones = zones or {}
    stem = Path(case).stem.replace("streams_", "")
    duties = pd.read_csv(p.DATA / f"duties_{stem}.csv") if (p.DATA / f"duties_{stem}.csv").exists() else None
    imp = F.Impact(G, eq, lines, duties)
    for m in G.graph.get("motors", []):
        G.add_node(m["tag"], kind="motor", drives=m["drives"], area=m["area"], desc=m["description"])
    assets, rels = {}, []
    eqi = eq.set_index("tag")
    for t, r in eqi.iterrows():
        ins = [dict(line=l["line_no"]) for l in lines[lines.to == t].to_dict("records")]
        outs = [dict(line=l["line_no"]) for l in lines[lines["from"] == t].to_dict("records")]
        assets[t] = dict(id=t, kind="equipment", title=r["name"], type=r["type"], area=int(r.area), duty=clean(r.get("duty")) or "",
                         source=G.nodes[t].get("source", ""),
                         inlets=[x["line"] for x in ins], outlets=[x["line"] for x in outs])
    for d in lines.to_dict("records"):
        nb = p.to_nb(d["size"])
        parts = d["line_no"].split("-")
        spec, ins_code = (parts[-2], parts[-1]) if len(parts) >= 5 else ("", "")
        vol = clean(d.get("vol_m3h"))
        assets[d["line_no"]] = dict(
            id=d["line_no"], kind="line", title=d["description"], pfd_stream=d["pfd_stream"], service=d["service"], phase=d["phase"],
            from_=d["from"], to=d["to"], flow_direction=f"{d['from']} -> {d['to']}", size=d["size"], nominal_bore=nb, pipe_spec=spec,
            insulation={"H": "H - heat conservation", "P": "P - personnel protection", "N": "N - none"}.get(ins_code, ""), flow_kgh=clean(d["flow_kgh"]), temp_C=clean(d["temp_C"]),
            press_bar=clean(d["press_bar"]), vol_m3h=vol, density_kgm3=clean(d.get("density_kgm3")), velocity_ms=velocity(d["size"], vol) if vol else None,
            enthalpy_kW=clean(d.get("enthalpy_kW")), composition=clean(d.get("composition")) or "",
            source=d.get("source", ""), size_source=d.get("size_source", ""))
        rels += [(d["from"], "FEEDS", d["line_no"]), (d["line_no"], "FEEDS", d["to"])]
    for v in valves.itertuples():
        pre = F.prefix(v.tag)
        fo = G.graph.get("fail_override", {}).get(v.tag)
        assets[v.tag] = dict(id=v.tag, kind="valve" if pre != "PSV" else "instrument", title=v.type, line=v.line, size=v.size, nominal_bore=p.to_nb(v.size),
                             fail_position=(fo[0] + f" (correction {fo[1]})") if fo else F.FAIL_POSITION.get(pre, ("-", ""))[0] + " (assumed)",
                             area=int(v.area), source=getattr(v, "source", ""))
        if pre != "PSV":
            rels.append((v.tag, "INSTALLED_ON", v.line))
    for i in inst.itertuples():
        a = assets.setdefault(i.tag, {})
        a.update(id=i.tag, kind="instrument", title=i.description, function=i.function, function_meaning=decode_letters(i.function),
                 loop=i.tag.split("-")[1], location={"FIELD": "field", "DCS": "control system (DCS)", "SIS": "safety system"}[i.location],
                 attached_to=i.attached_to, area=int(i.area), source=getattr(i, "source", ""))
        rels.append((i.tag, "INSTALLED_ON" if i.attached_to in lines.line_no.values else "MEASURES", i.attached_to))
    for m in G.graph.get("motors", []):
        assets[m["tag"]] = dict(id=m["tag"], kind="motor", title=m["description"], drives=m["drives"], area=int(m["area"]))
        rels.append((m["tag"], "DRIVES", m["drives"]))
    for u, v, d in G.edges(data=True):
        if d["kind"] in ("signal", "trip") and G.nodes[u]["kind"] == "instrument" and G.nodes.get(v, {}).get("kind") in ("valve", "equipment", "instrument"):
            if G.nodes[v]["kind"] == "instrument":
                rels.append((u, "SENDS_TO", v))
            else:
                rels.append((u, "TRIPS" if d["kind"] == "trip" else "CONTROLS", v))
    # failure consequences (first mode = the assumed fail position)
    for k, a in assets.items():
        if k in G and a["kind"] != "line":
            fm = []
            for code, desc in F.modes(G, k):
                res = imp.analyse(k, code)
                sh = res["energy"]
                shaft = float(sh[sh["item"].str.contains("shaft", case=False)]["change_kW"].sum()) if len(sh) else 0.0
                fm.append(dict(mode=desc, effect=res["summary"], notes=res["notes"], lines_lost=[dict(r) for _, r in res["lines"].head(6).iterrows()],
                               engine_power_change_kW=round(shaft, 1), protections=res["protections"]))
            a["failure_modes"] = fm
    # engineering data (pipe class / MOC, insulation, design, ends, fail position, set points, nozzles)
    import engineering
    ET_ = engineering.all_tables(case, G, eq, lines, inst, valves)
    clean_ = lambda d: {k: (None if (isinstance(v, float) and pd.isna(v)) else v) for k, v in d.items()}
    for r_ in ET_["line_designation"].to_dict("records"):
        if r_["line_no"] in assets:
            assets[r_["line_no"]]["eng"] = clean_({k: r_[k] for k in ("moc", "material_spec", "rating", "insulation_purpose", "insulation_mm", "design_temp_C",
                                                                     "design_press_barg", "test_press_barg", "end_connection", "drains_vents")})
    for r_ in ET_["valve_schedule"].to_dict("records"):
        if r_["tag"] in assets:
            assets[r_["tag"]]["eng"] = clean_(dict(fail_position=r_["fail_position"], reason=r_["reason"], actuator=r_["actuator"], body_moc=r_["body_moc"],
                                                   end_connection=r_["end_connection"]))
    for r_ in ET_["setpoints"].to_dict("records"):
        if r_["tag"] in assets:
            assets[r_["tag"]]["eng"] = clean_({k: r_[k] for k in ("unit", "normal", "set_point", "alarm_low", "alarm_high", "trip_low", "trip_high", "range", "basis")})
    for t_, g_ in ET_["nozzles"].groupby("equipment"):
        if t_ in assets:
            assets[t_]["eng"] = dict(nozzles="; ".join(f"{x.nozzle} {x.purpose} {x.size}" for x in g_.itertuples()))
    # traceability: drawing zone, corrections and review remarks for each asset
    corr = workflow.corrections(case)
    revs = workflow.reviews(case)
    line_of_stream = {a["pfd_stream"]: k for k, a in assets.items() if a["kind"] == "line"}
    for k, a in assets.items():
        a["zone"] = zones.get(k, "")
        keys = {k} | ({a["pfd_stream"]} if a["kind"] == "line" else set())
        a["corrections"] = [dict(r) for _, r in corr[corr.target.isin(keys)].iterrows()]
        a["remarks"] = [dict(r) for _, r in revs[revs.target == k].iterrows()]
    for _, r in corr.iterrows():                       # corrections that target a stream also show on the line
        if r.target in line_of_stream and r.target not in assets:
            pass
    model = dict(case=case, revision=workflow.revision_letter(case), applied_corrections=G.graph.get("applied_corrections", []),
                 assets=assets, relations=[dict(subject=s, relation=r, object=o) for s, r, o in sorted(set(rels))])
    return model, G, eq, lines, inst, valves


# ---------------------------------------------------------------------------- handover files
def graphml_pid2graph(G, path):
    """P&ID graph in the PID2Graph schema (undirected; label general / valve / instrumentation; edge_label solid / non-solid)."""
    H = nx.Graph()
    lab = {"equipment": "general", "valve": "valve", "instrument": "instrumentation", "tap": "connector"}
    for n, d in G.nodes(data=True):
        if d["kind"] in lab:
            H.add_node(n, label=lab[d["kind"]], tag=n, kind=d["kind"], type=str(d.get("type") or d.get("letters") or d.get("desc") or ""))
    for u, v, d in G.edges(data=True):
        if u in H and v in H:
            H.add_edge(u, v, edge_label="solid" if d["kind"] == "process" else "non-solid", line=str(d.get("line", "")), relation=d["kind"])
    nx.write_graphml(H, path)


DEXPI_EQ = {"gasifier": "Reactor", "auger_reactor": "Reactor", "heat_exchanger": "HeatExchanger", "gas_cooler": "HeatExchanger", "condenser": "HeatExchanger",
            "solids_cooler": "HeatExchanger", "electric_heater_vessel": "ElectricHeater", "cyclone": "Cyclone", "bag_filter": "Filter", "filter": "Filter",
            "knockout_drum": "Vessel", "tank": "Tank", "bin": "Silo", "hopper": "Silo", "screw_heated": "ScrewConveyor", "blower": "Blower",
            "flare": "Flare", "stack": "Stack", "cooling_tower": "CoolingTower", "solids_separator": "Separator", "air_intake": "Filter", "package": "CustomEquipment"}
DEXPI_VALVE = {"HV": "GateValve", "CK": "CheckValve", "PCV": "OperatedValve", "XV": "OperatedValve", "FV": "GlobeValve", "TV": "GlobeValve", "LV": "GlobeValve",
               "SG": "GateValve", "RV": "RotaryValve", "FA": "FlameArrestor", "PSV": "SafetyValveOrFitting"}


def dexpi_style_xml(model, G, path):
    """DEXPI 1.3-style Proteus XML. Structure follows the published DEXPI / Proteus element names; not validated here."""
    A = model["assets"]
    out = ['<?xml version="1.0" encoding="utf-8"?>',
           '<PlantModel xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
           '  <PlantInformation Application="pfd2pid" ApplicationVersion="1" Date="" Discipline="PID" SchemaVersion="4.1.1" Units="mm" Is3D="no"/>',
           '  <MetaData/>']
    ids = {k: f"ID{i}" for i, k in enumerate(A)}
    att = lambda name, val: f'<GenericAttribute Name="{escape(name)}" Value="{escape(str(val))}" Format="string"/>'
    for k, a in A.items():
        if a["kind"] == "equipment":
            out.append(f'  <Equipment ID="{ids[k]}" TagName="{escape(k)}" ComponentClass="{DEXPI_EQ.get(a["type"], "CustomEquipment")}">'
                       f'<GenericAttributes Set="DexpiAttributes">{att("TagNameAssignmentClass", k)}{att("EquipmentDescription", a["title"])}</GenericAttributes>'
                       f'<Nozzle ID="{ids[k]}_N1" ComponentClass="Nozzle"/></Equipment>')
    for k, a in A.items():
        if a["kind"] != "line":
            continue
        comps = [v for v, x in A.items() if x["kind"] == "valve" and x.get("line") == k]
        out.append(f'  <PipingNetworkSystem ID="{ids[k]}" ComponentClass="PipingNetworkSystem">'
                   f'<GenericAttributes Set="DexpiAttributes">{att("LineNumberAssignmentClass", k)}{att("NominalDiameterRepresentationAssignmentClass", a.get("nominal_bore") or a.get("size"))}'
                   f'{att("PipingClassCodeAssignmentClass", a.get("pipe_spec", ""))}{att("FluidCodeAssignmentClass", a.get("service", ""))}</GenericAttributes>'
                   f'<PipingNetworkSegment ID="{ids[k]}_S" ComponentClass="PipingNetworkSegment">')
        for v in comps:
            out.append(f'    <PipingComponent ID="{ids[v]}" ComponentClass="{DEXPI_VALVE.get(F.prefix(v), "PipingComponent")}" TagName="{escape(v)}"/>')
        out.append(f'    <Connection FromID="{ids.get(a["from_"], "")}_N1" ToID="{ids.get(a["to"], "")}_N1"/></PipingNetworkSegment></PipingNetworkSystem>')
    for k, a in A.items():
        if a["kind"] == "instrument" and a.get("function") and a["function"] != "PSV":
            out.append(f'  <ProcessInstrumentationFunction ID="{ids[k]}" ComponentClass="ProcessInstrumentationFunction">'
                       f'<GenericAttributes Set="DexpiAttributes">{att("ProcessInstrumentationFunctionCategoryAssignmentClass", a["function"][0])}'
                       f'{att("ProcessInstrumentationFunctionModifierAssignmentClass", a["function"][1:])}{att("ProcessInstrumentationFunctionNumberAssignmentClass", a.get("loop", ""))}'
                       f'{att("LocationAssignmentClass", a.get("location", ""))}</GenericAttributes></ProcessInstrumentationFunction>')
    for r in model["relations"]:
        if r["relation"] in ("CONTROLS", "TRIPS", "SENDS_TO"):
            out.append(f'  <InformationFlow ComponentClass="{"SignalConveyingFunction"}" FromID="{ids.get(r["subject"], "")}" ToID="{ids.get(r["object"], "")}" Type="{r["relation"]}"/>')
    out.append("</PlantModel>")
    Path(path).write_text("\n".join(out), encoding="utf-8")


def write_handover(case, out_dir, model, G, eq, lines):
    out = Path(out_dir)
    out.mkdir(exist_ok=True)
    (out / "pid_model.json").write_text(json.dumps(model, indent=1, default=str), encoding="utf-8")
    st = pd.read_csv(p.DATA / case, dtype={"no": str})
    pfd = sfiles.encode(sfiles.pfd_graph(eq, st))
    pid = sfiles.encode(sfiles.pid_graph(G))
    (out / "pid.sfiles.txt").write_text(f"# PFD (SFILES 2.0-style)\n{pfd}\n\n# P&ID (SFILES 2.0-style, with valves, instruments and signals)\n{pid}\n", encoding="utf-8")
    graphml_pid2graph(G, out / "pid_graph.graphml")
    dexpi_style_xml(model, G, out / "pid_dexpi_style.xml")
    return pfd, pid


# ---------------------------------------------------------------------------- clickable sheet
PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title><style>
:root{--ink:#1c2430;--muted:#5d6878;--line:#d5dae2;--accent:#0b62c4;--hit:#e8590c;--panel:#fff;--bg:#eef1f5;--chip:#e9eef6}
@media (prefers-color-scheme:dark){:root{--ink:#e6eaf0;--muted:#9aa5b5;--line:#313a47;--accent:#6aaeff;--panel:#1c222b;--bg:#14181e;--chip:#27303c}}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink);background:var(--bg);height:100vh;display:flex;flex-direction:column}
header{display:flex;gap:10px;align-items:center;padding:8px 12px;background:var(--panel);border-bottom:1px solid var(--line);flex-wrap:wrap}
h1{font-size:15px;margin:0;font-weight:600}input{padding:6px 9px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);font:inherit;min-width:220px}
button{font:inherit;padding:5px 10px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink);cursor:pointer}
main{flex:1;display:grid;grid-template-columns:1fr 360px;min-height:0}#sheet{overflow:auto;background:#fff}#sheet svg{display:block}
#sheet [data-id]{cursor:pointer}#sheet .on>*,#sheet path.on{stroke:var(--hit)!important}#sheet path.hit.on{stroke:rgba(232,89,12,.45)!important}
#sheet g.on text{fill:var(--hit)!important}aside{background:var(--panel);border-left:1px solid var(--line);overflow:auto;padding:12px 14px}
h2{font-size:17px;margin:0 0 2px}h3{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:16px 0 4px}
.kind{display:inline-block;font-size:12px;background:var(--chip);border-radius:10px;padding:1px 9px;color:var(--muted)}
table{border-collapse:collapse;width:100%;font-size:13px}td{padding:3px 4px;border-bottom:1px solid var(--line);vertical-align:top}td:first-child{color:var(--muted);width:42%}
.link{color:var(--accent);cursor:pointer;text-decoration:underline;background:none;border:0;padding:0;font:inherit}.note{font-size:12px;color:var(--muted)}
.fm{border:1px solid var(--line);border-radius:8px;padding:8px;margin:6px 0}.fm b{display:block;margin-bottom:3px}
@media (max-width:900px){main{grid-template-columns:1fr;grid-template-rows:1fr 300px}aside{border-left:0;border-top:1px solid var(--line)}}
#sheet.cad{background:#000}#sheet svg.cad rect[style*="fill:#fff"]{fill:#000!important}#sheet svg.cad *{stroke:#d9d9d9}
#sheet svg.cad text,#sheet svg.cad tspan{fill:#e6e6e6}#sheet svg.cad .w{fill:#000}#sheet svg.cad .f{fill:#e6e6e6}
#sheet svg.cad .proc{stroke:#ffff00}#sheet svg.cad .proch{fill:#ffff00;stroke:#ffff00}#sheet svg.cad .solids{stroke:#ff9f3f}#sheet svg.cad .solidsh{fill:#ff9f3f;stroke:#ff9f3f}
#sheet svg.cad .util{stroke:#3fa9ff}#sheet svg.cad .utilh{fill:#3fa9ff;stroke:#3fa9ff}#sheet svg.cad .sig{stroke:#ff4fff}#sheet svg.cad [stroke="#c00"]{stroke:#ff3b3b}
#sheet svg.cad .ins *{stroke:#00ffff}#sheet svg.cad .ins text{fill:#00ffff}#sheet svg.cad .vlv *{stroke:#3bff3b}#sheet svg.cad .vlv .f{fill:#3bff3b}
#sheet svg.cad .mot *{stroke:#ffff3b}#sheet svg.cad .mot text{fill:#ffff3b}#sheet svg.cad .eq *{stroke:#fff;fill:#000}#sheet svg.cad .hit,#sheet svg.cad .hitbox{stroke:transparent;fill:transparent}
#sheet svg.cad .on>*,#sheet svg.cad path.on{stroke:#ff8c00!important}
svg.hide-PROCESS .proc,svg.hide-PROCESS .proch,svg.hide-SOLIDS .solids,svg.hide-SOLIDS .solidsh,svg.hide-UTILITY .util,svg.hide-UTILITY .utilh,svg.hide-SIGNAL .sig,
svg.hide-INST-CONN .thin,svg.hide-EQUIPMENT .eq,svg.hide-VALVES .vlv,svg.hide-INSTRUMENTS .ins,svg.hide-MOTORS .mot,svg.hide-TEXT text,svg.hide-REMARKS .rmk{display:none}
#layers{display:none;position:absolute;right:12px;top:46px;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 12px;z-index:5;font-size:13px}
#layers label{display:block;white-space:nowrap}#xy{font:12px ui-monospace,Menlo,Consolas,monospace;color:var(--muted)}header{position:relative}
</style></head><body>
<header><h1>__TITLE__</h1><input id="q" type="search" placeholder="Find a tag or line, e.g. R-401, FV-4021, S4" aria-label="Find a tag">
<button id="zo" aria-label="Zoom out">&minus;</button><button id="fit">Fit</button><button id="zi" aria-label="Zoom in">+</button>
<button id="cadbtn">CAD view</button><button id="laybtn">Layers</button><span id="xy"></span>
<div id="layers">__LAYERS__</div></header>
<main><div id="sheet">__SVG__</div><aside id="p"></aside></main>
<script>
const M=__DATA__, A=M.assets, R=M.relations;
const box=document.getElementById('sheet'), svg=box.querySelector('svg'), panel=document.getElementById('p');
const W=parseFloat(svg.getAttribute('width')), H=parseFloat(svg.getAttribute('height')); let z=1;
function zoom(f){z=Math.max(.2,Math.min(5,f));svg.setAttribute('width',W*z);svg.setAttribute('height',H*z)}
function fit(){zoom(box.clientWidth/W)}
document.getElementById('zi').onclick=()=>zoom(z*1.25);document.getElementById('zo').onclick=()=>zoom(z/1.25);document.getElementById('fit').onclick=fit;
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function row(k,v){return v===null||v===undefined||v===''?'':'<tr><td>'+esc(k)+'</td><td>'+v+'</td></tr>'}
function ref(id){return A[id]?'<button class="link" data-go="'+esc(id)+'">'+esc(id)+'</button>':esc(id)}
function num(v,u){return v===null||v===undefined?'':(+v).toLocaleString(undefined,{maximumFractionDigits:2})+' '+u}
function home(){panel.innerHTML='<h2>Smart P&amp;ID</h2><p class="note">Click any equipment, line, valve, instrument or motor on the sheet. You get its size, pipe class, flow direction, flow and conditions, its connections, and what happens if it fails.</p><h3>On this sheet</h3><table>'+['equipment','line','valve','instrument','motor'].map(k=>row(k,Object.values(A).filter(a=>a.kind===k).length)).join('')+'</table><p class="note">'+esc(M.basis||'')+'</p>'}
function clear(){box.querySelectorAll('.on').forEach(e=>e.classList.remove('on'))}
function show(id){const a=A[id];if(!a)return;clear();box.querySelectorAll('[data-id="'+CSS.escape(id)+'"]').forEach(e=>{e.classList.add('on');});
 const first=box.querySelector('[data-id="'+CSS.escape(id)+'"]');if(first){const r=first.getBoundingClientRect(),c=box.getBoundingClientRect();if(r.left<c.left||r.right>c.right||r.top<c.top||r.bottom>c.bottom){box.scrollLeft+=r.left-c.left-c.width/2;box.scrollTop+=r.top-c.top-c.height/2}}
 let h='<h2>'+esc(id)+'</h2><span class="kind">'+esc(a.kind)+'</span> <span class="kind">zone '+esc(window.zoneOf?zoneOf(id):'')+'</span><p>'+esc(a.title)+'</p><h3>Data</h3><table>';
 if(a.kind==='line'){h+=row('Flow direction',ref(a.from_)+' &rarr; '+ref(a.to))+row('PFD / Aspen stream',esc(a.pfd_stream))+row('Service',esc(a.service)+' ('+esc(a.phase)+')')+row('Size',esc(a.size)+(a.nominal_bore?' ('+esc(a.nominal_bore)+')':''))+row('Pipe class / spec',esc(a.pipe_spec))+row('Insulation',esc(a.insulation))+row('Mass flow',num(a.flow_kgh,'kg/h'))+row('Volume flow',num(a.vol_m3h,'m³/h'))+row('Velocity (at this size)',num(a.velocity_ms,'m/s'))+row('Temperature',num(a.temp_C,'°C'))+row('Pressure',num(a.press_bar,'bar'))+row('Density',num(a.density_kgm3,'kg/m³'))+row('Enthalpy flow',num(a.enthalpy_kW,'kW'))+row('Composition (mass)',esc(a.composition));}
 else if(a.kind==='equipment'){h+=row('Type',esc(a.type))+row('Area',a.area)+row('Duty / power',esc(a.duty))+row('Inlets',(a.inlets||[]).map(ref).join(', '))+row('Outlets',(a.outlets||[]).map(ref).join(', '));}
 else if(a.kind==='valve'){h+=row('On line',ref(a.line))+row('Size',esc(a.size)+(a.nominal_bore?' ('+esc(a.nominal_bore)+')':''))+row('Assumed fail position',esc(a.fail_position));}
 else if(a.kind==='instrument'){h+=row('Function',esc(a.function)+' = '+esc(a.function_meaning))+row('Loop number',esc(a.loop))+row('Location',esc(a.location))+row('On',ref(a.attached_to||a.line));}
 else if(a.kind==='motor'){h+=row('Drives',ref(a.drives))}
 h+='</table>';
 if(a.eng){const L={moc:'MOC',material_spec:'Pipe material',rating:'Rating',insulation_purpose:'Insulation',insulation_mm:'Insulation (mm)',design_temp_C:'Design temperature (°C)',design_press_barg:'Design pressure (bar g)',test_press_barg:'Test pressure (bar g)',end_connection:'End connection',drains_vents:'Drains / vents / purge',fail_position:'Fail position',reason:'Why',actuator:'Actuator',body_moc:'Body MOC',unit:'Unit',normal:'Normal (Aspen)',set_point:'Set point',alarm_low:'Alarm low',alarm_high:'Alarm high',trip_low:'Trip low',trip_high:'Trip high',range:'Range',basis:'Basis',nozzles:'Nozzles'};
  h+='<h3>Engineering data (proposed)</h3><table>'+Object.keys(a.eng).map(k=>row(L[k]||k,esc(a.eng[k]))).join('')+'</table>';}
 h+='<h3>Traceability</h3><table>'+row('Drawing zone',esc(a.zone||(window.zoneOf?zoneOf(id):'')))+row('Comes from',esc(a.source))+row('Size from',esc(a.size_source))+row('Revision',esc(M.revision))+'</table>';
 if(a.corrections&&a.corrections.length)h+='<table>'+a.corrections.map(c=>row(c.id+' ('+c.status+')',esc(c.action)+': '+esc(c.value)+'<div class="note">'+esc(c.reason)+' - '+esc(c.by)+', '+esc(c.date)+'</div>')).join('')+'</table>';
 if(a.remarks&&a.remarks.length)h+='<h3>Review remarks</h3>'+a.remarks.map(r=>'<div class="fm"><b>'+esc(r.id)+' ('+esc(r.status)+', '+esc(r.category)+')</b>'+esc(r.remark)+'<div class="note">'+esc(r.reviewer)+', '+esc(r.date)+(r.response?'<br>Response: '+esc(r.response)+(r.closed_by?' - '+esc(r.closed_by):''):'')+'</div></div>').join('');
 const rel=R.filter(r=>r.subject===id||r.object===id);
 if(rel.length)h+='<h3>Connections</h3><table>'+rel.map(r=>r.subject===id?row(r.relation.toLowerCase().replace('_',' ')+' →',ref(r.object)):row('← '+r.relation.toLowerCase().replace('_',' '),ref(r.subject))).join('')+'</table>';
 if(a.failure_modes&&a.failure_modes.length){h+='<h3>If it fails</h3>'+a.failure_modes.map(f=>'<div class="fm"><b>'+esc(f.mode)+'</b>'+esc(f.effect)+(f.notes&&f.notes.length?'<div class="note">'+f.notes.map(esc).join('<br>')+'</div>':'')+(f.engine_power_change_kW?'<div>Engine net power: '+num(f.engine_power_change_kW,'kW')+'</div>':'')+(f.lines_lost&&f.lines_lost.length?'<table>'+f.lines_lost.map(l=>row(l.stream+' ('+l.service+')',num(l.design_kgh,'kg/h')+' → '+num(l.after_kgh,'kg/h'))).join('')+'</table>':'')+(f.protections&&f.protections.length?'<div class="note">Protections on affected equipment: '+f.protections.map(esc).join('; ')+'</div>':'')+'</div>').join('')+'<p class="note">Estimate from connections and design flows; not a dynamic simulation. Fail positions are assumed.</p>';}
 panel.innerHTML=h;panel.querySelectorAll('[data-go]').forEach(b=>b.onclick=()=>show(b.dataset.go))}
box.addEventListener('click',e=>{const t=e.target.closest('[data-id]');if(t)show(t.dataset.id);else{clear();home()}});
document.getElementById('q').addEventListener('change',e=>{const v=e.target.value.trim().toLowerCase();const k=Object.keys(A).find(x=>x.toLowerCase()===v)||Object.keys(A).find(x=>x.toLowerCase().includes(v))||Object.keys(A).find(x=>String(A[x].pfd_stream||'').toLowerCase()===v);if(k)show(k)});
const cad=document.getElementById('cadbtn');cad.onclick=()=>{const on=svg.classList.toggle('cad');box.classList.toggle('cad',on);cad.textContent=on?'Paper view':'CAD view'};
const lay=document.getElementById('layers');document.getElementById('laybtn').onclick=()=>{lay.style.display=lay.style.display==='block'?'none':'block'};
lay.querySelectorAll('input').forEach(c=>c.onchange=()=>svg.classList.toggle('hide-'+c.value,!c.checked));
function user(ev){const r=svg.getBoundingClientRect();return[(ev.clientX-r.left)*W/r.width,(ev.clientY-r.top)*H/r.height]}
function zone(x,y){const M=26,c=Math.floor((x-M)/((W-2*M)/12)),rr=Math.floor((y-M)/((H-2*M)/8));return (c<0||c>11||rr<0||rr>7)?'-':'ABCDEFGH'[7-rr]+'-'+(12-c)}
svg.addEventListener('mousemove',ev=>{const [x,y]=user(ev);document.getElementById('xy').textContent='X '+x.toFixed(0)+'  Y '+(H-y).toFixed(0)+'  zone '+zone(x,y)});
window.zoneOf=function(id){const el=box.querySelector('[data-id="'+CSS.escape(id)+'"]');if(!el)return '';const b=el.getBoundingClientRect(),r=svg.getBoundingClientRect();return zone((b.left+b.width/2-r.left)*W/r.width,(b.top+b.height/2-r.top)*H/r.height)};
home();fit();window.addEventListener('resize',fit);
</script></body></html>"""


def smart_sheet(case, title, out_dir, **sheet_kw):
    out = Path(out_dir)
    remarks = workflow.reviews(case).to_dict("records")
    svg, _, info = sheet.draw(case, title, out, remarks=remarks, **sheet_kw)
    model, G, eq, lines, inst, valves = build_model(case, zones=info.get("zones"))
    model["basis"] = "Draft generated by rules from the stream table; not reviewed, no HAZOP."
    pfd, pid = write_handover(case, out, model, G, eq, lines)
    blob = json.dumps(model, default=str).replace("</", "<\\/")
    layers = "".join(f'<label><input type="checkbox" value="{k}" checked> {k.title().replace("-", " ")}</label>'
                     for k in ["PROCESS", "SOLIDS", "UTILITY", "SIGNAL", "INST-CONN", "EQUIPMENT", "VALVES", "INSTRUMENTS", "MOTORS", "TEXT", "REMARKS"])
    html = PAGE.replace("__TITLE__", f"Smart P&amp;ID - {escape(title)}").replace("__SVG__", svg).replace("__DATA__", blob).replace("__LAYERS__", layers)
    (out / "pid_smart.html").write_text(html, encoding="utf-8")
    return html, model, pfd, pid


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--streams", default="streams_aspen_10tpd.csv")
    ap.add_argument("--out", default="out_aspen_10tpd")
    ap.add_argument("--title", default="Aspen 10 TPD case")
    a = ap.parse_args()
    html, model, pfd, pid = smart_sheet(a.streams, a.title, HERE / a.out)
    print(f"{len(model['assets'])} assets, {len(model['relations'])} relations -> {a.out}/pid_smart.html, pid_model.json, pid.sfiles.txt, pid_graph.graphml, pid_dexpi_style.xml")
