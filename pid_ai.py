"""
pid_ai.py - learned PFD -> P&ID model (control structure prediction), pure NumPy.

Task (after Hirtreiter, Schulze Balhorn and Schweidtmann, AIChE J. 2024): given the PFD (equipment and streams,
with no valves or instruments), generate the P&ID control structure. The PFD is split into elements, one per
stream and one per equipment item. For each element the model writes the P&ID items token by token
(e.g. a stream -> FIC, XV, CHK, <eos>; a vessel -> LIC, LSHH, TI, <eos>), each step conditioned on the element's
context in the flowsheet and on the tokens already written.

Training follows the transfer-learning recipe in three stages:
  1. pre-training on the synthetic P&ID graphs (PID2Graph-style GraphML): the grammar of what sits on a pipe
     between two items (valves, instruments, how many). Coarse tokens only, because those graphs carry no tags.
  2. training on generated flowsheets (random plants labelled by an engineering policy with built-in variation).
  3. fine-tuning on the real plant (the 10 TPD Aspen case) with augmentation.
The network is a two-layer MLP with two output heads (fine tokens and coarse tokens); the trunk is shared,
so what is learned in stage 1 carries into stages 2 and 3. Inference needs only NumPy (no PyTorch).
"""
import json
import random
from pathlib import Path

import networkx as nx
import numpy as np
import pandas as pd

import pfd_to_pid as p

HERE = Path(__file__).parent
MODEL_DIR = HERE / "models"
EOS = "<eos>"
MAXLEN = 8
COARSE = {"ISO": "VALVE", "CHK": "VALVE", "PCV": "VALVE", "XV": "VALVE", "TV": "VALVE", "LV": "VALVE", "SG": "VALVE", "RV": "VALVE", "FA": "VALVE",
          "PSV": "VALVE"}            # every other token is an instrument
REACTORS = {"gasifier", "auger_reactor"}
KEYWORDS = ["engine", "oxygen", "oxidant", "nitrogen", "biomass", "cooling", "air", "flare", "condensate", "ash", "dust"]


def coarse(tok):
    return EOS if tok == EOS else COARSE.get(tok, "INST")


def _band(t):
    if t is None or (isinstance(t, float) and np.isnan(t)):
        return "na"
    return "<60" if t < 60 else ("60-200" if t <= 200 else ">200")


def _kw(name):
    n = str(name).lower()
    return [k for k in KEYWORDS if k in n]


def _bucket(n):
    return str(min(int(n), 3))


# ============================================================================ elements of a PFD
def elements(eq, st):
    """One element per stream and per equipment item: (kind, id, context features, target tokens or None)."""
    eq = eq[eq.tag.isin(set(st["from"]) | set(st["to"]))]
    info = eq.set_index("tag")
    D = nx.MultiDiGraph()
    D.add_nodes_from(eq.tag)
    for _, r in st.iterrows():
        D.add_edge(r["from"], r["to"], service=r["service"], phase=r["phase"])
    out = []
    for _, r in st.iterrows():
        ft, tt = info.type.get(r["from"], "package"), info.type.get(r["to"], "package")
        f = ["kind=line", f"phase={r['phase']}", f"service={r['service']}", f"from={ft}", f"to={tt}", f"tband={_band(r.get('temp_C'))}",
             f"from_out={_bucket(D.out_degree(r['from']))}", f"to_in={_bucket(D.in_degree(r['to']))}", f"svc_to={r['service']}|{tt}",
             f"svc_from={r['service']}|{ft}", "from_c=general", "to_c=general", "edge=solid"]
        f += [f"fromkw={k}" for k in _kw(info.name.get(r["from"], ""))] + [f"tokw={k}" for k in _kw(info.name.get(r["to"], ""))]
        hints = r.get("pid_hints")
        tgt = [h.strip() for h in str(hints).split(";") if h.strip()] if isinstance(hints, str) or not pd.isna(hints) else []
        out.append(("line", str(r["no"]), f, tgt))
    for e in eq.itertuples():
        ins = [d for _, _, d in D.in_edges(e.tag, data=True)]
        outs = [d for _, _, d in D.out_edges(e.tag, data=True)]
        f = ["kind=equip", f"type={e.type}", f"nin={_bucket(len(ins))}", f"nout={_bucket(len(outs))}"]
        f += sorted({f"in_svc={d['service']}" for d in ins} | {f"out_svc={d['service']}" for d in outs} | {f"out_phase={d['phase']}" for d in outs})
        f += [f"kw={k}" for k in _kw(e.name)]
        tgt = list(e.instruments.split(";")) if isinstance(getattr(e, "instruments", None), str) else [x[0] for x in p.EQUIPMENT_RULES.get(e.type, [])]
        tgt = [t for t in tgt if t]
        out.append(("equip", e.tag, f, tgt))
    return out


