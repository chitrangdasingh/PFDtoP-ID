"""
failure.py - "what if this fails?" for every valve, instrument and equipment item.

For each asset it lists the credible failure modes (from its type and assumed fail position) and estimates the
consequences from the plant graph and the stream table:
  - which line loses or gains flow, and by how much (kg/h, from the stream table / Aspen)
  - which equipment downstream loses feed, as a fraction of its inlet flow, carried along the process
  - production effect: if a reactor loses its feed or its oxidant, its whole output is counted as lost
  - which heat duties and how much engine power are affected (scaled by the lost fraction)
  - which safety functions sit on the affected equipment
This is a connectivity and mass-share estimate, not a dynamic simulation. Fail positions are assumptions until the
control philosophy fixes them.
"""
import re

import networkx as nx
import pandas as pd

REACTORS = {"gasifier", "auger_reactor"}
CRITICAL_FEEDS = {"BM", "CH", "PG", "PV", "OX", "OXC", "AIR"}     # losing one of these stops a reactor
FAIL_POSITION = {   # valve prefix -> (assumed fail position, what that means)
    "XV": ("FC", "closes"), "FV": ("FC", "closes"), "LV": ("FC", "closes"), "TV": ("FO", "opens fully"),
    "PCV": ("-", "fails open (regulator)"), "HV": ("-", "left closed by mistake"), "CK": ("-", "fails to close (back-flow)"),
    "RV": ("-", "stops (drive failure)"), "SG": ("-", "jams closed"), "FA": ("-", "blocks (fouled element)"), "PSV": ("-", "fails to open on demand"),
}


def prefix(tag):
    return re.match(r"[A-Z]+", tag).group()


def modes(G, tag):
    """Failure modes for one asset: list of (mode code, plain description)."""
    d = G.nodes[tag]
    if d["kind"] == "valve" or tag.startswith("PSV"):
        pre = prefix(tag)
        pos, txt = FAIL_POSITION.get(pre, ("-", "fails"))
        fo = G.graph.get("fail_override", {}).get(tag) if hasattr(G, "graph") else None
        if fo and pre in ("XV", "FV", "LV", "TV"):           # fail position fixed by a correction
            pos = fo[0]
            txt = {"FC": "closes", "FO": "opens fully", "FL": "stays where it is (fail locked)"}.get(pos, txt)
        how = f"fail position {pos}, set by correction {fo[1]}" if fo and pre in ("XV", "FV", "LV", "TV") else f"assumed fail position {pos}"
        out = [(f"{pre}:{txt}", f"{tag} {txt}" + (f" ({how})" if pos != "-" else ""))]
        if pre in ("XV", "FV", "LV", "TV"):
            other = "opens fully" if txt == "closes" else "closes"
            out.append((f"{pre}:{other}", f"{tag} {other} (actuator or positioner fault, opposite to its fail position)"))
        return out
    if d["kind"] == "instrument":
        L = d["letters"]
        if L.endswith(("SHH", "SLL", "SH", "SL")) or L.startswith("BS"):
            return [("trip:no-demand", f"{tag} does not act when needed (loss of protection)"), ("trip:spurious", f"{tag} trips without cause")]
        if "C" in L[1:]:
            return [("ctrl:high", f"{tag} reads high: the loop acts as if the value is high"), ("ctrl:low", f"{tag} reads low: the loop acts as if the value is low")]
        return [("meas:lost", f"{tag} reading lost (operator loses this measurement)")]
    if d["kind"] == "motor":
        return [("motor:stop", f"{tag} stops")]
    return [("equip:stop", f"{tag} stops or is taken out of service")]


