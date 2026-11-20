"""
sfiles.py - SFILES 2.0-style text notation for the PFD and the P&ID.

Written from the notation published by Vogel, Hirtreiter, Schulze Balhorn and Schweidtmann,
"SFILES 2.0: an extended text-based flowsheet representation", Optimization and Engineering (2023).
It is an independent implementation, not the authors' code; the official package (github.com/process-intelligence-research/SFILES2)
uses a full canonical ranking, so its strings can differ in order while describing the same graph.

Notation used here
  (abbr)            unit, e.g. (r) reactor, (hex) heat exchanger, (raw) inlet, (prod) outlet
  (u)(v)            u feeds v
  (u)[(v)](w)       branch: u feeds v and w
  <1 ... 1          numbered connection (from 10 up written %10, as in SMILES): '1' after the sending unit, '<1' after the receiving unit
                    (used for recycles and for converging streams)
  (hex){1}          both sides of one multi-stream heat exchanger carry the same number
  n|                start of a separate mass train
  (v){XV}           valve, with its type
  (C){TIC}          control / instrument unit with its ISA-5.1 letters. Between two units it measures the stream;
                    hung in a branch from a unit it measures that unit.
  _1 ... <_1        signal: '_1' after the sending control unit, '<_1' after the receiving valve or unit
Extensions to the published abbreviation list, for solids and power units, are marked * in ABBR.
"""
import re
from collections import defaultdict

import networkx as nx

ABBR = {  # equipment type -> SFILES abbreviation (* = extension)
    "package": "X", "hopper": "hop*", "screw_heated": "scr*", "electric_heater_vessel": "hex", "auger_reactor": "r", "cyclone": "cyc*",
    "condenser": "cond", "tank": "tank", "bin": "tank", "blower": "comp", "flare": "prod", "cooling_tower": "X", "solids_cooler": "hex",
    "solids_separator": "sep", "heat_exchanger": "hex", "gasifier": "r", "gas_cooler": "hex", "bag_filter": "fil*", "filter": "fil*",
    "knockout_drum": "flash", "stack": "prod", "air_intake": "raw",
}
VALVE_NAME = {"HV": "HV", "CK": "CK", "PCV": "PCV", "XV": "XV", "FV": "FV", "TV": "TV", "LV": "LV", "SG": "SG", "RV": "RV", "FA": "FA", "PSV": "PSV"}


def _abbr(eq_type, in_deg, out_deg):
    a = ABBR.get(eq_type, "X").rstrip("*")
    if a == "X" and in_deg == 0:
        return "raw"
    if a == "X" and out_deg == 0:
        return "prod"
    return a


# ---------------------------------------------------------------------------- graphs
def pfd_graph(eq, st):
    """Material-flow graph of the PFD tables. Nodes: equipment tags. Edges: streams."""
    H = nx.MultiDiGraph()
    types = dict(zip(eq.tag, eq.type))
    for r in st.itertuples(index=False):
        a, b = getattr(r, "_1") if hasattr(r, "_1") else None, None
    for _, r in st.iterrows():
        for t in (r["from"], r["to"]):
            if t not in H:
                H.add_node(t, tag=t, etype=types.get(t, "package"), kind="unit")
        H.add_edge(r["from"], r["to"], kind="mat", line=str(r["no"]), service=r.get("service", ""))
    for n in H.nodes:
        H.nodes[n]["abbr"] = _abbr(H.nodes[n]["etype"], H.in_degree(n), H.out_degree(n))
    return _split_hex(H)