def step_features(base, prev):
    """Features for one generation step: the element context plus the last three tokens written."""
    f = list(base) + [f"step={min(len(prev), 5)}"]
    for i in range(1, 4):
        t = prev[-i] if len(prev) >= i else "<s>"
        f.append(f"p{i}={t}")
        f.append(f"c{i}={coarse(t) if t != '<s>' else '<s>'}")
    if prev:
        f.append(f"last_ctx={prev[-1]}|{next((x for x in base if x.startswith(('svc_to=', 'type='))), '')}")
    return f


# ============================================================================ stage 1 data: synthetic P&ID graphs (GraphML)
def graphml_fragments(path):
    """Sequences of items found on each pipe run between two anchors (equipment, branch points, line ends)."""
    G = nx.read_graphml(path)
    lab = {n: d.get("label") for n, d in G.nodes(data=True)}
    chain = {n for n in G if lab[n] in ("connector", "valve", "instrumentation", "arrow") or (lab[n] == "crossing" and G.degree(n) == 2)}
    anchors = [n for n in G if n not in chain and G.degree(n) > 0]
    kindof = lambda n: "general" if lab[n] == "general" else ("end" if G.degree(n) == 1 else "branch")
    frags, done = [], set()
    for a in anchors:
        for nb in G[a]:
            path, prev, cur, styles = [], a, nb, []
            styles.append(G.edges[a, nb].get("edge_label", "solid"))
            while cur in chain:
                path.append(cur)
                nxt = [x for x in G[cur] if x != prev]
                if not nxt:
                    break
                prev, cur = cur, nxt[0]
                styles.append(G.edges[prev, cur].get("edge_label", "solid"))
            key = frozenset([a, cur, *path])
            if key in done:
                continue
            done.add(key)
            toks = ["VALVE" if lab[n] == "valve" else "INST" for n in path if lab[n] in ("valve", "instrumentation")]
            edge = "solid" if styles.count("solid") >= styles.count("non-solid") else "nonsolid"
            end = cur if cur not in chain else path[-1]
            for seq, x, y in ((toks, a, end), (toks[::-1], end, a)):
                frags.append((["kind=line", f"from_c={kindof(x)}", f"to_c={kindof(y)}", f"edge={edge}"], seq))
    return frags


# ============================================================================ stage 2 data: generated flowsheets
NAMES = {"package": "Package", "hopper": "Feed Hopper", "screw_heated": "Heated Screw Feeder", "auger_reactor": "Auger Pyrolyser", "gasifier": "Gasifier",
         "cyclone": "Cyclone", "heat_exchanger": "Heat Exchanger", "gas_cooler": "Gas Cooler", "bag_filter": "Bag Filter", "condenser": "Condenser",
         "knockout_drum": "Knock-out Drum", "filter": "Cartridge Filter", "blower": "Blower", "flare": "Flare", "stack": "Stack", "bin": "Bin", "tank": "Tank",
         "solids_cooler": "Solids Cooler", "solids_separator": "Solids Separator", "electric_heater_vessel": "Electric Heater", "cooling_tower": "Cooling Tower",
         "air_intake": "Air Intake"}
NEXT = {"hopper": ["screw_heated"], "screw_heated": ["auger_reactor", "gasifier"], "auger_reactor": ["gasifier", "cyclone", "condenser"],
        "gasifier": ["heat_exchanger", "gas_cooler", "cyclone"], "cyclone": ["gas_cooler", "condenser", "heat_exchanger", "bag_filter"],
        "heat_exchanger": ["cyclone", "gas_cooler", "condenser"], "gas_cooler": ["bag_filter", "knockout_drum", "condenser", "filter"],
        "bag_filter": ["condenser", "blower", "knockout_drum"], "condenser": ["condenser", "blower", "filter", "knockout_drum"],
        "knockout_drum": ["blower", "engine", "filter"], "filter": ["blower", "engine"], "blower": ["flare", "engine"]}
