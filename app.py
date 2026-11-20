"""
app.py - Streamlit dashboard: PFD / Aspen tables -> digitized P&ID, BOM, knowledge graph, flow directions,
editable data and a question-answering assistant.

Run locally:   streamlit run app.py        (opens http://localhost:8501)
Deploy:        see DEPLOY.md
"""
from pathlib import Path

import networkx as nx
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import agent
import aspen_link
import balance as BAL
import bom as B
import copilot_kb as KBASE
import copilot_nlu as NLU
import engineering as ENGR
import gaps as GAPS
import twin as TWIN
import copilot as C
import dexpi_bridge
import kg_query as K
import svg2dxf
import workflow as W
import failure as fail
import make_smart_pid as smart
import pid_ai
import sfiles
import make_knowledge_graph as mk
import make_pid_sheet as sheet
import make_viewer as mv
import pfd_to_pid as p

DATA = Path(__file__).parent / "data"
st.set_page_config(page_title="PFD to P&ID", layout="wide")


def secret(name):
    try:
        return st.secrets.get(name)
    except Exception:
        return None


API_KEY = secret("ANTHROPIC_API_KEY") or agent.os.environ.get("ANTHROPIC_API_KEY")

# ================================================================ sidebar
cases = sorted(f.name for f in DATA.glob("streams*.csv"))
default = next((cases.index(c) for c in ("streams_aspen_10tpd.csv", "streams_gasifier_full.csv") if c in cases), 0)
st.sidebar.title("PFD to P&ID")
case = st.sidebar.selectbox("Case (stream table)", cases, index=default)
stem = Path(case).stem.replace("streams_", "")
name = stem.replace("_", " ")
out_name = "out_app_" + Path(case).stem
CO = sheet.company_settings()
with st.sidebar.expander("Drawing sheet and title block"):
    rows = st.number_input("Rows on the sheet", min_value=1, max_value=3, value=2)
    company = st.text_input("Company", value=CO.get("name", ""))
    drawing_no = st.text_input("Drawing no.", value=CO.get("drawing_no", "PID-001"))
    designed = st.text_input("Designed by", value=CO.get("designed", ""))
    drawn = st.text_input("Drawn by", value=CO.get("drawn", ""))
    checked = st.text_input("Checked by", value=CO.get("checked", ""))
    approved = st.text_input("Approved by", value=CO.get("approved", ""))
    with st.popover("More title block fields") if hasattr(st, "popover") else st.container():
        co_new = {k: st.text_input(k.replace("_", " ").capitalize(), value=str(CO.get(k, "")), key="co_" + k)
                  for k in ("address", "unit", "product", "sheet_size", "scale", "copyright")}
        logo_up = st.file_uploader("Company logo (SVG with viewBox)", type=["svg"])
    if st.button("Save as company standard"):
        CO.update(co_new, name=company, drawing_no=drawing_no, designed=designed, drawn=drawn, checked=checked, approved=approved)
        if logo_up is not None:
            (DATA / "company_logo.svg").write_bytes(logo_up.getvalue())
            CO["logo"] = "company_logo.svg"
        (DATA / "company.json").write_text(__import__("json").dumps(CO, indent=2), encoding="utf-8")
        st.cache_resource.clear()
        st.rerun()
user = st.sidebar.text_input("Your name (for the revision log)", value=st.session_state.get("user", ""), key="user")
st.sidebar.caption("AI key: " + ("found. Free-form questions and PFD import are on." if API_KEY else "not set. Built-in answers only."))


@st.cache_resource(show_spinner="Generating P&ID...")
def run(case, stamp, rows, company, drawing_no, designed, drawn="", checked="", approved=""):
    """Regenerates everything for one case. `stamp` changes when a data file is edited, which refreshes the cache."""
    G, eq, lines, inst, valves = p.generate(case, out_name, "LR")
    out = p.HERE / out_name
    has_drawing = (out / "pid.svg").exists()
    viewer = mv.render(out, name) if has_drawing else None
    kg_html, n_nodes, n_rel = mk.render(out, name)
    sheet_svg, smart_html, model, pfd_s, pid_s = None, None, None, "", ""
    if has_drawing:
        try:            # the drawing sheet and smart P&ID are extras; never block the rest of the app
            smart_html, model, pfd_s, pid_s = smart.smart_sheet(case, name, out, rows=int(rows), company=company, drawing_no=drawing_no, designed=designed,
                                                                    drawn=drawn, checked=checked, approved=approved)
            sheet_svg = (out / "pid_sheet.svg").read_text(encoding="utf-8")
            svg2dxf.convert(out / "pid_sheet.svg", out / "pid_sheet.dxf")
            sheet.draw_schedules(case, name, out, company=company, drawing_no=drawing_no)
            svg2dxf.convert(out / "pid_schedules.svg", out / "pid_schedules.dxf")
        except Exception as e:
            st.session_state["sheet_error"] = str(e)
    return dict(G=G, eq=eq, lines=lines, inst=inst, valves=valves, viewer=viewer, kg=kg_html, sheet=sheet_svg, smart=smart_html, model=model,
                pfd_sfiles=pfd_s, pid_sfiles=pid_s, out=out, n_nodes=n_nodes, n_rel=n_rel,
                triples=pd.read_csv(out / "knowledge_graph_triples.csv"), motors=pd.DataFrame(G.graph.get("motors", [])),
                bom=p.bom_summary(G, eq, lines, inst, valves), nodes=pd.read_csv(out / "knowledge_graph_nodes.csv"),
                eng=ENGR.all_tables(case, G, eq, lines, inst, valves),
                zones=(__import__("json").loads((out / "sheet_zones.json").read_text()) if (out / "sheet_zones.json").exists() else {}))


stamp = max(f.stat().st_mtime for f in [DATA / case, DATA / "equipment.csv", DATA / f"duties_{stem}.csv", DATA / f"engineering_{stem}.csv",
                                         DATA / f"corrections_{stem}.csv", DATA / f"reviews_{stem}.csv",
                                         DATA / "company.json", DATA / "company_logo.svg"] if f.exists())
r = run(case, stamp, rows, company, drawing_no, designed, drawn, checked, approved)
G, eq, lines = r["G"], r["eq"], r["lines"]

for label, v in [("Equipment", len(eq)), ("Lines", len(lines)), ("Instruments", len(r["inst"])), ("Valves and fittings", len(r["valves"])), ("Motors", len(r["motors"]))]:
    st.sidebar.metric(label, v)
st.sidebar.caption("Draft generated by rules. For engineering review only. Not for construction.")

st.title(f"Biomass gasification plant: {name}")
n_applied = int((W.corrections(case).status == "applied").sum())
n_open = int((W.reviews(case).status == "open").sum())
st.caption(f"Revision {W.revision_letter(case)} · {n_applied} applied correction(s) · {n_open} open remark(s) · draft for review, not for construction")
(t_pid, t_eng, t_kg, t_trace, t_corr, t_rev, t_bom, t_cop, t_bal, t_aspen, t_twin, t_gap, t_flow, t_fail, t_ai, t_hand, t_edit, t_new, t_whatif,
 t_gloss, t_dexpi, t_ask) = st.tabs(
    ["Smart P&ID", "Engineering data", "Knowledge graph", "Traceability", "Corrections", "Review remarks", "BOM creation", "Copilot",
     "Mass and energy balance", "Aspen link", "Digital twin", "Challenges and gaps", "Flow directions", "Failure what-if", "AI P&ID model",
     "Handover files", "Edit and regenerate", "New case from a PFD", "What-if: capacity", "Glossary and legend sheet", "DEXPI import", "Ask the plant"])
ASSET_IDS = sorted(r["model"]["assets"]) if r["model"] else sorted(r["nodes"].id)

