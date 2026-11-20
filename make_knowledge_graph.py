"""
make_knowledge_graph.py - turn the P&ID model into a knowledge graph.

Run the generator first, then:
    python make_knowledge_graph.py --out out_gasifier_full --title "Gasifier mode"

Writes into the output folder:
    knowledge_graph_triples.csv   subject, relation, object  (loadable into Neo4j, Excel, pandas)
    knowledge_graph_nodes.csv     one row per node with its properties
    knowledge_graph.html          interactive graph, opens in any browser
"""
import argparse
import json
from pathlib import Path

import networkx as nx
import pandas as pd

HERE = Path(__file__).parent

RELATIONS = {
    "FEEDS": "process flow, in flow direction",
    "INSTALLED_ON": "valve or instrument sits on this line",
    "MEASURES": "instrument reads this equipment",
    "CONTROLS": "controller drives this valve or equipment",
    "SENDS_TO": "transmitter signal to controller",
    "TRIPS": "safety function shuts this down",
    "DRIVES": "motor drives this equipment or rotary valve",
}


def build_kg(out):
    kg = json.loads((out / "knowledge_graph.json").read_text(encoding="utf-8"))
    lines = pd.read_excel(out / "PID_deliverables.xlsx", sheet_name="Line list", dtype={"pfd_stream": str})
    nodes = {n["id"]: n for n in kg["nodes"] if n["kind"] != "tap"}
    props, triples = {}, []
    for i, n in nodes.items():
        props[i] = dict(id=i, kind=n["kind"], label=n.get("name") or n.get("desc") or "", type=n.get("type") or n.get("letters") or "",
                        area=n.get("area"), location=n.get("loc", ""), duty=n.get("duty", ""))
    for l in json.loads(lines.to_json(orient="records")):
        props[l["line_no"]] = dict(id=l["line_no"], kind="line", label=l["description"], type=l["service"], area=None, location="",
                                   pfd_stream=l["pfd_stream"], phase=l["phase"], flow_kgh=l["flow_kgh"], temp_C=l["temp_C"],
                                   press_bar=l["press_bar"], size=l["size"],
                                   **{k: l[k] for k in ("vol_m3h", "density_kgm3", "enthalpy_kW", "composition") if k in l})
        triples += [(l["from"], "FEEDS", l["line_no"]), (l["line_no"], "FEEDS", l["to"])]
    for i, n in nodes.items():
        target = n.get("line") or n.get("on")
        if n["kind"] in ("valve", "instrument") and target in props and props[target]["kind"] == "line":
            triples.append((i, "INSTALLED_ON", target))
    for m in kg.get("graph", {}).get("motors", []):
        props[m["tag"]] = dict(id=m["tag"], kind="motor", label=m["description"], type="motor", area=m["area"], location="", duty="")
        triples.append((m["tag"], "DRIVES", m["drives"]))
    for k in kg["links"]:
        a, b = k["source"], k["target"]
        if k["kind"] == "process" or a not in nodes or b not in nodes:
            continue
        if k["kind"] == "trip":
            triples.append((a, "TRIPS", b))
        elif nodes[a]["kind"] == "equipment":
            triples.append((b, "MEASURES", a))
        elif nodes[b]["kind"] == "instrument":
            triples.append((a, "SENDS_TO", b))
        else:
            triples.append((a, "CONTROLS", b))
    return props, sorted(set(triples))


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f4f5f7;--panel:#fff;--ink:#1c2430;--muted:#5d6878;--line:#d5dae2;--accent:#0b62c4;--chip:#e9eef6;--edge:#9aa3b0;
--equipment:#1f5fbf;--linec:#6b7280;--valve:#b45309;--instrument:#0f766e;--motor:#7c3aed;--hit:#d9480f}
@media (prefers-color-scheme:dark){:root{--bg:#14181e;--panel:#1c222b;--ink:#e6eaf0;--muted:#9aa5b5;--line:#313a47;--accent:#6aaeff;--chip:#27303c;--edge:#5b6675;
--equipment:#6aaeff;--linec:#aab3c0;--valve:#f0a04b;--instrument:#4fd1c5;--motor:#b794f6;--hit:#ff8a4c}}
*{box-sizing:border-box}
body{margin:0;font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--ink);height:100vh;display:flex;flex-direction:column}
header{display:flex;gap:10px 16px;align-items:center;padding:8px 14px;background:var(--panel);border-bottom:1px solid var(--line);flex-wrap:wrap}
h1{font-size:15px;margin:0;font-weight:600}
input[type=search]{padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);font:inherit;min-width:200px}
label{font-size:13px;white-space:nowrap;cursor:pointer}
button{font:inherit;padding:5px 11px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink);cursor:pointer}
main{flex:1;display:grid;grid-template-columns:1fr 340px;min-height:0}
#g{width:100%;height:100%;display:block;cursor:grab;touch-action:none}
aside{background:var(--panel);border-left:1px solid var(--line);overflow:auto;padding:12px 14px}
h2{font-size:16px;margin:0 0 2px} h3{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:16px 0 4px}
table{border-collapse:collapse;width:100%;font-size:13px} td{padding:3px 4px;border-bottom:1px solid var(--line);vertical-align:top} td:first-child{color:var(--muted);width:40%}
.link{color:var(--accent);cursor:pointer;text-decoration:underline;background:none;border:0;padding:0;font:inherit}
.kind{display:inline-block;font-size:12px;background:var(--chip);border-radius:10px;padding:1px 9px;color:var(--muted)}
.note{font-size:12px;color:var(--muted)}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px;vertical-align:-1px}
.e{stroke:var(--edge);stroke-width:1.2;fill:none} .e.TRIPS{stroke:#d64545;stroke-dasharray:5 3} .e.DRIVES{stroke-dasharray:1 3} .e.CONTROLS,.e.SENDS_TO,.e.MEASURES{stroke-dasharray:3 3} .e.FEEDS{stroke-width:2}
.n circle,.n rect{stroke:var(--panel);stroke-width:1.5} .n text{font-size:10px;fill:var(--ink);pointer-events:none}
.n{cursor:pointer} .n.equipment text{font-weight:600;font-size:11px}
svg.dim .n:not(.on){opacity:.15} svg.dim .e:not(.on){opacity:.07} .e.on{stroke:var(--hit);stroke-width:2.4} .n.sel circle,.n.sel rect{stroke:var(--hit);stroke-width:3.5}
.n.hide,.e.hide{display:none}
footer{font-size:12px;color:var(--muted);padding:5px 14px;background:var(--panel);border-top:1px solid var(--line)}
@media (max-width:900px){main{grid-template-columns:1fr;grid-template-rows:1fr 240px}aside{border-left:0;border-top:1px solid var(--line)}}
</style></head><body>
<header><h1>__TITLE__</h1><input id="q" type="search" placeholder="Find a tag, e.g. R-401" aria-label="Find a tag">
<span id="rels"></span><button id="reset">Reset view</button></header>
<main><svg id="g" role="img" aria-label="Knowledge graph of plant assets and their relations"><g id="vp"><g id="es"></g><g id="ns"></g></g></svg>
<aside id="p"></aside></main><footer id="f"></footer>
<script>
const D=__DATA__, P=D.nodes, T=D.triples, REL=D.relations;
const NS='http://www.w3.org/2000/svg', svg=document.getElementById('g'), vp=document.getElementById('vp'), es=document.getElementById('es'), ns=document.getElementById('ns'), panel=document.getElementById('p');
const color={equipment:'var(--equipment)',line:'var(--linec)',valve:'var(--valve)',instrument:'var(--instrument)',motor:'var(--motor)'};
function el(t,a){const e=document.createElementNS(NS,t); for(const k in a)e.setAttribute(k,a[k]); return e}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
svg.insertBefore(el('defs',{}),vp).innerHTML='<marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M1 1L9 5L1 9" fill="none" stroke="context-stroke" stroke-width="1.6"/></marker>';
const R={equipment:13,line:6,valve:7,instrument:7,motor:7}, eEls=[], nEls={};
T.forEach(([s,r,o])=>{const a=P[s],b=P[o]; if(!a||!b)return; const dx=b.x-a.x,dy=b.y-a.y,d=Math.hypot(dx,dy)||1,ra=R[a.kind]+1,rb=R[b.kind]+3;
 const e=el('line',{x1:a.x+dx/d*ra,y1:a.y+dy/d*ra,x2:b.x-dx/d*rb,y2:b.y-dy/d*rb,class:'e '+r,'marker-end':'url(#ah)'}); es.appendChild(e); eEls.push({e,s,r,o})});
Object.values(P).forEach(n=>{const g=el('g',{class:'n '+n.kind,transform:'translate('+n.x+','+n.y+')'}); const r=R[n.kind];
 g.appendChild(n.kind==='line'?el('rect',{x:-r,y:-r,width:2*r,height:2*r,rx:2,fill:color.line}):el('circle',{r,fill:color[n.kind]}));
 const t=el('text',{x:r+3,y:4}); t.textContent=n.kind==='line'?'':n.id; g.appendChild(t);
 const tt=el('title',{}); tt.textContent=n.id+' - '+n.label; g.appendChild(tt);
 g.addEventListener('click',ev=>{ev.stopPropagation(); pick(n.id)}); ns.appendChild(g); nEls[n.id]=g});
let view={x:0,y:0,k:1};
function apply(){vp.setAttribute('transform','translate('+view.x+','+view.y+') scale('+view.k+')')}
function fitView(){const w=svg.clientWidth,h=svg.clientHeight,k=Math.min(w/D.w,h/D.h)*.96; view={k,x:(w-D.w*k)/2,y:(h-D.h*k)/2}; apply()}
svg.addEventListener('wheel',ev=>{ev.preventDefault(); const f=ev.deltaY<0?1.15:1/1.15, r=svg.getBoundingClientRect(), mx=ev.clientX-r.left,my=ev.clientY-r.top; view.x=mx-(mx-view.x)*f; view.y=my-(my-view.y)*f; view.k*=f; apply()},{passive:false});
let drag=null; svg.addEventListener('pointerdown',ev=>{drag={x:ev.clientX-view.x,y:ev.clientY-view.y,moved:false}});
svg.addEventListener('pointermove',ev=>{if(!drag)return; const nx=ev.clientX-drag.x, ny=ev.clientY-drag.y; if(Math.abs(nx-view.x)+Math.abs(ny-view.y)>3)drag.moved=true; view.x=nx; view.y=ny; apply()});
window.addEventListener('pointerup',()=>{setTimeout(()=>drag=null,0)});
svg.addEventListener('click',()=>{if(drag&&drag.moved)return; home()});
const off=new Set();
const rels=document.getElementById('rels');
Object.keys(REL).forEach(r=>{const l=document.createElement('label'); l.title=REL[r]; l.innerHTML='<input type="checkbox" checked> '+r.toLowerCase().replace('_',' ')+' &nbsp;'; l.firstChild.onchange=ev=>{ev.target.checked?off.delete(r):off.add(r); eEls.forEach(x=>x.e.classList.toggle('hide',off.has(x.r)))}; rels.appendChild(l)});
function ref(id){return '<button class="link" data-go="'+esc(id)+'">'+esc(id)+'</button>'}
function row(a,b){return b===''||b==null?'':'<tr><td>'+esc(a)+'</td><td>'+b+'</td></tr>'}
function clear(){svg.classList.remove('dim'); svg.querySelectorAll('.on,.sel').forEach(x=>x.classList.remove('on','sel'))}
function downstream(id){const seen=new Set([id]),q=[id]; while(q.length){const a=q.shift(); T.forEach(([s,r,o])=>{if(s===a&&r==='FEEDS'&&!seen.has(o)){seen.add(o); q.push(o)}})} seen.delete(id); return [...seen]}
function pick(id){const n=P[id]; if(!n)return; clear(); svg.classList.add('dim'); nEls[id].classList.add('on','sel');
 const out=[],inn=[]; eEls.forEach(x=>{if(off.has(x.r))return; if(x.s===id){x.e.classList.add('on'); nEls[x.o].classList.add('on'); out.push(x)} else if(x.o===id){x.e.classList.add('on'); nEls[x.s].classList.add('on'); inn.push(x)}});
 let h='<h2>'+esc(id)+'</h2><span class="kind">'+esc(n.kind)+'</span><h3>Properties</h3><table>'+row('Description',esc(n.label))+row('Type',esc(n.type||''))+row('Location',esc(n.location||''))+row('Area',n.area)+row('PFD stream',n.pfd_stream)+row('Phase',n.phase)+row('Flow',n.flow_kgh!=null?n.flow_kgh+' kg/h':'')+row('Temperature',n.temp_C!=null?n.temp_C+' °C':'')+row('Pressure',n.press_bar!=null?n.press_bar+' bar':'')+row('Size',n.size?esc(n.size):'')+row('Volume flow',n.vol_m3h!=null?(+n.vol_m3h).toFixed(1)+' m³/h':'')+row('Enthalpy flow',n.enthalpy_kW!=null?n.enthalpy_kW+' kW':'')+row('Composition (mass)',n.composition?esc(n.composition):'')+row('Duty / power',n.duty?esc(n.duty):'')+'</table>';
 h+='<h3>Relations ('+(out.length+inn.length)+')</h3><table>'+out.map(x=>row(x.r.toLowerCase().replace('_',' ')+' →',ref(x.o))).join('')+inn.map(x=>row('← '+x.r.toLowerCase().replace('_',' '),ref(x.s))).join('')+'</table>';
 let start=[id]; if(n.kind==='instrument')start=out.filter(x=>x.r==='CONTROLS'||x.r==='TRIPS').map(x=>x.o); if(n.kind==='valve')start=out.filter(x=>x.r==='INSTALLED_ON').map(x=>x.o);
 const ds=[...new Set(start.flatMap(downstream))].filter(d=>P[d].kind==='equipment'&&d!==id).sort();
 h+='<h3>Downstream equipment</h3><p>'+(ds.length?ds.map(ref).join(', '):'None on the process side.')+'</p><p class="note">Reached by following "feeds" relations. Recycle lines make this list long.</p>';
 panel.innerHTML=h; panel.querySelectorAll('[data-go]').forEach(b=>b.onclick=()=>pick(b.dataset.go))}
const count=k=>Object.values(P).filter(n=>n.kind===k).length;
function home(){clear(); panel.innerHTML='<h2>Knowledge graph</h2><p class="note">Each dot is an asset; each arrow is a relation between two assets. Click a dot to see its properties, relations and what is downstream of it. Scroll to zoom, drag to move.</p><h3>Node types</h3><p>'+['equipment','line','valve','instrument','motor'].map(k=>'<span class="dot" style="background:'+color[k]+(k==='line'?';border-radius:2px':'')+'"></span>'+k+' ('+count(k)+')').join('<br>')+'</p><h3>Relations</h3><table>'+Object.keys(REL).map(r=>row(r.toLowerCase().replace('_',' '),esc(REL[r])+' ('+T.filter(t=>t[1]===r).length+')')).join('')+'</table>'}
document.getElementById('q').addEventListener('input',ev=>{const v=ev.target.value.trim().toLowerCase(); if(!v){home(); return} const m=Object.values(P).find(n=>n.id.toLowerCase()===v)||Object.values(P).find(n=>n.id.toLowerCase().startsWith(v))||Object.values(P).find(n=>(n.id+' '+n.label).toLowerCase().includes(v)); if(m)pick(m.id)});
document.getElementById('reset').onclick=()=>{fitView(); home(); document.getElementById('q').value=''};
document.getElementById('f').textContent=Object.keys(P).length+' nodes · '+T.length+' relations';
home(); fitView(); window.addEventListener('resize',fitView);
</script></body></html>
"""


def render(out, title="Plant"):
    """Write the triples / nodes CSVs and return the knowledge-graph page (one HTML string)."""
    out = Path(out)
    props, triples = build_kg(out)
    pd.DataFrame(triples, columns=["subject", "relation", "object"]).to_csv(out / "knowledge_graph_triples.csv", index=False)
    pd.DataFrame(props.values()).to_csv(out / "knowledge_graph_nodes.csv", index=False)

    G = nx.DiGraph()
    G.add_nodes_from(props)
    G.add_edges_from((s, o) for s, _, o in triples)
    W, H = 1700, 1150
    pos = nx.spring_layout(G.to_undirected(), k=1.6 / (len(G) ** 0.5), iterations=400, seed=11)
    xs, ys = [p[0] for p in pos.values()], [p[1] for p in pos.values()]
    for i, (x, y) in pos.items():
        props[i]["x"] = round(40 + (x - min(xs)) / (max(xs) - min(xs)) * (W - 160), 1)
        props[i]["y"] = round(40 + (y - min(ys)) / (max(ys) - min(ys)) * (H - 80), 1)

    data = {"nodes": props, "triples": triples, "relations": RELATIONS, "w": W, "h": H}
    blob = json.dumps(data, default=str).replace("</", "<\\/").replace("NaN", "null")
    return PAGE.replace("__TITLE__", f"Knowledge graph - {title}").replace("__DATA__", blob), len(props), len(triples)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out")
    ap.add_argument("--title", default="Plant")
    args = ap.parse_args()
    out = HERE / args.out
    html, n, t = render(out, args.title)
    (out / "knowledge_graph.html").write_text(html, encoding="utf-8")
    print(f"{n} nodes, {t} relations -> {out / 'knowledge_graph.html'}")


if __name__ == "__main__":
    main()