HOT = {"gasifier": (700, 950), "auger_reactor": (400, 550), "electric_heater_vessel": (550, 700), "heat_exchanger": (300, 750), "cyclone": (350, 750),
       "engine": (550, 700)}


def label_stream(service, phase, ft, tt, fname, tname, temp, rng):
    """Engineering policy used to label generated flowsheets: which in-line items a stream gets. Randomised on purpose."""
    pr = lambda x: rng.random() < x
    eng = lambda n: "engine" in n.lower()
    h = []
    if phase == "solid":
        if ft == "hopper" and pr(0.85):
            h.append("SG")
        if tt in REACTORS and pr(0.9):
            h.append("RV")
        elif ft in ("cyclone", "bag_filter", "gasifier") and tt == "bin" and pr(0.85):
            h.append("RV")
        elif not h and pr(0.15):
            h.append("SG")
    elif phase == "gas":
        if service in ("OX", "OXC", "AIR") and ft == "package" and not eng(fname):
            if pr(0.85):
                h += ["ISO", "PCV"]
        if service in ("OX", "OXC") and tt in REACTORS:
            h += ["FIC", "XV", "CHK"] if pr(0.85) else ["FIC", "XV"]
        if service in ("SG", "PG", "NCG"):
            if tt == "package" and eng(tname):
                h += ["ISO", "XV", "FA"] if pr(0.8) else ["XV", "FA"]
            elif tt == "flare" and pr(0.8):
                h += ["CHK", "PCV", "FA"]
            elif tt in REACTORS and pr(0.85):
                h.append("XV")
            elif tt == "screw_heated" and pr(0.8):
                h += ["CHK", "FIC"]
            if ft == "gasifier" and pr(0.8):
                h.append("TI")
            elif temp > 200 and pr(0.6):
                h.append("TI")
            if (ft == "gasifier" or (tt == "package" and eng(tname))) and pr(0.5):
                h.append("AI")
        if service == "EG" and pr(0.7):
            h.append("TI")
        if service == "N2" and pr(0.8):
            h += ["ISO", "PCV"]
    else:
        if ft == "knockout_drum" and pr(0.85):
            h.append("LV")
        elif service == "CWS" and pr(0.85):
            h.append("ISO")
        elif service == "CWR" and pr(0.8):
            h.append("TV")
        elif pr(0.6):
            h.append("ISO")
    return ";".join(h)