# ================================================================ 1. digitized P&ID
with t_pid:
    if not r["viewer"]:
        st.warning("Graphviz is not installed, so the drawing could not be made. Install Graphviz and add it to PATH (see DEPLOY.md).")
    else:
        view = st.radio("View", ["Smart P&ID (click any item)", "Drawing sheet only", "Schedules sheet (set points, fail positions, nozzles)",
                                 "Clickable layout viewer"], horizontal=True)
        if view.startswith("Smart") and r["smart"]:
            st.caption("Click equipment, a line, a valve, an instrument or a motor: size, nominal bore, pipe class, flow direction, flow, velocity, "
                       "conditions, composition, connections and what happens if it fails.")
            c1, c2, c3 = st.columns(3)
            c1.download_button("Smart P&ID (HTML, works offline)", r["smart"], f"PID_smart_{stem}.html", "text/html")
            if (r["out"] / "pid_sheet.dxf").exists():
                c2.download_button("Drawing for AutoCAD (DXF, layered)", (r["out"] / "pid_sheet.dxf").read_bytes(), f"PID_{stem}.dxf", "application/dxf")
            leg = p.HERE / "symbols" / "legend_sheet.dxf"
            if leg.exists():
                c3.download_button("Legend sheet for AutoCAD (DXF)", leg.read_bytes(), "PID_legend_sheet.dxf", "application/dxf")
            components.html(r["smart"], height=950, scrolling=False)
        elif view.startswith("Schedules") and (r["out"] / "pid_schedules.svg").exists():
            sv = (r["out"] / "pid_schedules.svg").read_text(encoding="utf-8")
            c1, c2, c3 = st.columns(3)
            c1.download_button("Schedules sheet (HTML, print to PDF)", (r["out"] / "pid_schedules.html").read_text(encoding="utf-8"), f"PID_schedules_{stem}.html", "text/html")
            if (r["out"] / "pid_schedules.dxf").exists():
                c2.download_button("Schedules sheet (DXF)", (r["out"] / "pid_schedules.dxf").read_bytes(), f"PID_schedules_{stem}.dxf", "application/dxf")
            c3.download_button("Schedules (Excel)", (r["out"] / "engineering_schedules.xlsx").read_bytes(), f"engineering_schedules_{stem}.xlsx")
            sv = sv.replace("<svg ", "<svg style='width:100%;height:auto' ", 1)
            components.html(f"<div style='background:#fff'>{sv}</div>", height=950, scrolling=True)
        elif view.startswith("Drawing") and r["sheet"]:
            c1, c2 = st.columns(2)
            c1.download_button("Download drawing (SVG, editable)", r["sheet"], f"PID_{stem}.svg", "image/svg+xml")
            c2.download_button("Download drawing (HTML, print to PDF)", (r["out"] / "pid_sheet.html").read_text(encoding="utf-8"), f"PID_{stem}.html", "text/html")
            inline = r["sheet"].replace("<svg ", "<svg style='width:100%;height:auto' ", 1)
            components.html(f"<div style='background:#fff'>{inline}</div>", height=950, scrolling=True)
        else:
            components.html(r["viewer"], height=780, scrolling=False)

# ================================================================ 2. BOM creation
with t_bom:
    st.caption("Quantities and tags come from the current P&ID and update with every correction. Enter pipe lengths and unit costs here; "
               "they are kept in data/ and survive regeneration. The Excel file keeps the formulas.")
    with st.expander("Pipe lengths (m) per line - from the layout or an isometric estimate", expanded=False):
        pl = B.pipe_lengths(case, lines)
        pl_ed = st.data_editor(pl, hide_index=True, disabled=["line_no", "pfd_stream", "size", "service"], key="pl_" + case)
        if st.button("Save pipe lengths"):
            B.save_pipe_lengths(case, pl_ed, user)
            st.success("Saved.")
            st.rerun()
    bom = B.create(case, G, eq, lines, r["inst"], r["valves"], r["zones"])
    k = st.columns(6)
    for col, cat in zip(k, ["Equipment", "Valve / fitting", "Instrument", "Motor", "Piping"]):
        col.metric(cat, f"{bom[bom.category == cat].qty.sum():g}")
    k[5].metric("Priced total", f"{bom.total.sum():,.0f} {bom.currency.iloc[0] if len(bom) else ''}")
    pick = st.multiselect("Categories", sorted(bom.category.unique()), default=sorted(bom.category.unique()))
    view = bom[bom.category.isin(pick)]
    ed = st.data_editor(view, hide_index=True, key="bom_" + case,
                        disabled=[c for c in bom.columns if c not in B.EDIT_COLS],
                        column_config={"unit_cost": st.column_config.NumberColumn("unit cost", min_value=0.0), "total": st.column_config.NumberColumn("total", format="%.0f")})
    c1, c2, c3 = st.columns(3)
    if c1.button("Save BOM edits"):
        full = bom.set_index("pos")
        full.update(ed.set_index("pos")[B.EDIT_COLS])
        B.save(case, full.reset_index(), user)
        st.success("Saved.")
        st.rerun()
    xl = r["out"] / f"BOM_{stem}.xlsx"
    B.to_excel(bom, xl, f"Bill of material - {name} - revision {W.revision_letter(case)}")
    c2.download_button("Download BOM (Excel, with formulas)", xl.read_bytes(), f"BOM_{stem}.xlsx")
    c3.download_button("Download BOM (CSV)", bom.to_csv(index=False), f"BOM_{stem}.csv", "text/csv")
    st.caption("Not included until there is a layout: fittings (elbows, tees, reducers), flanges, bolts and gaskets, supports, insulation quantities.")
    for title, df in [("Equipment list", eq), ("Line list", lines), ("Valve list", r["valves"]), ("Instrument index", r["inst"]), ("Motor list", r["motors"])]:
        with st.expander(f"{title} ({len(df)})"):
            st.dataframe(df, hide_index=True)

# ================================================================ 3. knowledge graph
with t_kg:
    st.caption(f"{r['n_nodes']} nodes, {r['n_rel']} relations. Click a node for its properties and relations; use the search box and the relation switches.")
    components.html(r["kg"], height=780, scrolling=False)
    c1, c2 = st.columns(2)
    c1.download_button("Download relations (CSV)", r["triples"].to_csv(index=False), "knowledge_graph_triples.csv", "text/csv")
    c2.download_button("Download nodes (CSV)", (r["out"] / "knowledge_graph_nodes.csv").read_text(encoding="utf-8"), "knowledge_graph_nodes.csv", "text/csv")
    with st.expander("Query the relations table"):
        rel = st.multiselect("Relation", sorted(r["triples"].relation.unique()), default=sorted(r["triples"].relation.unique()))
        txt = st.text_input("Tag contains", value="", key="kgq")
        t = r["triples"][r["triples"].relation.isin(rel)]
        if txt:
            t = t[t.subject.str.contains(txt, case=False, regex=False) | t.object.str.contains(txt, case=False, regex=False)]
        st.dataframe(t, hide_index=True)

    st.subheader("Explore and query")
    Gk = K.graph(r["nodes"], r["triples"])
    q1, q2 = st.tabs(["Pattern query", "Neighbours and paths"])
    with q1:
        pat = st.text_input("Pattern", value="(a:instrument)-[TRIPS]->(b:valve)", key="kgpat",
                            help="(a:kind)-[RELATION]->(b:kind) WHERE a.prop = value. Kinds: equipment, line, valve, instrument, motor.")
        st.caption("Try: `(a)-[INSTALLED_ON]->(b:line) WHERE b.type = SG` · `(a:equipment)-[FEEDS]->(b) WHERE a.area = 400` · `(a:motor)-[DRIVES]->(b)`")
        try:
            st.dataframe(K.run(pat, r["nodes"], r["triples"]), hide_index=True)
        except ValueError as e:
            st.warning(str(e))
    with q2:
        c1, c2 = st.columns([3, 1])
        who = c1.selectbox("Item", sorted(Gk.nodes), key="kgn")
        hops = c2.number_input("Hops", 1, 4, 1)
        st.dataframe(K.neighbours(Gk, who, int(hops)), hide_index=True)
        c1, c2 = st.columns(2)
        pa = c1.selectbox("Path from", sorted(Gk.nodes), key="kpa")
        pb = c2.selectbox("to", sorted(Gk.nodes), index=len(Gk.nodes) - 1, key="kpb")
        try:
            st.dataframe(K.path(Gk, pa, pb), hide_index=True)
        except Exception:
            try:
                st.caption("No path in flow/signal direction; showing the connection ignoring direction.")
                st.dataframe(K.path(Gk, pa, pb, directed=False), hide_index=True)
            except Exception:
                st.info("Not connected.")
    st.subheader("Reuse the graph in other tools")
    c1, c2, c3 = st.columns(3)
    c1.download_button("Neo4j script (Cypher)", K.to_cypher(r["nodes"], r["triples"]), f"plant_{stem}.cypher", "text/plain")
    gm = r["out"] / "pid_graph.graphml"
    if gm.exists():
        c2.download_button("P&ID graph (GraphML)", gm.read_bytes(), f"pid_{stem}.graphml", "application/xml")
    if (r["out"] / "pid_model.json").exists():
        c3.download_button("Full model (JSON)", (r["out"] / "pid_model.json").read_bytes(), f"pid_model_{stem}.json", "application/json")