def pid_graph(G):
    """SFILES-ready graph of the generated P&ID (from pfd_to_pid.build).
    In-line valves and measured streams become units on the line; equipment instruments hang from their unit;
    transmitter + controller pairs on a line collapse into one control unit, as in SFILES 2.0."""
    H = nx.MultiDiGraph()
    rep = {}                                         # P&ID node -> SFILES node
    for n, d in G.nodes(data=True):
        if d["kind"] == "equipment":
            H.add_node(n, tag=n, etype=d.get("type", "package"), kind="unit")
            rep[n] = n
        elif d["kind"] == "valve":
            pre = re.match(r"[A-Z]+", n).group()
            H.add_node(n, tag=n, kind="valve", abbr="v", name=VALVE_NAME.get(pre, pre))
            rep[n] = n
    for n, d in G.nodes(data=True):                  # taps: the instrument on the line, or its controller
        if d["kind"] == "tap":
            insts = [v for _, v in G.out_edges(n)]
            first = insts[0]
            ctrl = [v for _, v, e in G.out_edges(first, data=True) if G.nodes[v]["kind"] == "instrument"]
            shown = ctrl[0] if ctrl else first
            H.add_node(shown, tag=shown, kind="control", abbr="C", name=G.nodes[shown]["letters"], members=[first] + ctrl)
            for m in [n, first] + ctrl:
                rep[m] = shown
    for n, d in G.nodes(data=True):                  # equipment-mounted instruments
        if d["kind"] == "instrument" and n not in rep:
            H.add_node(n, tag=n, kind="control", abbr="v" if d["letters"] == "PSV" else "C",
                       name=d["letters"], on=d.get("on"))
            rep[n] = n
    for u, v, d in G.edges(data=True):
        a, b = rep.get(u), rep.get(v)
        if a is None or b is None or a == b:
            continue
        if d["kind"] == "process":
            H.add_edge(a, b, kind="mat", line=d.get("line", ""), service=d.get("service", ""))
        elif H.nodes[b]["kind"] == "control" and H.nodes[a]["kind"] == "unit" and not H.nodes[b].get("members"):
            H.add_edge(a, b, kind="att")             # unit -> its own instrument: hung as a branch
        else:
            if H.nodes[a]["kind"] == "unit" and H.nodes[b]["kind"] == "control":
                continue
            H.add_edge(a, b, kind="sig", trip=d["kind"] == "trip")
    for n in list(H.nodes):                          # instruments that only act on their unit still hang from it
        d = H.nodes[n]
        if d["kind"] == "control" and not d.get("members") and H.in_degree(n) == 0 and d.get("on") in H:
            H.add_edge(d["on"], n, kind="att")
    for n, d in H.nodes(data=True):
        if d["kind"] == "unit":
            mi = sum(1 for _, _, e in H.in_edges(n, data=True) if e["kind"] == "mat")
            mo = sum(1 for _, _, e in H.out_edges(n, data=True) if e["kind"] == "mat")
            d["abbr"] = _abbr(d["etype"], mi, mo)
    return _split_hex(H)


def _split_hex(H):
    """A heat exchanger with two process sides becomes two nodes sharing a number, as SFILES 2.0 requires."""
    num = 0
    pair = {"CWS": "CWR"}
    for n in [n for n, d in H.nodes(data=True) if d.get("abbr") == "hex"]:
        ins = [(u, k, e) for u, _, k, e in H.in_edges(n, keys=True, data=True) if e["kind"] == "mat"]
        outs = [(v, k, e) for _, v, k, e in H.out_edges(n, keys=True, data=True) if e["kind"] == "mat"]
        if len(ins) < 2 or len(outs) < 2:
            continue
        num += 1
        free = list(outs)
        sides = []
        for u, k, e in ins:
            want = pair.get(e.get("service"), e.get("service"))
            match = next((o for o in free if o[2].get("service") == want), free[0] if free else None)
            if match:
                free.remove(match)
            sides.append(((u, k, e), match))
        base = dict(H.nodes[n])
        for i, ((u, k, e), match) in enumerate(sides, 1):
            side = f"{n}/{i}"
            H.add_node(side, **{**base, "tag": n, "hexnum": num, "side": i})
            H.add_edge(u, side, **e)
            if match:
                H.add_edge(side, match[0], **match[2])
        for o in free:
            H.add_edge(f"{n}/1", o[0], **o[2])
        for u, _, k, e in list(H.in_edges(n, keys=True, data=True)):
            if e["kind"] != "mat":
                H.add_edge(u, f"{n}/1", **e)
        for _, v, k, e in list(H.out_edges(n, keys=True, data=True)):
            if e["kind"] != "mat":
                H.add_edge(f"{n}/1", v, **e)
        H.remove_node(n)
    return H


