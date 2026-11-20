"""
make_viewer.py - build a self-contained interactive P&ID viewer (one HTML file).

Run the generator first, then:
    python make_viewer.py --out out_gasifier --title "Gasifier mode"
Open out_gasifier/viewer.html in any browser. No server needed.
"""
import argparse
import json
import re
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent

PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f4f5f7;--panel:#ffffff;--ink:#1c2430;--muted:#5d6878;--line:#d5dae2;--accent:#0b62c4;--hit:#d9480f;--chip:#e9eef6}
@media (prefers-color-scheme:dark){:root{--bg:#14181e;--panel:#1c222b;--ink:#e6eaf0;--muted:#9aa5b5;--line:#313a47;--accent:#6aaeff;--hit:#ff8a4c;--chip:#27303c}}
*{box-sizing:border-box}
body{margin:0;font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--ink);height:100vh;display:flex;flex-direction:column}
header{display:flex;gap:12px;align-items:center;padding:8px 14px;background:var(--panel);border-bottom:1px solid var(--line);flex-wrap:wrap}
header h1{font-size:15px;margin:0;font-weight:600}
header .tag{font-size:12px;color:var(--muted)}
header input{flex:1;min-width:180px;max-width:420px;padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);font:inherit}
button{font:inherit;padding:5px 11px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink);cursor:pointer}
button:hover{border-color:var(--accent)}
main{flex:1;display:grid;grid-template-columns:250px 1fr 330px;min-height:0}
aside{background:var(--panel);overflow:auto;padding:10px 12px}
#tree{border-right:1px solid var(--line)} #props{border-left:1px solid var(--line)}
details{margin-bottom:6px} summary{cursor:pointer;font-weight:600;padding:3px 0}
.item{display:block;width:100%;text-align:left;border:0;border-radius:4px;padding:2px 8px 2px 18px;background:none;font-size:13px}
.item:hover{background:var(--chip)} .item.on{background:var(--accent);color:#fff}
.item small{color:var(--muted);margin-left:6px} .item.on small{color:#e8f1ff}
#canvas{overflow:auto;background:#fff;position:relative}
#canvas svg{display:block}
#canvas g.node,#canvas g.edge{cursor:pointer}
#canvas g.sel polygon,#canvas g.sel ellipse,#canvas g.sel path{stroke:var(--hit)!important;stroke-width:3.5px!important}
#canvas g.sel text{fill:var(--hit)!important;font-weight:bold}
#canvas g.down polygon,#canvas g.down ellipse,#canvas g.down path{stroke:#0b62c4!important}
#canvas.dim g.node:not(.sel):not(.down):not(.near),#canvas.dim g.edge:not(.sel):not(.down){opacity:.28}
h2{font-size:16px;margin:2px 0 2px} h3{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:16px 0 4px}
.kind{display:inline-block;font-size:12px;background:var(--chip);border-radius:10px;padding:1px 9px;color:var(--muted)}
table{border-collapse:collapse;width:100%;font-size:13px} td{padding:3px 4px;border-bottom:1px solid var(--line);vertical-align:top}
td:first-child{color:var(--muted);width:42%}
.link{color:var(--accent);cursor:pointer;text-decoration:underline;background:none;border:0;padding:0;font:inherit}
.note{font-size:12px;color:var(--muted);margin-top:10px}
footer{font-size:12px;color:var(--muted);padding:5px 14px;background:var(--panel);border-top:1px solid var(--line)}
@media (max-width:900px){main{grid-template-columns:1fr;grid-template-rows:170px 1fr 230px}#tree{border-right:0;border-bottom:1px solid var(--line)}#props{border-left:0;border-top:1px solid var(--line)}}
</style></head><body>
<header><h1>__TITLE__</h1><span class="tag">draft, auto-generated from PFD</span>
<input id="q" type="search" placeholder="Search tag, name or type, e.g. R-401, valve, TIC" aria-label="Search">
<button id="zo" aria-label="Zoom out">&minus;</button><button id="fit">Fit</button><button id="zi" aria-label="Zoom in">+</button><button id="clr">Clear</button></header>
<main><aside id="tree" aria-label="Model tree"></aside><div id="canvas">__SVG__</div>
<aside id="props" aria-label="Properties"><h2>Nothing selected</h2><p class="note">Click a symbol or line on the drawing, or pick an item from the tree, to see its properties, connections and what is downstream of it.</p></aside></main>
<footer id="foot"></footer>
<script>
const D=__DATA__;
const N=Object.fromEntries(D.nodes.map(n=>[n.id,n])), L=Object.fromEntries(D.lines.map(l=>[l.line_no,l]));
const canvas=document.getElementById('canvas'), svg=canvas.querySelector('svg'), props=document.getElementById('props');
const gNode={}, gEdge=[];
svg.querySelectorAll('g.node').forEach(g=>{const t=g.querySelector('title').textContent; gNode[t]=g; g.addEventListener('click',e=>{e.stopPropagation(); if(N[t]&&N[t].kind!=='tap')pick(t)})});
svg.querySelectorAll('g.edge').forEach(g=>{const [a,b]=g.querySelector('title').textContent.split('->'); const k=D.links.find(x=>x.source===a&&x.target===b); if(k){gEdge.push({g,k}); g.addEventListener('click',e=>{e.stopPropagation(); if(k.line)pickLine(k.line)})}});
svg.querySelectorAll('title').forEach(t=>t.remove());
const W=parseFloat(svg.getAttribute('width')), H=parseFloat(svg.getAttribute('height')); let z=1;
function zoom(f){z=Math.max(.15,Math.min(4,f)); svg.setAttribute('width',W*z+'pt'); svg.setAttribute('height',H*z+'pt')}
function fit(){zoom(Math.min(canvas.clientWidth/(W*1.34),4))}
document.getElementById('zi').onclick=()=>zoom(z*1.25); document.getElementById('zo').onclick=()=>zoom(z/1.25); document.getElementById('fit').onclick=fit;
const groups=[['Equipment',n=>n.kind==='equipment'],['Valves and fittings',n=>n.kind==='valve'],['Instruments',n=>n.kind==='instrument']];
const tree=document.getElementById('tree');
function label(n){return n.name||n.desc||''}
function buildTree(f){tree.innerHTML=''; f=(f||'').toLowerCase(); let count=0;
 const hit=s=>!f||s.toLowerCase().includes(f);
 for(const [title,test] of groups){const items=D.nodes.filter(test).filter(n=>hit(n.id+' '+label(n)+' '+(n.type||'')+' '+n.kind)); count+=items.length; add(title,items.map(n=>[n.id,label(n),()=>pick(n.id)]))}
 const ls=D.lines.filter(l=>hit(l.line_no+' '+l.description+' line '+l.service)); count+=ls.length; add('Lines',ls.map(l=>[l.line_no,'stream '+l.pfd_stream,()=>pickLine(l.line_no)]));
 return count;
 function add(title,rows){const d=document.createElement('details'); d.open=!!f||title==='Equipment'; d.innerHTML='<summary>'+title+' ('+rows.length+')</summary>';
  rows.forEach(([id,sub,fn])=>{const b=document.createElement('button'); b.className='item'; b.dataset.id=id; b.innerHTML=esc(id)+'<small>'+esc(sub.slice(0,34))+'</small>'; b.onclick=fn; d.appendChild(b)}); tree.appendChild(d)}}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function clear(){svg.querySelectorAll('.sel,.down,.near').forEach(g=>g.classList.remove('sel','down','near')); tree.querySelectorAll('.on').forEach(b=>b.classList.remove('on')); canvas.classList.remove('dim')}
function mark(id){const b=tree.querySelector('[data-id="'+CSS.escape(id)+'"]'); if(b){b.classList.add('on'); b.parentElement.open=true; b.scrollIntoView({block:'nearest'})}}
function downstream(starts){const seen=new Set(starts), q=[...starts]; while(q.length){const a=q.shift(); D.links.filter(k=>k.kind==='process'&&k.source===a).forEach(k=>{if(!seen.has(k.target)){seen.add(k.target); q.push(k.target)}})} return seen}
function row(a,b){return b===''||b==null||b!==b?'':'<tr><td>'+esc(a)+'</td><td>'+b+'</td></tr>'}
function ref(id){return N[id]&&N[id].kind==='tap'?'':'<button class="link" data-go="'+esc(id)+'">'+esc(id)+'</button>'}
function lref(l){return '<button class="link" data-line="'+esc(l)+'">'+esc(l)+'</button>'}
function wire(){props.querySelectorAll('[data-go]').forEach(b=>b.onclick=()=>pick(b.dataset.go)); props.querySelectorAll('[data-line]').forEach(b=>b.onclick=()=>pickLine(b.dataset.line))}
function show(g){const r=g.getBoundingClientRect(), c=canvas.getBoundingClientRect(); if(r.left<c.left||r.right>c.right||r.top<c.top||r.bottom>c.bottom){canvas.scrollLeft+=r.left-c.left-c.width/2+r.width/2; canvas.scrollTop+=r.top-c.top-c.height/2+r.height/2}}
function pick(id){const n=N[id]; if(!n)return; clear(); canvas.classList.add('dim'); const g=gNode[id]; if(g){g.classList.add('sel'); show(g)} mark(id);
 const ins=D.links.filter(k=>k.target===id), outs=D.links.filter(k=>k.source===id);
 const starts=n.kind==='instrument'?outs.filter(k=>k.kind!=='process').map(k=>k.target):[id];
 const ds=downstream(starts.filter(s=>D.links.some(k=>k.kind==='process'&&(k.source===s||k.target===s))));
 gEdge.forEach(e=>{if(e.k.source===id||e.k.target===id){e.g.classList.add('sel'); const o=e.k.source===id?e.k.target:e.k.source; if(gNode[o])gNode[o].classList.add('near')} else if(ds.has(e.k.source)&&e.k.kind==='process')e.g.classList.add('down')});
 ds.forEach(d=>{if(d!==id&&gNode[d])gNode[d].classList.add('down')});
 const dsEq=[...ds].filter(d=>d!==id&&N[d].kind==='equipment').sort();
 const procLines=[...new Set(ins.concat(outs).filter(k=>k.line).map(k=>k.line))];
 const sig=k=>k.kind==='trip'?'trip':'signal';
 let h='<h2>'+esc(id)+'</h2><span class="kind">'+esc(n.kind)+'</span><h3>Properties</h3><table>'+row('Description',esc(label(n)))+row('Type',esc(n.type||n.letters||''))+row('Location',esc(n.loc||''))+row('Area',n.area)+row('Duty / power',n.duty?esc(n.duty):'')+row('Installed on',n.line?lref(n.line):(n.on&&L[n.on]?lref(n.on):(n.on?ref(n.on):'')))+'</table>';
 if(procLines.length)h+='<h3>Process lines</h3><table>'+procLines.map(l=>{const x=L[l]||{}; return row(l,(x.from===id?'out to '+ref(x.to):x.to===id?'in from '+ref(x.from):'in line')+(x.flow_kgh!=null?' · '+x.flow_kgh+' kg/h':''))}).join('')+'</table>';
 const si=ins.filter(k=>k.kind!=='process'&&N[k.source].kind!=='tap'), so=outs.filter(k=>k.kind!=='process');
 if(si.length||so.length)h+='<h3>Signals</h3><table>'+si.map(k=>row(sig(k)+' from',ref(k.source))).join('')+so.map(k=>row(sig(k)+' to',ref(k.target))).join('')+'</table>';
 h+='<h3>Downstream if this changes or fails</h3>'+(dsEq.length?'<p>'+dsEq.map(ref).join(', ')+'</p>':'<p>Nothing downstream on the process side.</p>')+'<p class="note">Connectivity only. Recycle lines make most of the plant downstream of most items. Numbers need a process model.</p>';
 props.innerHTML=h; wire()}
function pickLine(l){const x=L[l]; if(!x)return; clear(); canvas.classList.add('dim'); mark(l); let first;
 gEdge.forEach(e=>{if(e.k.line===l){e.g.classList.add('sel'); first=first||e.g; [e.k.source,e.k.target].forEach(o=>gNode[o]&&gNode[o].classList.add('near'))}}); if(first)show(first);
 const inl=D.nodes.filter(n=>n.line===l||n.on===l).map(n=>ref(n.id)).join(', ');
 props.innerHTML='<h2>'+esc(l)+'</h2><span class="kind">'+esc(x.phase)+' line</span><h3>Properties</h3><table>'+row('PFD stream',esc(x.pfd_stream))+row('Description',esc(x.description))+row('Service',esc(x.service))+row('From',ref(x.from))+row('To',ref(x.to))+row('Flow',x.flow_kgh!=null?x.flow_kgh+' kg/h':'not on PFD')+row('Temperature',x.temp_C!=null?x.temp_C+' °C':'not on PFD')+row('Pressure',x.press_bar!=null?x.press_bar+' bar':'not on PFD')+row('Size',esc(x.size))+row('Volume flow',x.vol_m3h!=null?(+x.vol_m3h).toFixed(1)+' m³/h':'')+row('Density',x.density_kgm3!=null?x.density_kgm3+' kg/m³':'')+row('Enthalpy flow',x.enthalpy_kW!=null?x.enthalpy_kW+' kW (Aspen basis)':'')+row('Composition (mass)',x.composition?esc(x.composition):'')+'</table>'+(inl?'<h3>Valves and instruments on this line</h3><p>'+inl+'</p>':'')+'<p class="note">Sizes are velocity-based estimates.</p>'; wire()}
const q=document.getElementById('q'), foot=document.getElementById('foot');
const total=D.nodes.filter(n=>n.kind!=='tap').length+D.lines.length;
q.oninput=()=>{const c=buildTree(q.value); foot.textContent=q.value?c+' of '+total+' items match':base};
const cnt=k=>D.nodes.filter(n=>n.kind===k).length, base=cnt('equipment')+' equipment · '+D.lines.length+' lines · '+cnt('instrument')+' instruments · '+cnt('valve')+' valves and fittings';
document.getElementById('clr').onclick=()=>{clear(); q.value=''; buildTree(); foot.textContent=base; props.innerHTML='<h2>Nothing selected</h2><p class="note">Click a symbol or line on the drawing, or pick an item from the tree.</p>'};
canvas.addEventListener('click',()=>{clear()});
buildTree(); foot.textContent=base; fit();
</script></body></html>
"""


def render(out, title="P&ID viewer"):
    """Return the viewer page (one HTML string) for an output folder."""
    out = Path(out)
    svg = (out / "pid.svg").read_text(encoding="utf-8")
    svg = svg[svg.index("<svg"):]                       # drop the XML header
    kg = json.loads((out / "knowledge_graph.json").read_text(encoding="utf-8"))
    lines = pd.read_excel(out / "PID_deliverables.xlsx", sheet_name="Line list", dtype={"pfd_stream": str})
    lines = json.loads(lines.to_json(orient="records"))  # NaN -> null
    data = {"nodes": kg["nodes"], "links": kg["links"], "lines": lines}
    blob = json.dumps(data, default=str).replace("</", "<\\/")
    full = f"P&ID viewer - {title}"
    return PAGE.replace("__TITLE__", full.replace("&", "&amp;")).replace("__SVG__", svg).replace("__DATA__", blob)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out", help="folder holding pid.svg, knowledge_graph.json, PID_deliverables.xlsx")
    ap.add_argument("--title", default="P&ID viewer")
    args = ap.parse_args()
    out = HERE / args.out
    html = render(out, args.title)
    (out / "viewer.html").write_text(html, encoding="utf-8")
    print(f"Wrote {out / 'viewer.html'} ({len(html) // 1024} KB)")


if __name__ == "__main__":
    main()
