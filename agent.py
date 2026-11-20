"""
agent.py - "plant operator" assistant that answers questions about the plant.

Two modes:
1. quick_answer(): no AI and no key needed. Answers counts, linkage, trips/controls, flows and energy figures
   straight from the knowledge graph and tables.
2. ask(): if an Anthropic API key is set, a general LLM (Claude) is given the plant data and answers in free text.
   It is not a trained model; the data is sent with every question.

Key: environment variable ANTHROPIC_API_KEY, or Streamlit secret of the same name.  pip install anthropic
Test:  python agent.py --out out_aspen_10tpd --case streams_aspen_10tpd.csv "How many control valves are there?"
"""
import argparse
import os
import re
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
DATA = HERE / "data"
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")

SYSTEM = """You are an experienced control-room operator and process engineer for a two-stage biomass \
gasification plant (auger pyrolyser, partial-oxidation gasifier with char bed, oxidant supply, syngas cooling and \
clean-up, gas engine).

Answer using ONLY the plant data given below: the knowledge graph (subject, relation, object), the node \
properties, the line list, the energy balance and the tag glossary. Rules:
- Use the exact tags from the data (for example R-401, FIC-4021). Never invent a tag, value or set point.
- For counts, count from the tables and list the tags.
- For "what happens if" questions, trace the FEEDS relations downstream and the CONTROLS / TRIPS relations. \
Say clearly that this is a connectivity-based answer. For capacity changes, flows and duties scale in proportion \
only if ratios and temperatures stay the same; say so.
- If the data does not contain what is needed (no set points, no datasheets, no dynamic model), say so.
- The P&ID is an auto-generated draft that has not been through HAZOP. Never present an answer as an approved \
operating instruction. For anything safety-related, tell the user to follow site procedures and a qualified engineer.
- Be concise and use plain language."""


def _load(out_dir, case=None):
    out = Path(out_dir)
    nodes = pd.read_csv(out / "knowledge_graph_nodes.csv")
    triples = pd.read_csv(out / "knowledge_graph_triples.csv")
    lines = pd.read_excel(out / "PID_deliverables.xlsx", sheet_name="Line list", dtype={"pfd_stream": str})
    energy = None
    if case:
        f = DATA / (Path(case).stem.replace("streams_", "") + "_energy.csv")
        energy = pd.read_csv(f) if f.exists() else None
    return nodes, triples, lines, energy


def build_context(out_dir, case=None):
    """All plant facts as text. Small plants fit in one prompt; for large plants retrieve a sub-graph instead."""
    nodes, triples, lines, energy = _load(out_dir, case)
    tags = pd.read_csv(DATA / "glossary_tag_conventions.csv")
    keep = [c for c in ["id", "kind", "label", "type", "area", "location", "duty"] if c in nodes.columns]
    parts = ["## NODES (assets)\n" + nodes[nodes.kind != "line"][keep].to_csv(index=False),
             "## LINE LIST (blank = no data)\n" + lines.to_csv(index=False),
             "## KNOWLEDGE GRAPH (subject, relation, object)\n" + triples.to_csv(index=False),
             "## TAG CONVENTIONS\n" + tags.to_csv(index=False)]
    if energy is not None:
        parts.append("## ENERGY BALANCE FROM ASPEN (kW unless stated; efficiencies are fractions)\n" + energy.to_csv(index=False))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------- no-AI answers
VALVE_GROUPS = {"control valve": ("FV", "TV", "LV", "PV"), "shutdown valve": ("XV",), "on/off valve": ("XV",), "safety valve": ("PSV",), "relief valve": ("PSV",),
                "check valve": ("CK",), "isolation valve": ("HV",), "manual valve": ("HV",), "regulator": ("PCV",), "rotary": ("RV",), "slide gate": ("SG",), "flame arrestor": ("FA",)}
VARIABLE = {"temperature": "T", "pressure": "P", "flow": "F", "level": "L", "speed": "S", "analy": "A"}


def _prefix(tag):
    return re.match(r"[A-Z]+", tag).group()