# ---------------------------------------------------------------------------- encoder
def _token(d):
    t = f"({d['abbr']})"
    if d.get("name"):
        t += "{" + d["name"] + "}"
    if d.get("hexnum"):
        t += "{" + str(d["hexnum"]) + "}"
    return t


def _ranks(H):
    """Morgan-style invariant on the material graph, then the SFILES 2.0 tie-breaks (control, outlet, inlet, other; name; tag)."""
    U = nx.Graph()
    U.add_nodes_from(H.nodes)
    U.add_edges_from((u, v) for u, v, e in H.edges(data=True) if e["kind"] in ("mat", "att"))
    val = {n: 1 for n in U}
    distinct = 1
    while True:
        new = {n: sum(val[m] for m in U[n]) or val[n] for n in U}
        nd = len(set(new.values()))
        if nd <= distinct:
            break
        val, distinct = new, nd
    def cat(n):
        d = H.nodes[n]
        if d["kind"] != "unit":
            return 0
        return {"prod": 1, "raw": 2}.get(d["abbr"], 3)
    order = sorted(H.nodes, key=lambda n: (cat(n), val[n], H.nodes[n].get("abbr", ""), H.nodes[n].get("name", ""), str(n)))
    return {n: i for i, n in enumerate(order)}


def _num(k):
    """Connection number as written: one digit, or %nn from 10 up (the SMILES convention SFILES follows)."""
    return str(k) if k < 10 else "%" + str(k)


def encode(H):
    """Graph -> SFILES 2.0-style string."""
    rank = _ranks(H)
    marks = defaultdict(list)
    counter = {"r": 0, "s": 0}
    out, seen = [], set()

    def mat_out(n):
        return sorted([(v, e) for _, v, e in H.out_edges(n, data=True) if e["kind"] == "mat"], key=lambda x: rank[x[0]])

    def att_out(n):
        return sorted([v for _, v, e in H.out_edges(n, data=True) if e["kind"] == "att"], key=lambda x: rank[x])

    def visit(n):
        seen.add(n)
        out.append(("node", n))
        for a in att_out(n):                       # instruments hung from the unit
            if a not in seen:
                out.append("[")
                visit(a)
                out.append("]")
        nxt = []
        for v, e in mat_out(n):
            if v in seen:
                counter["r"] += 1
                marks[n].append(_num(counter["r"]))
                marks[v].append("<" + _num(counter["r"]))
            else:
                nxt.append(v)
        for i, v in enumerate(nxt):
            if v in seen:                           # reached through an earlier branch
                counter["r"] += 1
                marks[n].append(_num(counter["r"]))
                marks[v].append("<" + _num(counter["r"]))
                continue
            last = i == len(nxt) - 1 or all(w in seen for w in nxt[i + 1:])
            if last:
                visit(v)
            else:
                out.append("[")
                visit(v)
                out.append("]")

    starts = sorted([n for n in H.nodes if not any(e["kind"] in ("mat", "att") for _, _, e in H.in_edges(n, data=True))], key=lambda n: rank[n])
    first = True
    loops = sorted([n for n in H.nodes if H.nodes[n]["kind"] != "control"], key=lambda n: rank[n])   # loops with no inlet start at a unit
    for s in starts + loops + sorted(H.nodes, key=lambda n: rank[n]):
        if s in seen:
            continue
        if not first:
            out.append("n|")
        visit(s)
        first = False
    for u, v, e in H.edges(data=True):
        if e["kind"] == "sig":
            counter["s"] += 1
            marks[u].append("_" + _num(counter["s"]))
            marks[v].append("<_" + _num(counter["s"]))
    txt = "".join((_token(H.nodes[x[1]]) + "".join(marks[x[1]])) if isinstance(x, tuple) else x for x in out)
    return txt.replace("[]", "")