def random_plant(rng, idx=0):
    """A random two-stage-gasification-like flowsheet with side streams, utilities and recycles, labelled by the policy."""
    eq, st = [], []
    count = {}

    def add(t, name=None, area=100):
        count[t] = count.get(t, 0) + 1
        prefix = {"package": "PK", "hopper": "H", "screw_heated": "SC", "auger_reactor": "R", "gasifier": "R", "cyclone": "CY", "heat_exchanger": "E",
                  "gas_cooler": "E", "bag_filter": "F", "condenser": "E", "knockout_drum": "V", "filter": "F", "blower": "B", "flare": "FL", "stack": "ST",
                  "bin": "TK", "tank": "TK", "solids_cooler": "E", "solids_separator": "S", "electric_heater_vessel": "E", "cooling_tower": "CT",
                  "air_intake": "F"}.get(t, "X")
        tag = f"{prefix}-{area // 100}{len(eq) + 1:02d}"
        eq.append(dict(tag=tag, name=name or NAMES.get(t, t), type=t, area=area))
        return tag

    tmp = {}

    def flow(a, b, service, phase):
        ea = next(e for e in eq if e["tag"] == a)
        ta = ea["type"]
        is_engine = ta == "package" and ea["name"] == "Gas Engine Genset"
        lo, hi = HOT.get("engine" if is_engine else ta, (20, 120) if ta not in ("package", "air_intake") else (15, 45))
        temp = rng.uniform(lo, hi) if phase != "liquid" else rng.uniform(25, 60)
        if ta in ("gas_cooler", "condenser", "knockout_drum", "filter", "bag_filter", "blower"):
            temp = rng.uniform(30, 120)
        st.append({"no": str(len(st) + 1), "from": a, "to": b, "service": service, "phase": phase, "flow_kgh": round(rng.uniform(5, 5000), 1),
                   "temp_C": round(temp, 1) if rng.random() > 0.15 else None, "press_bar": 1.0, "pid_hints": None, "description": ""})

    src = add("package", "Biomass Handling System")
    cur, t = src, "hopper"
    cur_t = None
    service, phase = "BM", "solid"
    reactor = None
    while True:
        node = add(t, "Gas Engine Genset" if t == "engine" else None, area=100 if t in ("hopper", "screw_heated", "auger_reactor") else 400)
        if t == "engine":
            eq[-1]["type"] = "package"
        flow(cur, node, service, phase)
        cur, cur_t = node, t
        if t in REACTORS:
            reactor = (node, t)
        if t == "screw_heated":
            service, phase = "BM", "solid"
        elif t == "auger_reactor":
            service, phase = "PV", "gas"
        elif t == "gasifier":
            service, phase = "SG", "gas"
        elif t == "condenser":
            service = "NCG" if service in ("PV", "NCG") else service
        if t == "engine":
            break
        options = NEXT.get(t, ["engine"])
        t = rng.choice(options)
        if t == "condenser" and cur_t == "condenser" and rng.random() < 0.5:
            t = "blower"
        if t in REACTORS and any(e["type"] in REACTORS for e in eq if e["tag"] == cur) and t == "auger_reactor":
            t = "gasifier"
        if t == "flare":
            fl = add("flare", area=200)
            flow(cur, fl, service, phase)
            break
    engine = next((e["tag"] for e in eq if "Engine" in e["name"]), None)
    # side streams and utilities
    for e in list(eq):
        tg, ty = e["tag"], e["type"]
        if ty == "auger_reactor":
            gas = next((e2["tag"] for e2 in eq if e2["type"] == "gasifier"), None)
            if gas and rng.random() < 0.7:
                flow(tg, gas, "CH", "solid")
            else:
                flow(tg, add("bin", "Char Bin", 300), "CH", "solid")
            if rng.random() < 0.3:
                hv, sc, ss = add("electric_heater_vessel", area=100), add("solids_cooler", area=300), add("solids_separator", area=300)
                flow(hv, tg, "SH", "solid")
                flow(tg, sc, "CH", "solid")
                flow(sc, ss, "CH", "solid")
                flow(ss, hv, "SH", "solid")
        if ty == "gasifier":
            if rng.random() < 0.6:
                flow(tg, add("bin", "Ash Bin", 400), "AS", "solid")
            ox = add("package", rng.choice(["Oxygen Supply", "Oxidant Supply (O2 + CO2)", "Air Blower Package"]), 400)
            svc = "AIR" if "Air" in eq[-1]["name"] else ("OXC" if "CO2" in eq[-1]["name"] else "OX")
            if rng.random() < 0.5:
                ph = add("heat_exchanger", "Oxidant Pre-heater", 400)
                flow(ox, ph, svc, "gas")
                flow(ph, tg, svc, "gas")
            else:
                flow(ox, tg, svc, "gas")
        if ty in ("cyclone", "bag_filter") and rng.random() < 0.8:
            flow(tg, add("bin", "Dust Bin" if ty == "bag_filter" else "Ash Bin", 400), "AS", "solid")
        if ty == "bag_filter" and rng.random() < 0.5:
            flow(add("package", "Nitrogen Supply", 400), tg, "N2", "gas")
        if ty in ("condenser", "knockout_drum") and rng.random() < 0.9:
            flow(tg, add("tank", "Condensate Tank" if ty == "knockout_drum" else "Bio-oil Tank", 200), "CD" if ty == "knockout_drum" else "BO", "liquid")
        if ty in ("gas_cooler", "condenser") and rng.random() < 0.8:
            u = next((x["tag"] for x in eq if x["type"] in ("cooling_tower",) or x["name"] == "Cooling Water Header"), None) or \
                add(rng.choice(["cooling_tower", "package"]), "Cooling Water Header" if rng.random() < 0.5 else None, 400)
            if next(x for x in eq if x["tag"] == u)["type"] == "package":
                next(x for x in eq if x["tag"] == u)["name"] = "Cooling Water Header"
            flow(u, tg, "CWS", "liquid")
            flow(tg, u, "CWR", "liquid")
    if engine:
        if rng.random() < 0.7:
            flow(add("air_intake", "Engine Air Intake", 500), engine, "AIR", "gas")
        if rng.random() < 0.6:
            hx = add("heat_exchanger", "Exhaust Heat Recovery", 500)
            flow(engine, hx, "EG", "gas")
            flow(hx, add("stack", "Exhaust Stack", 500), "EG", "gas")
        else:
            flow(engine, add("stack", "Exhaust Stack", 500), "EG", "gas")
    blower = next((e["tag"] for e in eq if e["type"] == "blower"), None)
    screw = next((e["tag"] for e in eq if e["type"] == "screw_heated"), None)
    if blower and screw and rng.random() < 0.2:
        flow(blower, screw, "NCG", "gas")
    eqd = pd.DataFrame(eq)
    stt = pd.DataFrame(st)
    info = eqd.set_index("tag")
    stt["pid_hints"] = [label_stream(r.service, r.phase, info.type[r._2], info.type[r.to], info.name[r._2], info.name[r.to],
                                     r.temp_C if r.temp_C == r.temp_C and r.temp_C is not None else 25.0, rng)
                        for r in stt.itertuples()]
    # equipment instruments: the rules, with some designer-to-designer variation
    inst = []
    for e in eqd.itertuples():
        lst = [x[0] for x in p.EQUIPMENT_RULES.get(e.type, []) if rng.random() > 0.1]
        if e.type not in ("package", "stack", "air_intake") and rng.random() < 0.08:
            lst.append("PI")
        inst.append(";".join(lst))
    eqd["instruments"] = inst
    return eqd, stt