def quick_answer(question, out_dir, case=None):
    nodes, triples, lines, energy = _load(out_dir, case)
    q = question.lower()
    assets = nodes[nodes.kind != "line"]
    T = [tuple(x) for x in triples.values]
    found = sorted([i for i in nodes.id if str(i).lower() in q], key=len, reverse=True)
    found = [t for i, t in enumerate(found) if not any(t in u for u in found[:i])]
    L = lines.set_index("line_no")

    def down(start):
        seen, todo = set(start), list(start)
        while todo:
            a = todo.pop()
            for s, r, o in T:
                if s == a and r == "FEEDS" and o not in seen:
                    seen.add(o)
                    todo.append(o)
        return sorted(x for x in seen - set(start) if x in set(assets[assets.kind == "equipment"].id))

    # ---- counts
    if re.search(r"how many|count|number of|list (all|the)", q):
        sel, what = assets, "assets"
        m = re.search(r"area\s*(\d)00", q)
        for name, pre in VALVE_GROUPS.items():
            if name in q:
                sel, what = assets[(assets.kind.isin(["valve", "instrument"])) & assets.id.map(_prefix).isin(pre)], name + "s"
                break
        else:
            if "valve" in q:
                sel, what = assets[(assets.kind == "valve") | (assets.id.map(_prefix) == "PSV")], "valves and in-line fittings"
            elif re.search(r"trip|safety function|interlock", q):
                sel, what = assets[assets.location == "SIS"], "safety functions"
            elif "controller" in q or "control loop" in q:
                sel, what = assets[(assets.kind == "instrument") & assets.id.map(_prefix).str.contains("C") & (assets.location == "DCS")], "controllers"
            elif re.search(r"instrument|transmitter|indicator|sensor", q):
                sel, what = assets[assets.kind == "instrument"], "instruments"
                for word, letter in VARIABLE.items():
                    if word in q:
                        sel, what = sel[sel.id.str.startswith(letter)], f"{word} instruments"
            elif "motor" in q or "drive" in q:
                sel, what = assets[assets.kind == "motor"], "motors"
            elif "equipment" in q:
                sel, what = assets[assets.kind == "equipment"], "equipment items"
            elif "line" in q or "pipe" in q or "stream" in q:
                return f"**{len(lines)} lines.** " + ", ".join(lines.line_no)
        if m:
            sel, what = sel[sel.area == int(m.group(1)) * 100], what + f" in area {m.group(1)}00"
        if what == "assets":
            c = assets.kind.value_counts()
            return (f"**{len(assets)} assets and {len(lines)} lines:** {c.get('equipment', 0)} equipment, {c.get('instrument', 0)} instruments "
                    f"(including safety valves), {c.get('valve', 0)} valves and in-line fittings, {c.get('motor', 0)} motors.")
        return f"**{len(sel)} {what}.** " + ", ".join(sel.id)

    # ---- plant-level energy
    if energy is not None and not found and re.search(r"energy|efficien|power|shaft|kw|heat|duty|lhv", q):
        e = energy.set_index("item").value
        want = ["Biomass energy in (LHV, from", "Syngas chemical energy (LHV)", "Cold gas efficiency", "Net shaft work (turbine", "Overall net shaft efficiency",
                "Dryer + decomposer", "B2 syngas cooler", "B16 exhaust heat exchanger", "Stack sensible heat at S15"]
        pick = [next(k for k in e.index if k.startswith(w)) for w in want if any(k.startswith(w) for k in e.index)]
        return "**Energy balance (Aspen):**\n" + "\n".join(f"- {k}: {e[k]:.4g} {'(fraction)' if 'efficiency' in k.lower() else 'kW'}" for k in pick)

    if not found:
        return None
    tag = found[0]
    n = nodes[nodes.id == tag].iloc[0]
    start = [tag]
    if n.kind in ("valve", "instrument"):
        on = [o for s, r, o in T if s == tag and r == "INSTALLED_ON"]
        acts = [o for s, r, o in T if s == tag and r in ("CONTROLS", "TRIPS")]
        start = on or acts or [tag]
    ins = [(s, r) for s, r, o in T if o == tag]
    outs = [(r, o) for s, r, o in T if s == tag]

    if re.search(r"\bfail|stuck|what if|what happens if|closes?\b|stops?\b|trips? (spurious|without)|blocked", q) and n.kind != "line" and case:
        import failure as F
        import pfd_to_pid as p
        G = p.build(case)[0]
        for m in G.graph.get("motors", []):
            G.add_node(m["tag"], kind="motor", drives=m["drives"])
        if tag in G:
            stem = Path(case).stem.replace("streams_", "")
            duties = pd.read_csv(DATA / f"duties_{stem}.csv") if (DATA / f"duties_{stem}.csv").exists() else None
            eqd, lnd = p.build(case)[1], p.build(case)[2]
            imp = F.Impact(G, eqd, lnd, duties)
            opts = F.modes(G, tag)
            want = "open" if re.search(r"opens?|fully open", q) else ("spurious" if "spurious" in q else "")
            code, desc = next(((c, d) for c, d in opts if want and want in c), opts[0])
            res = imp.analyse(tag, code)
            txt = f"**If {desc}:** {res['summary']}"
            if res["notes"]:
                txt += " " + " ".join(res["notes"])
            if len(res["energy"]):
                sh = res["energy"][res["energy"]["item"].str.contains("shaft", case=False)]
                if len(sh):
                    txt += f" Engine net shaft work changes by {sh.change_kW.sum():,.0f} kW."
            if res["protections"]:
                txt += " Safety functions on the affected equipment: " + "; ".join(res["protections"]) + "."
            other = [d for c, d in opts if c != code]
            if other:
                txt += " Other failure modes: " + "; ".join(other) + "."
            return txt + " (Estimate from connections and design flows; fail positions assumed.)"
    if n.kind == "line":
        x = L.loc[tag]
        extra = "".join(f", {lab} {x[c]:g} {u}" for c, lab, u in [("vol_m3h", "volume flow", "m³/h"), ("density_kgm3", "density", "kg/m³"), ("enthalpy_kW", "enthalpy flow", "kW")]
                        if c in x and pd.notna(x[c]))
        comp = f" Composition (mass): {x.composition}." if "composition" in x and isinstance(x.composition, str) and x.composition else ""
        on_it = [s for s, r, o in T if o == tag and r == "INSTALLED_ON"]
        return (f"**{tag}** ({x.description}) runs from {x['from']} to {x.to}: {x.flow_kgh:g} kg/h at {x.temp_C:g} °C and {x.press_bar:g} bar, size {x['size']}{extra}.{comp}"
                + (f" On this line: {', '.join(on_it)}." if on_it else ""))
    if re.search(r"downstream|affect|impact|fail|replace|goes to|where does|feeds? (to|into what)|what does .* feed", q):
        d = down(start)
        return (f"Downstream of **{tag}** by process flow: {', '.join(d) or 'nothing'}. "
                "This follows connections only; recycle and heat-recovery links make the list long. Effect sizes need a process model.")
    if re.search(r"feeds|upstream|supplies|comes? (in|from)|inlet|source", q):
        src = sorted({(s2, s) for s, r in ins if r == "FEEDS" for s2, r2, o2 in T if o2 == s and r2 == "FEEDS"})
        return f"**{tag}** is fed by: " + ("; ".join(f"{a} through {l}" for a, l in src) or "nothing recorded") + "."
    if re.search(r"trip|interlock|control|loop|shut|close|measure|instrument", q):
        rel = [f"{s} {r.lower().replace('_', ' ')} {tag}" for s, r in ins if r != "FEEDS"] + [f"{tag} {r.lower().replace('_', ' ')} {o}" for r, o in outs if r != "FEEDS"]
        return f"Control and safety links of **{tag}**: " + ("; ".join(rel) or "none recorded") + "."
    # default: summary card
    txt = f"**{tag}**: {n.label} ({n.kind}" + (f", area {int(n.area)}" if pd.notna(n.area) else "") + ")."
    if "duty" in n and isinstance(n.duty, str) and n.duty:
        txt += f" {n.duty}."
    if n.kind == "equipment":
        li = [s for s, r in ins if r == "FEEDS"]
        lo = [o for r, o in outs if r == "FEEDS"]
        fmt = lambda ls: ", ".join(f"{l} ({L.loc[l].flow_kgh:g} kg/h, {L.loc[l].temp_C:g} °C)" if pd.notna(L.loc[l].flow_kgh) else l for l in ls) or "none"
        txt += f" In: {fmt(li)}. Out: {fmt(lo)}."
        inst = [s for s, r in ins if r in ("MEASURES", "CONTROLS", "TRIPS")]
        txt += f" Instruments and functions: {', '.join(inst) or 'none'}."
    else:
        txt += " " + "; ".join(f"{r.lower().replace('_', ' ')} {o}" for r, o in outs) + "."
    return txt