# ---------------------------------------------------------------------------- tokenizer and decoder
TOKEN = re.compile(r"\([^)]*\)|\{[^}]*\}|<_%\d+|<_\d|_%\d+|_\d|<&\||&\||n\||<%\d+|<\d|%\d+|\d|\[|\]")


def tokenize(s):
    toks = TOKEN.findall(s)
    if "".join(toks) != s:
        raise ValueError("Not a valid SFILES string (unknown characters).")
    return toks


def decode(s):
    """SFILES 2.0-style string -> MultiDiGraph (nodes numbered in order of appearance)."""
    H = nx.MultiDiGraph()
    cur, stack, branch_from = None, [], None
    open_r, open_s = {}, {}
    n = 0
    for t in tokenize(s):
        if t.startswith("("):
            n += 1
            node = f"{t[1:-1]}-{n}"
            H.add_node(node, abbr=t[1:-1], kind="control" if t == "(C)" else ("valve" if t == "(v)" else "unit"))
            src = branch_from if branch_from is not None else cur
            if src is not None:
                H.add_edge(src, node, kind="mat")
            cur, branch_from = node, None
        elif t.startswith("{"):
            body = t[1:-1]
            H.nodes[cur]["hexnum" if body.isdigit() else "name"] = int(body) if body.isdigit() else body
        elif t == "[":
            stack.append(cur)
            branch_from = cur
        elif t == "]":
            cur = stack.pop()
            branch_from = None
        elif t == "n|":
            cur, branch_from = None, None
        elif t.startswith("<_"):
            k = t[2:]
            if k in open_s:
                H.add_edge(open_s.pop(k), cur, kind="sig")
            else:
                open_s[k] = ("to", cur)
        elif t.startswith("_"):
            k = t[1:]
            if k in open_s and isinstance(open_s[k], tuple):
                H.add_edge(cur, open_s.pop(k)[1], kind="sig")
            else:
                open_s[k] = cur
        elif t.startswith("<"):
            k = t[1:]
            if k in open_r:
                H.add_edge(open_r.pop(k), cur, kind="mat")
            else:
                open_r[k] = ("to", cur)
        elif t.isdigit() or t.startswith("%"):
            if t in open_r and isinstance(open_r[t], tuple):
                H.add_edge(cur, open_r.pop(t)[1], kind="mat")
            else:
                open_r[t] = cur
    # a control unit (or relief valve) in a branch with nothing after it measures the unit it hangs from
    for u, v, k, e in list(H.edges(keys=True, data=True)):
        if e["kind"] == "mat" and H.nodes[v]["abbr"] in ("C", "v") and not any(x["kind"] == "mat" for _, _, x in H.out_edges(v, data=True)) \
                and H.nodes[u]["kind"] == "unit":
            e["kind"] = "att"
    return H


def signature(H):
    """Counts used to check that a round trip keeps the graph: units by abbreviation, valves and controls by name, edges by kind."""
    from collections import Counter
    nodes = Counter(H.nodes[n]["abbr"] + ("{" + H.nodes[n]["name"] + "}" if H.nodes[n].get("name") else "") for n in H.nodes)
    edges = Counter(e["kind"] for _, _, e in H.edges(data=True))
    return dict(nodes), dict(edges)


def roundtrip_ok(H):
    s = encode(H)
    D = decode(s)
    return signature(H) == signature(D), s


if __name__ == "__main__":
    import pandas as pd

    import pfd_to_pid as p
    for case in ("streams_aspen_10tpd.csv", "streams_gasifier_full.csv"):
        eq = pd.read_csv(p.DATA / "equipment.csv")
        st = pd.read_csv(p.DATA / case, dtype={"no": str})
        Hp = pfd_graph(eq[eq.tag.isin(set(st["from"]) | set(st["to"]))], st)
        okp, sp = roundtrip_ok(Hp)
        G = p.build(case)[0]
        Hi = pid_graph(G)
        oki, si = roundtrip_ok(Hi)
        print(case, "\nPFD  :", sp, "\n round trip:", okp, "\nP&ID :", si[:600], "...\n round trip:", oki, signature(Hi)[1])