class Impact:
    def __init__(self, G, eq, lines, duties=None, energy=None):
        self.G, self.eq, self.lines = G, eq.set_index("tag"), lines.set_index("line_no")
        self.duties = duties
        self.energy = energy
        self.P = nx.MultiDiGraph()
        for r in lines.itertuples():
            self.P.add_edge(r[lines.columns.get_loc("from") + 1], r.to, line=r.line_no)

    def _flow(self, ln):
        f = self.lines.flow_kgh.get(ln)
        return 0.0 if f is None or pd.isna(f) else float(f)

    def _is_engine(self, n):
        return "engine" in str(self.eq.name.get(n, "")).lower()

    def _outlet_sources(self, n, out_line):
        """Inlets that feed one outlet. A heat exchanger keeps its two sides apart (paired by service, CWS -> CWR);
        other equipment mixes all its inlets."""
        ins = [d["line"] for _, _, d in self.P.in_edges(n, data=True)]
        etype = self.eq.type.get(n, "")
        svc = self.lines.service.get(out_line)
        if etype in ("heat_exchanger", "gas_cooler", "condenser", "solids_cooler") and len(ins) > 1:
            want = {"CWR": "CWS"}.get(svc, svc)
            same = [l for l in ins if self.lines.service.get(l) == want]
            if same:
                return same
            return [l for l in ins if self.lines.service.get(l) not in ("CWS", "CWR")] or ins
        return ins

    def stop_lines(self, lost):
        """lost: {line_no: fraction of its flow lost}. Propagates through equipment; returns ({equipment: fraction}, {line: fraction})."""
        frac_eq, frac_ln = {}, dict(lost)
        for _ in range(len(self.P) + 2):                 # iterate to a fixed point (recycles)
            changed = False
            for n in self.P.nodes:
                ins = [(d["line"], self._flow(d["line"])) for _, _, d in self.P.in_edges(n, data=True)]
                tot = sum(f for _, f in ins)
                if not tot:
                    continue
                etype = self.eq.type.get(n, "")
                if self._is_engine(n):                       # an engine runs on its fuel: air does not count
                    fuel = [(l, f) for l, f in ins if self.lines.service.get(l) in ("SG", "PG", "NCG")] or ins
                    lostf = sum(frac_ln.get(l, 0) * f for l, f in fuel) / max(sum(f for _, f in fuel), 1e-9)
                else:
                    lostf = sum(frac_ln.get(l, 0) * f for l, f in ins if self.lines.service.get(l) not in ("CWS", "CWR")) / \
                        max(sum(f for l, f in ins if self.lines.service.get(l) not in ("CWS", "CWR")), 1e-9)
                if etype in REACTORS:                       # a reactor without its feed or oxidant stops altogether
                    if any(frac_ln.get(l, 0) >= 0.99 and self.lines.service.get(l) in CRITICAL_FEEDS for l, _ in ins):
                        lostf = 1.0
                lostf = min(1.0, lostf)
                if abs(frac_eq.get(n, 0) - lostf) > 1e-6:
                    frac_eq[n] = lostf
                    changed = True
                for _, _, d in self.P.out_edges(n, data=True):
                    ol = d["line"]
                    if ol in lost or self.lines.service.get(ol) == "CWR":
                        continue
                    srcs = self._outlet_sources(n, ol)
                    if etype in REACTORS or self._is_engine(n):
                        fr = lostf
                    else:
                        tf = sum(self._flow(l) for l in srcs)
                        fr = sum(frac_ln.get(l, 0) * self._flow(l) for l in srcs) / tf if tf else 0
                    if abs(frac_ln.get(ol, 0) - fr) > 1e-6:
                        frac_ln[ol] = fr
                        changed = True
            if not changed:
                break
        return {k: v for k, v in frac_eq.items() if v > 0.001}, {k: v for k, v in frac_ln.items() if v > 0.001}

    def line_of(self, tag):
        d = self.G.nodes[tag]
        return d.get("line") or d.get("on")

    def protections(self, eqs):
        out = []
        for n, d in self.G.nodes(data=True):
            if d.get("kind") == "instrument" and d.get("loc") == "SIS" and d.get("on") in eqs:
                acts = [v for _, v, e in self.G.out_edges(n, data=True) if e["kind"] == "trip"]
                out.append(f"{n} on {d['on']}" + (f" (acts on {', '.join(acts)})" if acts else ""))
        return out

    def analyse(self, tag, mode):
        """Consequences of one failure mode. Returns dict(summary, lines, equipment, energy, protections, basis)."""
        G = self.G
        kind = G.nodes[tag]["kind"]
        stop, rise, notes = {}, [], []
        if mode.endswith("fail locked)"):
            notes.append(f"{tag} holds its last position: the process carries on, but the loop can no longer correct it.")
        elif mode.startswith(("XV:closes", "FV:closes", "LV:closes", "HV:", "RV:", "SG:", "FA:", "TV:closes")):
            ln = self.line_of(tag)
            if ln in self.lines.index:
                stop[ln] = 1.0
        elif mode.endswith("opens fully") or mode.startswith("PCV:"):
            ln = self.line_of(tag)
            rise.append(ln)
            if mode.startswith("TV:"):
                notes.append("More cooling water flows: the cooled stream leaves colder than design. Usually the safe direction; check for condensation.")
            elif mode.startswith("PCV:"):
                notes.append("Full supply pressure reaches the downstream line and equipment. Check that its design pressure or a relief valve covers this.")
            else:
                notes.append("Flow rises above design. How much depends on the valve size (Cv), which is not defined yet.")
        elif mode.startswith("CK:"):
            notes.append(f"If downstream pressure exceeds upstream, flow runs backwards on {self.line_of(tag)}. Relevant for oxidant and fuel-gas lines.")
        elif mode.startswith("PSV:"):
            notes.append(f"No overpressure protection for {G.nodes[tag].get('on')} while the PSV is stuck; other protections must cover a pressure rise.")
        elif mode.startswith(("ctrl:", "meas:")):
            acts = [v for _, v, e in G.out_edges(tag, data=True) if e["kind"] in ("signal", "trip")]
            valves = [v for v in acts if G.nodes[v]["kind"] == "valve"]
            eqs = [v for v in acts if G.nodes[v]["kind"] == "equipment"]
            if mode == "meas:lost" or not (valves or eqs):
                notes.append("Only the indication is lost; nothing changes in the process unless an operator acts on a wrong value.")
            for v in valves:
                direct = mode == "ctrl:high"
                pre = prefix(v)
                if pre in ("FV", "LV"):        # a high reading makes the loop close the valve
                    sub = f"{pre}:closes" if direct else f"{pre}:opens fully"
                elif pre == "TV":               # cooling valve: a high temperature reading opens it
                    sub = "TV:opens fully" if direct else "TV:closes"
                else:
                    sub = f"{pre}:closes"
                notes.append(f"The loop drives {v}: it {sub.split(':')[1]}.")
                if sub.endswith("closes"):
                    stop[self.line_of(v)] = 1.0
                else:
                    rise.append(self.line_of(v))
            for e in eqs:
                notes.append(f"The loop acts on {e} ({'reduces' if mode == 'ctrl:high' else 'increases'} its heating or speed): its outlet temperature or throughput moves away from design.")
        elif mode.startswith("trip:"):
            acts = [v for _, v, e in G.out_edges(tag, data=True) if e["kind"] == "trip"]
            if mode == "trip:no-demand":
                notes.append(f"Loss of protection: on a real {G.nodes[tag]['letters']} condition, {', '.join(acts) or 'the protected equipment'} would not be shut down.")
            else:
                for v in acts:
                    if G.nodes[v]["kind"] == "valve":
                        stop[self.line_of(v)] = 1.0
                    else:
                        for _, _, d in self.P.out_edges(v, data=True):
                            stop[d["line"]] = 1.0
                notes.append(f"Spurious trip: {', '.join(acts) or 'the protected equipment'} shut down without a real cause.")
        elif mode in ("equip:stop", "motor:stop"):
            target = G.nodes[tag].get("drives") if kind == "motor" else tag
            if target in G and G.nodes[target]["kind"] == "valve":
                return self.analyse(target, f"{prefix(target)}:stops (drive failure)") if prefix(target) == "RV" else self.analyse(target, f"{prefix(target)}:closes")
            for _, _, d in self.P.out_edges(target, data=True):
                stop[d["line"]] = 1.0
        eq_loss, ln_loss = self.stop_lines(stop) if stop else ({}, {})
        blocked = [] if mode in ("equip:stop",) else [l for l, f in stop.items() if f >= 0.99 and l in self.lines.index]
        for ln in blocked:     # a blocked outlet fills its source
            src = self.lines.loc[ln, "from"]
            levels = [n for n, d in G.nodes(data=True) if d.get("kind") == "instrument" and d.get("on") == src and d["letters"].startswith("L")]
            if self.lines.phase.get(ln) in ("liquid", "solid") and levels:
                trips = [n for n in levels if G.nodes[n].get("loc") == "SIS"]
                notes.append(f"Material builds up in {src}; its level instruments ({', '.join(levels)}) see it"
                             + (f", and {', '.join(trips)} trips when the high-high level is reached." if trips else "."))
        rows = []
        for ln, fr in sorted(ln_loss.items(), key=lambda x: -x[1]):
            f = self._flow(ln)
            rows.append(dict(line=ln, stream=self.lines.pfd_stream.get(ln), service=self.lines.service.get(ln), design_kgh=round(f, 1),
                             lost_pct=round(fr * 100), after_kgh=round(f * (1 - fr), 1)))
        eqrows = [dict(equipment=e, name=self.eq.name.get(e, ""), inlet_lost_pct=round(fr * 100)) for e, fr in sorted(eq_loss.items(), key=lambda x: -x[1])]
        energy = []
        hexlike = ("heat_exchanger", "gas_cooler", "condenser", "solids_cooler")

        def duty_frac(t):                                 # an exchanger stops transferring heat if either side stops
            if self.eq.type.get(t, "") in hexlike:
                return max([ln_loss.get(d["line"], 0) for _, _, d in self.P.in_edges(t, data=True)] + [eq_loss.get(t, 0)])
            return eq_loss.get(t, 0)
        if self.duties is not None and len(eq_loss):
            for r in self.duties.itertuples():
                tags = [t.strip() for t in r.tag.split("+")]
                fr = max((duty_frac(t) for t in tags), default=0)
                if fr:
                    energy.append(dict(item=r.item, equipment=r.tag, design_kW=r.kW, change_kW=round(-r.kW * fr, 1)))
        prot = self.protections(set(eq_loss) | {G.nodes[tag].get("on")} | ({tag} if kind == "equipment" else set()))
        if rows:
            first = rows[0]
            summary = (f"Flow lost on {len(rows)} line(s), first {first['line']} ({first['design_kgh']} kg/h). "
                       f"{len(eqrows)} equipment item(s) lose feed" + (f", fully: {', '.join(e['equipment'] for e in eqrows if e['inlet_lost_pct'] >= 99)}" if any(e['inlet_lost_pct'] >= 99 for e in eqrows) else "") + ".")
        elif rise:
            summary = f"Flow on {', '.join(str(x) for x in rise)} rises above design."
        else:
            summary = notes[0] if notes else "No process effect found from the plant connections."
        return dict(summary=summary, notes=notes, lost_lines={k: v for k, v in ln_loss.items()}, lines=pd.DataFrame(rows), equipment=pd.DataFrame(eqrows),
                    energy=pd.DataFrame(energy), protections=prot,
                    basis="Estimate from plant connections and design flows; not a dynamic simulation. Fail positions are assumed.")

    def fmea(self):
        """One row per asset and failure mode: the whole plant at a glance."""
        rows = []
        for n, d in self.G.nodes(data=True):
            if d["kind"] in ("tap",):
                continue
            for code, desc in modes(self.G, n):
                a = self.analyse(n, code)
                pw = a["energy"]
                shaft = pw[pw["item"].str.contains("shaft", case=False)]["change_kW"].sum() if len(pw) else 0
                rows.append(dict(asset=n, kind=d["kind"], failure=desc, effect=a["summary"], lines_affected=len(a["lines"]),
                                 equipment_affected=len(a["equipment"]), engine_power_change_kW=round(shaft, 1),
                                 protections="; ".join(a["protections"])))
        return pd.DataFrame(rows)