HELP = ("I can answer these without an AI key: counts (\"how many control valves in area 400\"), linkage (\"what feeds E-402\", "
        "\"what is downstream of R-401\"), control and trips (\"what trips XV-4022\"), failures (\"what if FV-4021 fails\"), an asset or line by tag (\"R-401\"), "
        "and energy figures (\"cold gas efficiency\"). For free-form questions, set ANTHROPIC_API_KEY.")


def ask(question, out_dir, history=None, api_key=None, case=None):
    """history: list of {"role": "user"|"assistant", "content": str}. Returns the answer text."""
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        try:
            return quick_answer(question, out_dir, case) or HELP
        except Exception as e:
            return f"Could not work that out from the tables ({e}). " + HELP
    try:
        import anthropic
    except ImportError:
        return "The 'anthropic' package is not installed. Run: pip install anthropic"
    client = anthropic.Anthropic(api_key=api_key)
    messages = list(history or []) + [{"role": "user", "content": question}]
    reply = client.messages.create(model=MODEL, max_tokens=1200,
                                   system=SYSTEM + "\n\n# PLANT DATA\n" + build_context(out_dir, case), messages=messages)
    return "".join(block.text for block in reply.content if getattr(block, "type", "") == "text")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--out", default="out_aspen_10tpd")
    ap.add_argument("--case", default="streams_aspen_10tpd.csv")
    a = ap.parse_args()
    print(ask(a.question, HERE / a.out, case=a.case))