# ================================================================ traceability
with t_trace:
    st.caption("For every item: where it is on the drawing, which rule or data row created it, what corrections changed it and what reviewers said.")
    A = r["model"]["assets"] if r["model"] else {}
    reg = pd.DataFrame([dict(id=k, kind=a["kind"], description=a.get("title") or a.get("label") or a.get("description", ""), zone=a.get("zone", ""),
                             source=a.get("source", ""), corrections=len(a.get("corrections", [])), remarks=len(a.get("remarks", [])))
                        for k, a in A.items()])
    if len(reg):
        f1, f2 = st.columns(2)
        kinds = f1.multiselect("Kind", sorted(reg.kind.unique()), default=sorted(reg.kind.unique()), key="trk")
        only = f2.checkbox("Only items with corrections or remarks")
        v = reg[reg.kind.isin(kinds)]
        if only:
            v = v[(v.corrections > 0) | (v.remarks > 0)]
        st.dataframe(v, hide_index=True)
        st.download_button("Download traceability register (CSV)", reg.to_csv(index=False), f"traceability_{stem}.csv", "text/csv")
        who = st.selectbox("Trace one item", sorted(A), key="trone")
        a = A[who]
        c1, c2 = st.columns(2)
        with c1:
            st.markdown(f"**{who}** · {a['kind']} · zone **{a.get('zone') or '-'}**")
            st.write("Created by:", a.get("source") or "-")
            if a.get("size_source"):
                st.write("Size from:", a["size_source"])
            st.json({k: v_ for k, v_ in a.items() if k not in ("corrections", "remarks", "source", "size_source") and v_ not in (None, "", [])}, expanded=False)
        with c2:
            st.write("Corrections")
            st.dataframe(pd.DataFrame(a.get("corrections", [])), hide_index=True)
            st.write("Review remarks")
            st.dataframe(pd.DataFrame(a.get("remarks", [])), hide_index=True)
            st.write("Relations")
            st.dataframe(pd.DataFrame([x for x in r["model"]["relations"] if who in (x["subject"], x["object"])]), hide_index=True)
    st.subheader("Revision log")
    st.dataframe(W.revisions(case).iloc[::-1], hide_index=True)