# ============================================================================ features, network
class Vocab:
    def __init__(self, items=()):
        self.itos, self.stoi = [], {}
        for x in items:
            self.add(x)

    def add(self, x):
        if x not in self.stoi:
            self.stoi[x] = len(self.itos)
            self.itos.append(x)
        return self.stoi[x]

    def __len__(self):
        return len(self.itos)


def rows_from(elements_list, fvocab, tvocab, cvocab, grow=True, fine=True):
    """Expand elements into one training row per generation step (teacher forcing)."""
    X, yf, yc = [], [], []
    for _, _, base, tgt in elements_list:
        seq = list(tgt) + [EOS]
        for i, tok in enumerate(seq):
            feats = step_features(base, seq[:i])
            idx = [fvocab.add(f) if grow else fvocab.stoi.get(f) for f in feats]
            X.append(tuple(sorted(j for j in idx if j is not None)))
            if fine:
                yf.append(tvocab.add(tok) if grow else tvocab.stoi.get(tok, -1))
            else:
                yf.append(-1)
            yc.append(cvocab.add(coarse(tok) if fine else tok))
    return X, np.array(yf), np.array(yc)


def densify(X, d):
    M = np.zeros((len(X), d), dtype=np.float32)
    for i, idx in enumerate(X):
        M[i, list(idx)] = 1.0
    return M