# ---------------------------------------------------------------------------- AI import: PFD drawing -> tables
EXTRACT_PROMPT = """You are reading a process flow diagram (PFD). Extract it into two tables and return ONLY one JSON object:
{{"equipment": [{{"tag": "R-101", "name": "...", "type": "...", "area": 100}}],
 "streams": [{{"no": "1", "from": "TAG", "to": "TAG", "service": "..", "phase": "gas|liquid|solid", "flow_kgh": null, "temp_C": null, "press_bar": null,
              "pid_hints": "", "description": "..."}}]}}
Rules:
- One equipment row per item drawn. Invent a tag only if none is shown: letters for the type, then area digit and number (R-101).
- "type" must be one of: {types}. Use "package" for vendor units and anything that fits nothing else.
- "area" is 100, 200, 300 ... by plant section, left to right.
- One stream row per arrow between two items, in flow direction. "from" and "to" must be equipment tags from your equipment list.
- "service" is a short code such as BM (biomass), CH (char), PG (pyrolysis gas), SG (syngas), OX (oxygen), AIR, EG (exhaust), BO (bio-oil), CD (condensate), CWS, CWR, N2.
- Copy flow, temperature and pressure only if they are written on the drawing; otherwise null. Never guess numbers.
- "pid_hints" may hold codes separated by ';' from: {hints}. Leave it empty unless clearly appropriate (ISO on utility and liquid lines,
  FIC;XV;CHK on an oxidant feed to a reactor, RV on solids into a reactor, XV on fuel gas to an engine or reactor).
- In "description" say what the stream is and note anything you were unsure about."""


def extract_pfd(file_bytes, media_type, api_key=None):
    """Send a PFD image or PDF to the LLM and get equipment and stream tables back. Returns (equipment_df, streams_df, raw_text)."""
    import base64
    import json
    import pfd_to_pid as p
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("An Anthropic API key is needed for this step (ANTHROPIC_API_KEY).")
    import anthropic
    block_type = "document" if media_type == "application/pdf" else "image"
    content = [{"type": block_type, "source": {"type": "base64", "media_type": media_type, "data": base64.standard_b64encode(file_bytes).decode()}},
               {"type": "text", "text": EXTRACT_PROMPT.format(types=", ".join(sorted(p.EQUIPMENT_RULES)), hints=", ".join(sorted(p.HINTS)))}]
    reply = anthropic.Anthropic(api_key=api_key).messages.create(model=MODEL, max_tokens=8000, messages=[{"role": "user", "content": content}])
    text = "".join(b.text for b in reply.content if getattr(b, "type", "") == "text")
    data = json.loads(text[text.index("{"):text.rindex("}") + 1])
    eq = pd.DataFrame(data["equipment"], columns=["tag", "name", "type", "area"])
    st = pd.DataFrame(data["streams"], columns=["no", "from", "to", "service", "phase", "flow_kgh", "temp_C", "press_bar", "pid_hints", "description"])
    eq["type"] = eq["type"].where(eq["type"].isin(p.EQUIPMENT_RULES), "package")
    return eq, st, text


def check_tables(eq, st):
    """Problems that would stop generation. Returns a list of messages (empty = OK)."""
    import pfd_to_pid as p
    msgs = []
    known = set(eq.tag.dropna())
    missing = (set(st["from"].dropna()) | set(st["to"].dropna())) - known
    if missing:
        msgs.append("Streams refer to equipment that is not in the equipment table: " + ", ".join(sorted(map(str, missing))))
    bad = {h.strip() for cell in st["pid_hints"].dropna() for h in str(cell).split(";") if h.strip()} - set(p.HINTS)
    if bad:
        msgs.append("Unknown pid_hints codes: " + ", ".join(sorted(bad)))
    badt = set(eq.type.dropna()) - set(p.EQUIPMENT_RULES)
    if badt:
        msgs.append("Unknown equipment types: " + ", ".join(sorted(badt)))
    if eq.tag.duplicated().any():
        msgs.append("Duplicate equipment tags: " + ", ".join(eq.tag[eq.tag.duplicated()]))
    if st["no"].astype(str).duplicated().any():
        msgs.append("Duplicate stream numbers.")
    return msgs