# ================================================================ corrections
with t_corr:
    st.caption("Corrections replace manual redrafting: each one is stored with who, when and why, and applied every time the P&ID is regenerated. "
               "Tag numbers stay stable. Only 'applied' rows change the drawing; the copilot can only 'propose'.")
    with st.form("newcorr", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        act = c1.selectbox("Action", list(W.ACTIONS), format_func=lambda x: f"{x} (target: {W.ACTIONS[x]})")
        tgt = c2.text_input("Target", placeholder="stream no., equipment tag or valve tag")
        val = c3.text_input("New value", placeholder='e.g. ISO;TI  /  TI;PI;PSV  /  8"  /  FC')
        why = st.text_input("Reason")
        status = st.radio("Status", ["applied", "proposed"], horizontal=True)
        if st.form_submit_button("Add correction"):
            if not (tgt and val and why):
                st.error("Target, value and reason are all needed.")
            else:
                cid = W.add_correction(case, act, tgt.strip(), val.strip(), why, user, status)
                st.success(f"{cid} added. Regenerating.")
                st.rerun()
    st.caption("Codes for in-line items: " + ", ".join(sorted(p.HINTS)))
    cdf = W.corrections(case)
    if len(cdf):
        prop = cdf[cdf.status == "proposed"]
        if len(prop):
            st.warning(f"{len(prop)} proposed correction(s) wait for approval.")
            for row in prop.itertuples():
                c1, c2, c3 = st.columns([6, 1, 1])
                c1.write(f"**{row.id}** {row.action} **{row.target}** = `{row.value}` - {row.reason} ({row.by}, {row.date})")
                if c2.button("Approve", key="ap" + row.id):
                    W.set_correction_status(case, row.id, "applied", user)
                    st.rerun()
                if c3.button("Reject", key="rj" + row.id):
                    W.set_correction_status(case, row.id, "rejected", user)
                    st.rerun()
        ced = st.data_editor(cdf, hide_index=True, key="corr_" + case, disabled=["id", "date", "by"],
                             column_config={"status": st.column_config.SelectboxColumn(options=["applied", "proposed", "rejected"]),
                                            "action": st.column_config.SelectboxColumn(options=list(W.ACTIONS))})
        if st.button("Save table changes"):
            W.save_corrections(case, ced, user)
            st.rerun()
    else:
        st.info("No corrections yet.")

# ================================================================ review remarks
with t_rev:
    st.caption("Remarks are pinned to an item and drawn on the sheet as a numbered cloud flag until closed. Closed remarks keep their response for audit.")
    with st.form("newrev", clear_on_submit=True):
        c1, c2 = st.columns([2, 1])
        tgt = c1.selectbox("Item", ASSET_IDS)
        cat = c2.selectbox("Category", W.CATEGORIES)
        txt = st.text_area("Remark")
        if st.form_submit_button("Add remark") and txt:
            rid = W.add_review(case, tgt, txt, user, cat)
            st.success(f"{rid} added.")
            st.rerun()
    rv = W.reviews(case)
    show = st.radio("Show", ["open", "all"], horizontal=True)
    rshow = rv[rv.status == "open"] if show == "open" else rv
    st.dataframe(rshow, hide_index=True)
    if len(rv[rv.status == "open"]):
        with st.form("closerev"):
            c1, c2 = st.columns([1, 3])
            rid = c1.selectbox("Remark", list(rv[rv.status == "open"].id))
            resp = c2.text_input("Response")
            how = st.radio("Close as", ["closed", "rejected"], horizontal=True)
            if st.form_submit_button("Close remark") and resp:
                W.close_review(case, rid, resp, user, how)
                st.rerun()
    st.download_button("Download remarks (CSV)", rv.to_csv(index=False), f"review_remarks_{stem}.csv", "text/csv")

# ================================================================ copilot
with t_cop:
    st.caption("The copilot uses tools: knowledge-graph queries, tracing, paths, failure what-if, AI model suggestions, BOM, remarks and correction proposals. "
               "It never changes the drawing itself: corrections arrive as 'proposed' for an engineer to approve. "
               + ("AI key found: ask in plain words." if API_KEY else "No AI key: use the commands below."))
    if r["model"]:
        @st.cache_resource(show_spinner="Loading copilot knowledge...")
        def get_copilot(case_, stamp_, kb_stamp_):
            return C.Copilot(case_, r["model"], G, eq, lines, r["inst"], r["valves"], r["nodes"], r["triples"])
        kbf = KBASE.KB_DIR / "chunks.json"
        cp = get_copilot(case, stamp, kbf.stat().st_mtime if kbf.exists() else 0)
        ck = "copilot_" + case
        hist = st.session_state.setdefault(ck, [])
        for m in hist:
            with st.chat_message(m["role"]):
                st.markdown(m["content"])
                for name_, args_, res_ in m.get("steps", []):
                    with st.expander(f"tool: {name_} {args_}"):
                        st.json(res_, expanded=False)
        with st.form("cop", clear_on_submit=True):
            qq = st.text_input("Ask or command", placeholder="what if FV-4021 fails closed")
            go = st.form_submit_button("Send")
        with st.expander("Commands"):
            st.code(C.HELP, language=None)
        if go and qq:
            with st.spinner("Working..."):
                try:
                    ans, steps = cp.ask(qq, [dict(role=m["role"], content=m["content"]) for m in hist][-10:], API_KEY)
                except Exception as e:
                    ans, steps = cp.command(qq)
                    ans = f"AI call failed ({e}). Command mode: " + ans
            hist += [dict(role="user", content=qq), dict(role="assistant", content=ans, steps=steps)]
            st.rerun()
        if st.button("Clear copilot conversation"):
            st.session_state[ck] = []
            st.rerun()
        st.divider()
        k1, k2 = st.columns(2)
        with k1:
            st.subheader("Train the copilot")
            card = cp.nlu().card
            st.caption(f"Intent model: {card.get('train_examples', '?')} training questions ({card.get('user_examples', 0)} added by people); "
                       f"accuracy on question patterns it never saw: {card.get('held_out_accuracy', '?')}.")
            with st.form("teach", clear_on_submit=True):
                tq = st.text_input("A question the copilot got wrong")
                ti = st.selectbox("What it should do", sorted(NLU.TEMPLATES))
                if st.form_submit_button("Add example and retrain") and tq:
                    NLU.add_example(tq, ti)
                    cp._nlu = NLU.train(list(r["model"]["assets"]))
                    st.success("Retrained.")
            ex = NLU.examples()
            if len(ex):
                st.dataframe(ex, hide_index=True)
        with k2:
            st.subheader("Knowledge base")
            docs = KBASE.user_docs()
            srcs = sorted({d["source"] for d in docs})
            st.caption(f"{len(KBASE.NOTES)} built-in engineering notes, this case's engineering tables, the Aspen review, and {len(srcs)} uploaded document(s).")
            ups = st.file_uploader("Add papers, manuals, HAZOP reports (PDF, TXT, MD)", type=["pdf", "txt", "md"], accept_multiple_files=True, key="kbup")
            if ups and st.button("Add to knowledge base"):
                n = sum(KBASE.add_document(u.name, u.getvalue()) for u in ups)
                st.success(f"{n} passages added.")
                st.rerun()
            for sname in srcs:
                c1_, c2_ = st.columns([4, 1])
                c1_.write(sname)
                if c2_.button("Remove", key="rm_" + sname):
                    KBASE.remove_document(sname)
                    st.rerun()
            kq = st.text_input("Search the knowledge base", key="kbq")
            if kq:
                for h in cp.kb.search(kq, k=5):
                    st.markdown(f"**{h['title']}** ({h['source']}, score {h['score']}): {h['text'][:500]}")

# ================================================================ engineering data
with t_eng:
    E_ = r["eng"]
    st.caption("Data the PFD does not give, proposed by rules from the Aspen streams: pipe class and MOC, insulation, design and test conditions, "
               "end connections (flanged / socket-weld / threaded), valve fail positions, set points and alarms, nozzles, drains and vents. "
               "Change any value below; it is kept in data/engineering_<case>.csv, shown on the drawing and logged.")
    st.info("Line number = " + ENGR.LINE_NOMENCLATURE)
    which = st.radio("Table", ["Line designation", "Valve fail positions", "Set points and alarms", "Nozzle schedule", "Drains and vents",
                                 "Pipe classes / MOC", "Abbreviations"], horizontal=True)
    key_ = {"Line designation": ("line_designation", "line_no"), "Valve fail positions": ("valve_schedule", "tag"), "Set points and alarms": ("setpoints", "tag"),
            "Nozzle schedule": ("nozzles", "equipment"), "Drains and vents": ("drains_vents", "item"), "Pipe classes / MOC": ("pipe_classes", None),
            "Abbreviations": ("abbreviations", None)}[which]
    df_ = E_[key_[0]]
    if key_[1] and key_[0] in ("line_designation", "valve_schedule", "setpoints"):
        ed_ = st.data_editor(df_, hide_index=True, key="eng_" + key_[0] + case, disabled=[key_[1], "set_by"])
        why_ = st.text_input("Reason for the change", key="engwhy")
        if st.button("Save changes"):
            n_ = 0
            for i_ in range(len(df_)):
                for col_ in df_.columns:
                    a_, b_ = df_.iloc[i_][col_], ed_.iloc[i_][col_]
                    if str(a_) != str(b_) and not (pd.isna(a_) and pd.isna(b_)):
                        ENGR.set_override(case, str(df_.iloc[i_][key_[1]]), col_, b_, why_ or "edited in app", user)
                        n_ += 1
            st.success(f"{n_} value(s) saved.")
            st.cache_resource.clear()
            st.rerun()
    else:
        st.dataframe(df_, hide_index=True)
    ov_ = ENGR.overrides(case)
    if len(ov_):
        with st.expander(f"Changed values ({len(ov_)})"):
            prop_ = ov_[ov_.status == "proposed"]
            for i_, r_ in prop_.iterrows():
                c1_, c2_ = st.columns([5, 1])
                c1_.write(f"Proposed: {r_['item']} {r_['field']} = {r_['value']} ({r_['reason']})")
                if c2_.button("Approve", key=f"apv{i_}"):
                    ENGR.set_override(case, r_["item"], r_["field"], r_["value"], r_["reason"], user, "applied")
                    st.cache_resource.clear()
                    st.rerun()
            st.dataframe(ov_, hide_index=True)
    c1_, c2_ = st.columns(2)
    xl_ = r["out"] / "engineering_schedules.xlsx"
    ENGR.to_excel(E_, xl_)
    c1_.download_button("All engineering tables (Excel)", xl_.read_bytes(), f"engineering_{stem}.xlsx")
    if API_KEY and c2_.button("Ask AI to propose missing set points (as proposals)"):
        try:
            st.success(f"{ENGR.propose_with_ai(case, E_, API_KEY)} proposal(s) added; approve them above.")
        except Exception as e:
            st.error(f"AI proposal failed: {e}")

# ================================================================ aspen link
with t_aspen:
    st.caption("Bring new Aspen Plus results in, see what changed, and rebuild the P&ID from them.")
    old_ = BAL.results(stem)
    st.write(f"Current results: **{'data/' + stem + '_stream_results.csv' if old_ is not None else 'none for this case'}**"
             + (f", {len(old_)} streams" if old_ is not None else ""))
    m1, m2, m3 = st.tabs(["Upload HMBD workbook", "Upload stream summary CSV", "Live link to Aspen Plus (Windows PC)"])
    with m1:
        upw = st.file_uploader("Aspen HMBD workbook (.xlsx, same layout as data/aspen_10tpd_hmbd.xlsx)", type=["xlsx"], key="hmbd")
        if upw is not None and st.button("Import workbook and rebuild"):
            (DATA / "aspen_10tpd_hmbd.xlsx").write_bytes(upw.getvalue())
            import subprocess, sys
            res_ = subprocess.run([sys.executable, str(p.HERE / "import_aspen.py")], capture_output=True, text=True)
            st.code(res_.stdout[-2000:] + res_.stderr[-2000:])
            W.log(case, user, "Aspen results imported", upw.name)
            st.cache_resource.clear()
    with m2:
        upc = st.file_uploader("Aspen 'Stream Summary' exported as CSV", type=["csv"], key="sscsv")
        if upc is not None:
            tmp_ = r["out"] / "upload_summary.csv"
            tmp_.write_bytes(upc.getvalue())
            new_ = aspen_link.read_summary_csv(tmp_)
            st.dataframe(new_, hide_index=True)
            if old_ is not None:
                ch_ = aspen_link.compare(old_, new_)
                st.write(f"Changes against the current results: {len(ch_)}")
                st.dataframe(ch_, hide_index=True)
            if st.button("Save as current results"):
                aspen_link.save(new_, stem)
                W.log(case, user, "Aspen stream results replaced", upc.name)
                st.success("Saved. Map new streams in data/aspen_map_*.csv and run import_aspen.py to rebuild the stream table.")
    with m3:
        st.write("On the engineer's PC with Aspen Plus installed:")
        st.code(f'pip install pywin32\npython aspen_link.py --bkp "C:\\models\\10TPD_gasifier.bkp" --case {stem} --run', language="bash")
        st.caption("This opens the model through the Aspen Plus COM interface, runs it, reads every stream and writes the results CSV. "
                   "Commit the CSV to GitHub (or share the folder) and the app picks it up. Not available on Streamlit Cloud.")
        inbox_ = aspen_link.watch_folder()
        st.write("Files waiting in data/aspen_inbox/:", inbox_ or "none")

# ================================================================ digital twin
with t_twin:
    st.caption("The smart P&ID as a digital-twin input: asset hierarchy, lines with engineering data, signal list with Aspen baseline, set points, "
               "alarms and OPC UA-style node ids, relations, and live rules that ask operators the gap-bridging questions.")
    doc_, sig_ = TWIN.twin_model(case, r["model"] or {}, G, eq, lines, r["inst"], r["valves"], BAL.results(stem))
    c1_, c2_ = st.columns(2)
    c1_.download_button("Digital twin model (JSON)", __import__("json").dumps(doc_, indent=1, default=str), f"twin_{stem}.json", "application/json")
    c2_.download_button("Signal list for PLC / historian (CSV)", sig_.to_csv(index=False), f"signals_{stem}.csv", "text/csv")
    st.dataframe(sig_, hide_index=True)
    st.subheader("Live check against the Aspen baseline")
    live_ = TWIN.read_live()
    vals_ = {}
    cols_ = st.columns(4)
    for i_, (tg_, lab_, un_, lo_, hi_, base_) in enumerate(TWIN.LIVE_POINTS):
        vals_[tg_] = (live_ or {}).get(tg_) if live_ else cols_[i_ % 4].slider(f"{lab_} ({un_})", float(lo_), float(hi_), float(base_), key="tw" + tg_)
    st.caption("No live feed connected: values come from the sliders. Connect read_live() in twin.py to OPC UA / MQTT / historian.")
    al_ = TWIN.evaluate(vals_)
    for a_ in al_:
        (st.error if a_["rule"].startswith("SAFE") else st.warning)(f"**{a_['rule']}** {a_['condition']}. {a_['question']}")
    if not al_:
        st.success("All values are within the rules around the Aspen baseline.")

# ================================================================ challenges and gaps
with t_gap:
    st.caption("What is still open before this P&ID can be issued, and what the tools cannot do yet.")
    try:
        dis_ = None
        mp_ = pid_ai.MODEL_DIR / "pid_model.npz"
        if mp_.exists():
            P_ = pid_ai.PIDModel.load(mp_).predict(eq, pd.read_csv(DATA / case, dtype={"no": str}).fillna({"pid_hints": ""}))
            dis_ = int((~P_.agrees).sum())
    except Exception:
        dis_ = None
    gp_ = GAPS.table(case, G, r["eng"], dis_)
    k_ = st.columns(3)
    for col_, sev_ in zip(k_, ["high", "medium", "low"]):
        col_.metric(f"{sev_.capitalize()} priority", int((gp_.severity == sev_).sum()))
    st.dataframe(gp_, hide_index=True)
    st.download_button("Download gaps (CSV)", gp_.to_csv(index=False), f"gaps_{stem}.csv", "text/csv")

# ================================================================ 4. flow directions
with t_flow:
    E = nx.MultiDiGraph()
    E.add_nodes_from(eq.tag)
    for x in lines.itertuples():
        E.add_edge(x[5], x.to, line=x.line_no)          # x[5] is the 'from' column
    st.subheader("Every line, in flow direction")
    flow = pd.DataFrame({"from": lines["from"], "→": "→", "to": lines.to, "line": lines.line_no, "stream": lines.pfd_stream, "service": lines.service,
                         "phase": lines.phase, "kg/h": lines.flow_kgh, "°C": lines.temp_C})
    st.dataframe(flow, hide_index=True)

    st.subheader("Process sequence")
    sources = [n for n in E.nodes if E.in_degree(n) == 0] or list(E.nodes)[:1]
    depth = {}
    for s0 in sources:
        for n, d in nx.single_source_shortest_path_length(E, s0).items():
            depth[n] = min(d, depth.get(n, 10 ** 6))
    for d in sorted(set(depth.values())):
        st.write(f"**Step {d + 1}:** " + ", ".join(f"{n} ({eq.set_index('tag').name[n]})" for n in sorted(k for k, v in depth.items() if v == d)))
    st.caption("Steps count the number of lines from a feed point. Items on the same step are in parallel or are separate feeds.")

    st.subheader("Trace a path")
    c1, c2 = st.columns(2)
    tags = list(eq.tag)
    a = c1.selectbox("From", tags, index=0)
    b = c2.selectbox("To", tags, index=len(tags) - 1)
    try:
        path = nx.shortest_path(E, a, b)
        hops = [f"**{path[0]}**"]
        for u, v in zip(path, path[1:]):
            hops.append(f"—[{list(E.get_edge_data(u, v).values())[0]['line']}]→ **{v}**")
        st.markdown(" ".join(hops))
    except nx.NetworkXNoPath:
        st.info(f"No flow path from {a} to {b}. Try the other direction.")

    st.subheader("Upstream and downstream of one asset")
    assets = sorted(n for n, d in G.nodes(data=True) if d["kind"] != "tap")
    tag = st.selectbox("Asset (equipment, valve or instrument)", assets)
    st.code(p.impact(G, tag))

# ================================================================ 5. ask the plant
with t_ask:
    st.caption("Answers come from the knowledge graph, line list, energy balance and tag glossary of the selected case. "
               "Connectivity-based, not a simulation. The P&ID is a draft that has not been through HAZOP.")
    st.caption("Built-in answers (no AI key): counts, what feeds X, downstream of X, what trips or controls X, a tag on its own, energy figures. "
               "With an AI key: free-form questions.")
    examples = ["How many control valves are there?", "What feeds E-402?", "What is downstream of R-401?", "What trips XV-4022?",
                "What if FV-4021 fails?", "What is the cold gas efficiency?"]
    chat_key = "chat_" + case
    history = st.session_state.setdefault(chat_key, [])
    for m in history:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
    with st.form("ask", clear_on_submit=True):
        question = st.text_input("Question", placeholder=examples[0])
        sent = st.form_submit_button("Ask")
    st.caption("Examples: " + " · ".join(examples))
    if sent and question:
        with st.spinner("Working..."):
            try:
                answer = agent.ask(question, r["out"], history, API_KEY, case=case)
            except Exception as e:      # network / key / model errors
                answer = f"The AI call failed ({e}). Built-in answer: " + (agent.quick_answer(question, r["out"], case) or agent.HELP)
        history += [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
        st.rerun()
    if st.button("Clear conversation"):
        st.session_state[chat_key] = []
        st.rerun()

# ================================================================ 6. edit and regenerate
with t_edit:
    st.write(f"Editing `data/{case}`. Change a flow, temperature, connection or the `pid_hints` column, then press Save. "
             "The P&ID, BOM, knowledge graph and lists regenerate.")
    st.caption("pid_hints codes, separated by ';': " + ", ".join(sorted(p.HINTS)))
    table = pd.read_csv(DATA / case, dtype={"no": str})
    edited = st.data_editor(table, num_rows="dynamic", hide_index=True, key="editor_" + case)
    eq_all = pd.read_csv(DATA / "equipment.csv")
    with st.expander("Equipment table (data/equipment.csv): add equipment here before using its tag in a stream"):
        st.caption("type must be one of: " + ", ".join(sorted(p.EQUIPMENT_RULES)))
        eq_edit = st.data_editor(eq_all, num_rows="dynamic", hide_index=True, key="eq_editor")
    if st.button("Save and regenerate"):
        problems = agent.check_tables(eq_edit.dropna(subset=["tag"]), edited.dropna(subset=["from", "to"]).fillna({"pid_hints": ""}))
        if problems:
            for m in problems:
                st.error(m)
        else:
            eq_edit.dropna(subset=["tag"]).to_csv(DATA / "equipment.csv", index=False)
            edited.dropna(subset=["from", "to"]).to_csv(DATA / case, index=False)
            st.cache_resource.clear()
            st.rerun()
    st.caption("On Streamlit Community Cloud, saved edits last until the app restarts. Commit the CSV files to GitHub to keep them.")

# ================================================================ 7. new case from a PFD
with t_new:
    st.write("Upload a PFD (image or PDF). An AI model reads it into an equipment table and a stream table. "
             "Check and correct both tables, then save them as a new case; the P&ID is generated from the saved tables.")
    st.caption("The AI can misread a drawing. Nothing is generated until you have reviewed the tables.")
    if not API_KEY:
        st.info("This step needs an AI key (ANTHROPIC_API_KEY). Without one, create a case by copying a streams CSV in the data folder and editing it in the previous tab.")
    up = st.file_uploader("PFD file", type=["png", "jpg", "jpeg", "pdf"], key="pfd_up")
    if up is not None and API_KEY and st.button("Read the PFD"):
        media = "application/pdf" if up.name.lower().endswith(".pdf") else ("image/png" if up.name.lower().endswith(".png") else "image/jpeg")
        with st.spinner("Reading the drawing..."):
            try:
                st.session_state["new_eq"], st.session_state["new_st"], _ = agent.extract_pfd(up.getvalue(), media, API_KEY)
            except Exception as e:
                st.error(f"Could not read the drawing: {e}")
    if "new_eq" in st.session_state:
        st.subheader("Equipment (review)")
        new_eq = st.data_editor(st.session_state["new_eq"], num_rows="dynamic", hide_index=True, key="new_eq_editor")
        st.subheader("Streams (review)")
        new_st = st.data_editor(st.session_state["new_st"], num_rows="dynamic", hide_index=True, key="new_st_editor")
        new_name = st.text_input("Name for the new case (letters, digits, underscore)", value="new_plant")
        if st.button("Save as new case"):
            all_eq = pd.concat([pd.read_csv(DATA / "equipment.csv"), new_eq]).drop_duplicates("tag", keep="last")
            problems = agent.check_tables(all_eq, new_st.fillna({"pid_hints": ""}))
            safe = "".join(ch for ch in new_name if ch.isalnum() or ch == "_")
            if problems or not safe:
                for m in problems or ["Give the case a name."]:
                    st.error(m)
            else:
                all_eq.to_csv(DATA / "equipment.csv", index=False)
                new_st.to_csv(DATA / f"streams_{safe}.csv", index=False)
                st.cache_resource.clear()
                st.success(f"Saved as streams_{safe}.csv. Pick it in the sidebar.")

# ================================================================ 8. mass and energy balance
with t_bal:
    R_ = BAL.results(stem)
    if R_ is not None:
        st.subheader("Aspen model check: every block")
        bc_ = BAL.block_closure(R_)
        st.dataframe(bc_, hide_index=True)
        st.caption(f"Worst mass error {bc_.mass_error_pct.abs().max():.4f} %. Element (C, H, O, N) errors are in %; duty = enthalpy out - in. "
                   "Dryer and decomposer handle the biomass as a non-conventional solid, so their element balance needs the ultimate analysis.")
        st.dataframe(BAL.mass_balance_table(R_), hide_index=True)
        st.subheader("Corrected estimate for a real two-stage downdraft gasifier")
        a1, a2, a3, a4 = st.columns(4)
        cc_ = a1.slider("Char conversion (%)", 85, 100, 95) / 100
        hl_ = a2.slider("Wall heat loss (% of biomass LHV)", 0.0, 15.0, 4.0, 0.5)
        tar_ = a3.slider("Tar after char bed (mg/Nm3)", 0, 500, 50, 10)
        lam_ = a4.slider("Engine lambda", 1.0, 3.0, 1.6, 0.1)
        b1, b2, b3 = st.columns(3)
        se_ = b1.slider("Engine shaft efficiency (% of syngas LHV)", 20, 40, 30) / 100
        ge_ = b2.slider("Generator efficiency (%)", 90, 98, 95) / 100
        pl_ = b3.number_input("Parasitic load (kWe)", 0.0, 200.0, 45.0)
        enf_ = DATA / f"{stem}_energy.csv"
        cor_ = BAL.corrected_case(R_, pd.read_csv(enf_) if enf_.exists() else None, cc_, hl_, tar_, lam_, se_, ge_, pl_)
        st.dataframe(cor_, hide_index=True)
        st.caption("Estimate from the Aspen results and the assumptions above, to set expectations and margins. Re-run Aspen with the "
                   "corrections listed in the feedback workbook for design numbers.")
        fbp_ = r["out"] / f"aspen_feedback_{stem}.xlsx"
        BAL.feedback_workbook(stem, fbp_, char_conversion=cc_, heat_loss_pct=hl_, tar_mg_nm3=tar_, engine_lambda=lam_, shaft_eff=se_, gen_eff=ge_, parasitic_kW=pl_)
        st.download_button("Feedback workbook for the Aspen engineer (kinetics, thermodynamics, data requests)", fbp_.read_bytes(), fbp_.name)
        with st.expander("What the Aspen model idealises and how to correct it"):
            st.dataframe(pd.DataFrame(BAL.IDEALISATIONS, columns=["area", "Aspen model now", "real plant", "recommended correction"]), hide_index=True)
    f_energy = DATA / f"{stem}_energy.csv"
    st.subheader("Mass balance per equipment")
    bal = []
    for tg in eq.tag:
        i, o = lines[lines.to == tg].flow_kgh, lines[lines["from"] == tg].flow_kgh
        if len(i) and len(o) and i.notna().all() and o.notna().all():
            bal.append(dict(equipment=tg, in_kgh=round(i.sum(), 1), out_kgh=round(o.sum(), 1), out_minus_in=round(o.sum() - i.sum(), 2)))
    st.dataframe(pd.DataFrame(bal), hide_index=True)
    st.caption("Only equipment whose inlet and outlet flows are all known is listed.")
    if f_energy.exists():
        en = pd.read_csv(f_energy)
        e = en.set_index("item").value
        get = lambda start: next((e[k] for k in e.index if k.startswith(start)), None)
        st.subheader("Energy balance (from the Aspen workbook)")
        kk = st.columns(5)
        for col, (label, start, unit, mult) in zip(kk, [("Biomass energy in", "Biomass energy in (LHV, from", "kW", 1), ("Syngas chemical energy", "Syngas chemical energy", "kW", 1),
                                                          ("Cold gas efficiency", "Cold gas efficiency", "%", 100), ("Net shaft work", "Net shaft work (turbine", "kW", 1),
                                                          ("Overall efficiency", "Overall net shaft efficiency", "%", 100)]):
            v = get(start)
            col.metric(label, "n/a" if v is None else f"{v * mult:,.1f} {unit}")
        if "duty" in eq:
            st.write("**Heat duty and power per equipment**")
            st.dataframe(eq[eq.duty.fillna("") != ""][["tag", "name", "duty"]], hide_index=True)
        with st.expander("Full energy balance table"):
            st.dataframe(en, hide_index=True)
    else:
        st.info("No simulation energy data for this case. Run import_aspen.py with an Aspen workbook to add it.")

# ================================================================ 9. what-if: capacity
with t_whatif:
    base_st = pd.read_csv(DATA / case, dtype={"no": str})
    feed = base_st.flow_kgh.dropna().iloc[0] if base_st.flow_kgh.notna().any() else None
    if feed is None:
        st.info("This case has no flow data.")
    else:
        st.write("Move the biomass feed and see which flows, duties and line sizes change. Everything is scaled in proportion, "
                 "which holds only if the oxidant ratio, temperatures and pressures stay the same. It is not a re-run of the process model.")
        new_feed = st.slider("Biomass feed (kg/h)", min_value=float(round(feed * 0.4)), max_value=float(round(feed * 2.0)), value=float(feed), step=1.0)
        factor = new_feed / feed
        _, _, ln2, _, _ = p.build_frames(pd.read_csv(DATA / "equipment.csv"), p.scale_streams(base_st, factor))
        cmp = pd.DataFrame({"stream": lines.pfd_stream, "from": lines["from"], "to": lines.to, "flow now (kg/h)": lines.flow_kgh.round(1),
                            "flow new (kg/h)": ln2.flow_kgh.round(1), "size now": lines["size"], "size new": ln2["size"]})
        cmp["size changes"] = (cmp["size now"] != cmp["size new"]).map({True: "YES", False: ""})
        c1, c2, c3 = st.columns(3)
        c1.metric("Capacity", f"{new_feed * 24 / 1000:.2f} TPD", f"{(factor - 1) * 100:+.0f} %")
        c2.metric("Lines that change size", int((cmp["size changes"] == "YES").sum()))
        if f_energy.exists():
            ee = pd.read_csv(f_energy).set_index("item").value
            shaft = next((ee[k] for k in ee.index if k.startswith("Net shaft work (turbine")), None)
            if shaft is not None:
                c3.metric("Net shaft work (scaled)", f"{shaft * factor:,.0f} kW")
        st.dataframe(cmp, hide_index=True)
        f_duty, f_ref = DATA / f"duties_{stem}.csv", DATA / f"{stem}_reference.csv"
        if f_duty.exists():
            d = pd.read_csv(f_duty)
            d["kW new"] = (d.kW * factor).round(1)
            st.write("**Heat duties and power (scaled)**")
            st.dataframe(d.rename(columns={"kW": "kW now"}), hide_index=True)
        if f_ref.exists():
            ref = pd.read_csv(f_ref)
            ref = ref[ref.this_case > 5]                      # extensive quantities only (kW), not efficiencies
            ref["scaled to 8.5 TPD"] = (ref.this_case * 354 / 417).round(1)
            ref["difference %"] = ((ref["scaled to 8.5 TPD"] / ref.reference_8p5tpd - 1) * 100).round(2)
            st.write("**Check of the scaling rule** against the 8.5 TPD reference values in the Aspen workbook:")
            st.dataframe(ref.rename(columns={"reference_8p5tpd": "8.5 TPD reference (kW)", "this_case": "10 TPD (kW)"}), hide_index=True)

# ================================================================ 10. glossary and symbols
with t_gloss:
    c1, c2 = st.columns(2)
    c1.download_button("Download project glossary (Excel)", (DATA / "glossary.xlsx").read_bytes(), "glossary.xlsx")
    core = DATA / "PID_core_elements_glossary.xlsx"
    if core.exists():
        c2.download_button("Download P&ID core elements glossary (Excel)", core.read_bytes(), core.name)
    for title, f in [("Tag and control conventions", "glossary_tag_conventions.csv"), ("ISA-5.1 identification letters", "glossary_isa_letters.csv"),
                     ("Symbols, line types, service codes, pipe specs", "symbols_legend.csv")]:
        with st.expander(title):
            g = pd.read_csv(DATA / f).fillna("")
            st.dataframe(g, hide_index=True)
            st.download_button(f"Download CSV: {title}", g.to_csv(index=False), f, "text/csv", key="dl_" + f)
    lsheet = DATA.parent / "symbols" / "legend_sheet.html"
    if lsheet.exists():
        st.subheader("Legend sheet (PID-000)")
        st.download_button("Download legend sheet (HTML, print to PDF)", lsheet.read_text(encoding="utf-8"), "PID_legend_sheet.html", "text/html")
        components.html(lsheet.read_text(encoding="utf-8"), height=760, scrolling=True)
    legend = DATA.parent / "symbols" / "legend.html"
    if legend.exists():
        st.subheader("Symbol library")
        components.html(legend.read_text(encoding="utf-8"), height=900, scrolling=True)

# ================================================================ 11. DEXPI import (optional)
with t_dexpi:
    st.write("Upload a DEXPI (Proteus XML) P&ID exported from a P&ID tool. It is read directly, no extra install needed.")
    upd = st.file_uploader("DEXPI XML file", type=["xml"], key="dexpi_live")
    if upd is not None:
        try:
            dn_, dt_ = dexpi_bridge.parse_proteus(upd.getvalue())
            k_ = st.columns(4)
            for col_, kd_ in zip(k_, ["equipment", "line", "valve", "instrument"]):
                col_.metric(kd_.capitalize(), int((dn_.kind == kd_).sum()) if len(dn_) else 0)
            st.dataframe(dt_, hide_index=True)
            st.dataframe(dn_, hide_index=True)
            st.download_button("Relations (CSV)", dt_.to_csv(index=False), "dexpi_triples.csv", "text/csv")
            st.download_button("Items (CSV)", dn_.to_csv(index=False), "dexpi_items.csv", "text/csv")
        except Exception as e:
            st.error(f"Could not read this file as DEXPI XML: {e}")
    if dexpi_bridge.available():
        st.caption("pyDEXPI is installed: its own converter is below as a second opinion.")
        upx = st.file_uploader("DEXPI XML file", type=["xml"], key="dexpi_up")
        if upx is not None:
            tmp = r["out"] / upx.name
            tmp.write_bytes(upx.getvalue())
            try:
                dg = dexpi_bridge.dexpi_xml_to_graph(tmp)
                rel = pd.DataFrame(dexpi_bridge.graph_to_triples(dg), columns=["subject", "relation", "object"])
                st.success(f"{dg.number_of_nodes()} nodes, {dg.number_of_edges()} connections")
                st.dataframe(rel, hide_index=True)
                st.download_button("Download relations (CSV)", rel.to_csv(index=False), "dexpi_triples.csv", "text/csv")
            except Exception as e:
                st.error(f"pyDEXPI could not convert this file: {e}")

# ================================================================ failure what-if
with t_fail:
    st.write("Pick an asset and a failure mode. The estimate follows the plant connections and design flows: which lines lose flow, "
             "which equipment loses feed, the effect on heat duties and engine power, and which safety functions sit on the affected equipment.")
    st.caption("Not a dynamic simulation. Fail positions (FC / FO) are assumptions until the control philosophy fixes them.")
    Gf = r["G"].copy()
    for m in r["motors"].to_dict("records"):
        Gf.add_node(m["tag"], kind="motor", drives=m["drives"])
    f_duty = DATA / f"duties_{stem}.csv"
    imp = fail.Impact(Gf, eq, lines, pd.read_csv(f_duty) if f_duty.exists() else None)
    assets_f = sorted(n for n, d in Gf.nodes(data=True) if d["kind"] in ("valve", "instrument", "equipment", "motor"))
    c1, c2 = st.columns(2)
    who = c1.selectbox("Asset", assets_f, index=assets_f.index("FV-4021") if "FV-4021" in assets_f else 0)
    opts = fail.modes(Gf, who)
    mode = c2.selectbox("Failure mode", [d for _, d in opts])
    code = dict((d, c) for c, d in opts)[mode]
    res = imp.analyse(who, code)
    st.subheader(res["summary"])
    for n in res["notes"]:
        st.write("- " + n)
    if len(res["energy"]):
        shaft = res["energy"][res["energy"]["item"].str.contains("shaft", case=False)]
        if len(shaft):
            st.metric("Engine net shaft work", f"{float(shaft.design_kW.sum() + shaft.change_kW.sum()):,.0f} kW", f"{float(shaft.change_kW.sum()):,.0f} kW")
    if len(res["lines"]):
        st.write("**Lines affected**")
        st.dataframe(res["lines"], hide_index=True)
    if len(res["equipment"]):
        st.write("**Equipment losing feed**")
        st.dataframe(res["equipment"], hide_index=True)
    if len(res["energy"]):
        st.write("**Heat duties and power**")
        st.dataframe(res["energy"], hide_index=True)
    if res["protections"]:
        st.write("**Safety functions on the affected equipment:** " + "; ".join(res["protections"]))
    with st.expander("Whole-plant failure table (every asset, every mode)"):
        fm = imp.fmea()
        st.dataframe(fm, hide_index=True)
        st.download_button("Download failure table (CSV)", fm.to_csv(index=False), f"failure_table_{stem}.csv", "text/csv")

# ================================================================ AI P&ID model
with t_ai:
    mpath = pid_ai.MODEL_DIR / "pid_model.npz"
    if not mpath.exists():
        st.info("No trained model in models/. Run:  python train_pid_model.py")
    else:
        mdl = pid_ai.PIDModel.load(mpath)
        card = mdl.card
        st.write("A learned model that proposes the P&ID control structure from the PFD alone: for each stream the in-line valves and "
                 "instruments, for each equipment item its instruments. It runs in NumPy; no GPU or PyTorch is needed.")
        st.write("**How it was trained:** " + " → ".join(card.get("stages", [])))
        res_rows = []
        for name_v, v in card.get("results", {}).items():
            t = v["test_real_8p5tpd"]["line"]
            g = v["test_generated"]["line"]
            res_rows.append({"variant": name_v, "8.5 TPD streams, top-1": t["top1"], "top-5": t["topk"], "item F1": t["f1"],
                             "generated test, top-1": g["top1"], "generated F1": g["f1"]})
        st.write("**Results on the 8.5 TPD case, which the model never saw** (27 streams; one stream = 0.037):")
        st.dataframe(pd.DataFrame(res_rows), hide_index=True)
        sc = card.get("seed_check_real_test_streams")
        if sc:
            with st.expander("Check over three more random seeds"):
                for k_, v_ in sc.items():
                    st.write(f"- **{k_}**: {v_}")
        st.caption("What the numbers say: training on generated flowsheets is what helps most; fine-tuning on the 10 TPD plant adds a little; "
                   "pre-training on the GraphML grammar adds nothing measurable. Equipment instruments follow fixed rules and are easy. "
                   "Confidence values are over-confident: treat them as a ranking, not a probability.")
        st.subheader(f"Proposals for this case ({name})")
        st_now = pd.read_csv(DATA / case, dtype={"no": str})
        pred = mdl.predict(pd.read_csv(DATA / "equipment.csv"), st_now)
        show = st.radio("Show", ["Streams", "Equipment"], horizontal=True, key="ai_show")
        sel = pred[pred.kind == ("line" if show == "Streams" else "equip")]
        if show == "Streams":
            sel = sel.merge(st_now[["no", "from", "to", "service"]], left_on="element", right_on="no").drop(columns=["no", "kind"])
        st.metric("Agrees with the current table", f"{int(sel.agrees.sum())} of {len(sel)}")
        st.dataframe(sel, hide_index=True)
        if show == "Streams" and st.button("Save the model's proposals as a new case"):
            new = st_now.copy()
            new["pid_hints"] = new["no"].map(dict(zip(sel.element, sel.predicted))).fillna("")
            new.to_csv(DATA / f"streams_{stem}_ai.csv", index=False)
            st.cache_resource.clear()
            st.success(f"Saved as streams_{stem}_ai.csv. Pick it in the sidebar to see the P&ID the model proposes.")
        with st.expander("SFILES 2.0 strings used as the model's view of the flowsheet"):
            st.code(r["pfd_sfiles"] or "", language=None)

# ================================================================ handover files
with t_hand:
    st.write("Machine-readable files for handover to other tools, regenerated with every change:")
    files = [("pid_model.json", "Full model: every asset and line with all properties, relations and failure modes", "application/json"),
             ("pid.sfiles.txt", "PFD and P&ID as SFILES 2.0-style strings", "text/plain"),
             ("pid_graph.graphml", "P&ID graph in the PID2Graph GraphML schema (same format as the synthetic training graphs)", "application/xml"),
             ("pid_dexpi_style.xml", "DEXPI 1.3-style Proteus XML (not validated; check with pyDEXPI)", "application/xml"),
             ("knowledge_graph_triples.csv", "Knowledge graph as subject, relation, object rows", "text/csv"),
             ("PID_deliverables.xlsx", "Equipment, line, valve, instrument and motor lists, BOM", None)]
    for fname, desc, mime in files:
        fp = r["out"] / fname
        if fp.exists():
            c1, c2 = st.columns([3, 1])
            c1.write(f"**{fname}**: {desc}")
            c2.download_button("Download", fp.read_bytes(), f"{stem}_{fname}", mime, key="hand_" + fname)
    st.subheader("SFILES 2.0-style strings")
    st.write("**PFD**")
    st.code(r["pfd_sfiles"] or "", language=None)
    st.write("**P&ID** (valves `(v){XV}`, instruments `(C){TIC}`, signals `_n` / `<_n`)")
    st.code(r["pid_sfiles"] or "", language=None)
    if r["pid_sfiles"]:
        ok = sfiles.signature(sfiles.decode(r["pid_sfiles"])) == sfiles.signature(sfiles.pid_graph(r["G"]))
        st.caption(("Round trip checked: decoding the P&ID string gives back the same units, valves, instruments and connections." if ok
                    else "Round trip check failed for this case: the string does not decode to the same graph.") +
                   " Independent implementation of the published SFILES 2.0 notation; the official package may order the string differently.")