class Net:
    """Two-layer MLP, shared trunk, fine head (P&ID tokens) and coarse head (VALVE / INST / <eos>). Adam, softmax cross-entropy."""

    def __init__(self, d_in, n_fine, n_coarse, h1=256, h2=128, seed=0):
        r = np.random.default_rng(seed)
        he = lambda a, b: (r.standard_normal((a, b)) * np.sqrt(2 / a)).astype(np.float32)
        self.P = {"W1": he(d_in, h1), "b1": np.zeros(h1, np.float32), "W2": he(h1, h2), "b2": np.zeros(h2, np.float32),
                  "Wf": he(h2, n_fine) * 0.5, "bf": np.zeros(n_fine, np.float32), "Wc": he(h2, n_coarse) * 0.5, "bc": np.zeros(n_coarse, np.float32)}
        self.m = {k: np.zeros_like(v) for k, v in self.P.items()}
        self.v = {k: np.zeros_like(v) for k, v in self.P.items()}
        self.t = 0

    def grow_input(self, d_in, seed=1):
        """New input features (unseen in earlier stages) get small random weights; everything learned is kept."""
        W = self.P["W1"]
        if d_in > W.shape[0]:
            r = np.random.default_rng(seed)
            extra = (r.standard_normal((d_in - W.shape[0], W.shape[1])) * np.sqrt(2 / d_in)).astype(np.float32)
            self.P["W1"] = np.vstack([W, extra])
            self.m["W1"] = np.vstack([self.m["W1"], np.zeros_like(extra)])
            self.v["W1"] = np.vstack([self.v["W1"], np.zeros_like(extra)])

    def grow_output(self, n_fine, seed=2):
        W = self.P["Wf"]
        if n_fine > W.shape[1]:
            r = np.random.default_rng(seed)
            extra = (r.standard_normal((W.shape[0], n_fine - W.shape[1])) * np.sqrt(1 / W.shape[0])).astype(np.float32)
            self.P["Wf"] = np.hstack([W, extra])
            self.P["bf"] = np.concatenate([self.P["bf"], np.zeros(n_fine - W.shape[1], np.float32)])
            for s in (self.m, self.v):
                s["Wf"] = np.hstack([s["Wf"], np.zeros_like(extra)])
                s["bf"] = np.concatenate([s["bf"], np.zeros(n_fine - W.shape[1], np.float32)])

    def forward(self, X):
        P = self.P
        z1 = X @ P["W1"] + P["b1"]
        a1 = np.maximum(z1, 0)
        z2 = a1 @ P["W2"] + P["b2"]
        a2 = np.maximum(z2, 0)
        return z1, a1, z2, a2, a2 @ P["Wf"] + P["bf"], a2 @ P["Wc"] + P["bc"]

    @staticmethod
    def softmax(z):
        z = z - z.max(1, keepdims=True)
        e = np.exp(z)
        return e / e.sum(1, keepdims=True)

    def fit(self, X, yf, yc, w=None, epochs=5, lr=1e-3, batch=512, lam_c=0.3, freeze=(), seed=0, wd=1e-5, dropout=0.0):
        n = len(X)
        w = np.ones(n, np.float32) if w is None else w.astype(np.float32)
        r = np.random.default_rng(seed)
        hist = []
        for ep in range(epochs):
            order = r.permutation(n)
            tot = 0.0
            for s in range(0, n, batch):
                b = order[s:s + batch]
                xb, fb, cb, wb = X[b], yf[b], yc[b], w[b]
                if dropout:
                    xb = xb * (r.random(xb.shape) > dropout)
                z1, a1, z2, a2, lf, lc = self.forward(xb)
                g = {}
                dlf = np.zeros_like(lf)
                mf = fb >= 0
                if mf.any():
                    pf = self.softmax(lf[mf])
                    tot += float(-(np.log(pf[np.arange(mf.sum()), fb[mf]] + 1e-9) * wb[mf]).sum())
                    pf[np.arange(mf.sum()), fb[mf]] -= 1
                    dlf[mf] = pf * wb[mf, None]
                pc = self.softmax(lc)
                mc = cb >= 0
                tot += float(-(np.log(pc[mc, cb[mc]] + 1e-9) * wb[mc]).sum()) * lam_c
                pc[mc, cb[mc]] -= 1
                pc[~mc] = 0
                dlc = pc * wb[:, None] * lam_c
                norm = max(wb.sum(), 1e-6)
                dlf /= norm
                dlc /= norm
                g["Wf"], g["bf"] = a2.T @ dlf, dlf.sum(0)
                g["Wc"], g["bc"] = a2.T @ dlc, dlc.sum(0)
                da2 = dlf @ self.P["Wf"].T + dlc @ self.P["Wc"].T
                dz2 = da2 * (z2 > 0)
                g["W2"], g["b2"] = a1.T @ dz2, dz2.sum(0)
                dz1 = (dz2 @ self.P["W2"].T) * (z1 > 0)
                g["W1"], g["b1"] = xb.T @ dz1, dz1.sum(0)
                self.t += 1
                for k in self.P:
                    if k in freeze:
                        continue
                    gk = g[k] + wd * self.P[k]
                    self.m[k] = 0.9 * self.m[k] + 0.1 * gk
                    self.v[k] = 0.999 * self.v[k] + 0.001 * gk * gk
                    mh = self.m[k] / (1 - 0.9 ** self.t)
                    vh = self.v[k] / (1 - 0.999 ** self.t)
                    self.P[k] -= lr * mh / (np.sqrt(vh) + 1e-8)
            hist.append(tot / max(w.sum(), 1e-6))
        return hist

    def probs(self, X, head="f"):
        out = self.forward(X)
        return self.softmax(out[4] if head == "f" else out[5])


# ============================================================================ model wrapper: train, save, load, generate
class PIDModel:
    def __init__(self):
        self.fv, self.tv, self.cv = Vocab(), Vocab([EOS]), Vocab([EOS, "VALVE", "INST"])
        self.net = None
        self.card = {}

    # ------------------------------------------------ generation
    def beam(self, base, k=5, maxlen=MAXLEN):
        beams = [([], 0.0, False)]
        for _ in range(maxlen):
            live = [b for b in beams if not b[2]]
            if not live:
                break
            X = densify([tuple(sorted(j for j in (self.fv.stoi.get(f) for f in step_features(base, b[0])) if j is not None)) for b in live], len(self.fv))
            P = self.net.probs(X, "f")
            cand = [b for b in beams if b[2]]
            for b, pr in zip(live, P):
                for j in np.argsort(-pr)[:k]:
                    tok = self.tv.itos[j]
                    if tok in b[0] and tok != EOS and tok not in ("TI", "PI"):       # an item type appears once per element (TI, PI may repeat)
                        continue
                    cand.append((b[0] + ([] if tok == EOS else [tok]), b[1] + float(np.log(pr[j] + 1e-12)), tok == EOS))
            beams = sorted(cand, key=lambda b: -b[1])[:k]
        return [(b[0], float(np.exp(b[1]))) for b in sorted(beams, key=lambda b: -b[1])]

    def predict(self, eq, st, k=3):
        """For each stream and equipment item: top-k predicted item lists with probabilities, next to what the table holds now."""
        rows = []
        for kind, ident, base, tgt in elements(eq, st):
            top = self.beam(base, k=max(k, 3))
            rows.append(dict(kind=kind, element=ident, current=";".join(tgt), predicted=";".join(top[0][0]), confidence=round(top[0][1], 3),
                             alternatives=" | ".join(f"{';'.join(s) or '(none)'} ({pr:.2f})" for s, pr in top[1:k]),
                             agrees=set(top[0][0]) == set(tgt)))
        return pd.DataFrame(rows)

    # ------------------------------------------------ persistence
    def save(self, path):
        path = Path(path)
        path.parent.mkdir(exist_ok=True)
        np.savez_compressed(path, **self.net.P)
        meta = dict(features=self.fv.itos, fine=self.tv.itos, coarse=self.cv.itos, card=self.card)
        path.with_suffix(".json").write_text(json.dumps(meta, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path=MODEL_DIR / "pid_model.npz"):
        path = Path(path)
        m = cls()
        meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        m.fv, m.tv, m.cv = Vocab(meta["features"]), Vocab(meta["fine"]), Vocab(meta["coarse"])
        m.card = meta.get("card", {})
        z = np.load(path)
        m.net = Net(1, 1, 1)
        m.net.P = {k: z[k] for k in z.files}
        return m


# ============================================================================ evaluation
def evaluate(model, element_list, k=5):
    """Exact match of the whole item list per element (top-1 and top-k), and item-level precision / recall / F1."""
    res = {"line": [0, 0, 0, 0, 0, 0], "equip": [0, 0, 0, 0, 0, 0]}     # n, top1, topk, tp, fp, fn
    for kind, _, base, tgt in element_list:
        top = model.beam(base, k=k)
        pred = top[0][0]
        r = res[kind]
        r[0] += 1
        r[1] += sorted(pred) == sorted(tgt)
        r[2] += any(sorted(s) == sorted(tgt) for s, _ in top)
        sp, st_ = set(pred), set(tgt)
        r[3] += len(sp & st_)
        r[4] += len(sp - st_)
        r[5] += len(st_ - sp)
    out = {}
    for kind, (n, t1, tk, tp, fp, fn) in res.items():
        if n:
            pr, rc = tp / max(tp + fp, 1), tp / max(tp + fn, 1)
            out[kind] = dict(n=n, top1=round(t1 / n, 3), topk=round(tk / n, 3), precision=round(pr, 3), recall=round(rc, 3),
                             f1=round(2 * pr * rc / max(pr + rc, 1e-9), 3))
    return out
