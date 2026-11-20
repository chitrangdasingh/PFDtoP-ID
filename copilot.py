"""
copilot.py - agentic engineering copilot for the smart P&ID.

The copilot works through tools. Read tools look things up; write tools never change the drawing directly:
a correction goes in as "proposed" and only takes effect when an engineer approves it in the Corrections tab,
and a review remark goes in as "open". Every write is logged in the revision log with "copilot" as author.

Without an AI key the same tools run from short commands (see HELP). With ANTHROPIC_API_KEY set, Claude decides
which tools to call (tool-use loop, at most MAX_STEPS calls per question).
"""
import json
import os
import re

import pandas as pd

import balance as BAL
import bom as B
import copilot_kb as KB
import copilot_nlu as NLU
import engineering as ENGR
import failure as F
import kg_query as K
import pid_ai
import workflow

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
MAX_STEPS = 8

TOOLS = [
    dict(name="query_graph", description="Run a one-hop pattern query on the plant knowledge graph. Pattern syntax: (a:kind)-[RELATION]->(b:kind) WHERE a.prop = value. "
         "Kinds: equipment, line, valve, instrument, motor. Relations: FEEDS, MEASURES, CONTROLS, TRIPS, INSTALLED_ON, DRIVES, SIGNAL. Operators: = != > < CONTAINS.",
         input_schema=dict(type="object", properties=dict(pattern=dict(type="string")), required=["pattern"])),
    dict(name="trace", description="Everything known about one tag or line number: data, drawing zone, where it came from, corrections, review remarks, connections.",
         input_schema=dict(type="object", properties=dict(tag=dict(type="string")), required=["tag"])),
    dict(name="neighbours", description="Items within n hops of a tag in the knowledge graph.",
         input_schema=dict(type="object", properties=dict(tag=dict(type="string"), hops=dict(type="integer")), required=["tag"])),
    dict(name="path", description="Shortest flow/signal path between two tags.",
         input_schema=dict(type="object", properties=dict(start=dict(type="string"), end=dict(type="string")), required=["start", "end"])),
    dict(name="failure", description="What happens if an item fails. Leave mode empty to list the failure modes of the tag.",
         input_schema=dict(type="object", properties=dict(tag=dict(type="string"), mode=dict(type="string")), required=["tag"])),
    dict(name="ai_suggest", description="Instruments and valves the trained P&ID model suggests for a stream number or equipment tag, next to what the drawing has.",
         input_schema=dict(type="object", properties=dict(element=dict(type="string")), required=["element"])),
    dict(name="check_drawing", description="List where the trained model disagrees with the drawing, open review remarks and proposed corrections.",
         input_schema=dict(type="object", properties={})),
    dict(name="bom", description="Bill of material totals by category, or the rows matching a text filter.",
         input_schema=dict(type="object", properties=dict(filter=dict(type="string")))),
    dict(name="search_knowledge", description="Search the knowledge base: built-in MOC, maintenance, HAZOP and digital-twin notes for this plant, "
         "the case's engineering tables, the Aspen review and uploaded papers / manuals. Use for 'why', 'how', material and maintenance questions.",
         input_schema=dict(type="object", properties=dict(query=dict(type="string")), required=["query"])),
    dict(name="engineering", description="Engineering data of a tag or line: pipe class, MOC, insulation, design and test conditions, end connection, "
         "valve fail position, instrument set point and alarms, nozzles, drains and vents.",
         input_schema=dict(type="object", properties=dict(tag=dict(type="string")), required=["tag"])),
    dict(name="balance", description="Plant mass and energy balance from Aspen, block closure, and the corrected estimate for a real two-stage downdraft.",
         input_schema=dict(type="object", properties={})),
    dict(name="add_remark", description="Add an open review remark on a tag or line. Use for questions or concerns an engineer must answer.",
         input_schema=dict(type="object", properties=dict(target=dict(type="string"), remark=dict(type="string"),
                                                          category=dict(type="string", enum=workflow.CATEGORIES)),
                           required=["target", "remark"])),
    dict(name="propose_correction", description="Propose a change for engineer approval (status 'proposed'; nothing changes on the drawing until approved). "
         "action: line_items (target = stream number, value = item codes separated by ';', e.g. ISO;TI), equipment_instruments (target = equipment tag, value = e.g. TI;PI;PSV), "
         "line_size (target = stream number, value = size, e.g. 8\"), fail_position (target = valve tag, value = FC, FO or FL).",
         input_schema=dict(type="object", properties=dict(action=dict(type="string", enum=list(workflow.ACTIONS)), target=dict(type="string"),
                                                          value=dict(type="string"), reason=dict(type="string")),
                           required=["action", "target", "value", "reason"])),
]

SYSTEM = """You are an engineering copilot for a biomass gasification plant P&ID (draft generated from a PFD and an Aspen Plus stream table).
Use the tools to look things up; never invent tags, sizes or numbers. Quote tags exactly. Keep answers short and practical.
You may add review remarks and propose corrections, but say clearly that proposals need engineer approval in the Corrections tab.
Do not propose a correction unless the user asked for a change or the evidence from the tools is specific. This is a draft for review, not for construction."""


class Copilot:
    def __init__(self, case, model, G, eq, lines, inst, valves, nodes, triples, bom=None):
        self.case, self.model, self.G, self.eq, self.lines, self.inst, self.valves = case, model, G, eq, lines, inst, valves
        self.nodes, self.triples = nodes, triples
        self.KG = K.graph(nodes, triples)
        self.bom_df = bom
        self._ai = None
        self.eng = ENGR.all_tables(case, G, eq, lines, inst, valves)
        self.kb = KB.build_index(self.eng, BAL.IDEALISATIONS)
        self._nlu = None

    # ------------------------------------------------------------ tools
    def _tag(self, t):
        t = str(t).strip()
        if t in self.model["assets"]:
            return t
        hit = [k for k in self.model["assets"] if k.upper() == t.upper()]
        if hit:
            return hit[0]
        ln = [k for k, a in self.model["assets"].items() if a["kind"] == "line" and str(a.get("pfd_stream")) == t]
        if ln:
            return ln[0]
        raise KeyError(f"No tag or line '{t}' on this P&ID.")

    def query_graph(self, pattern):
        r = K.run(pattern, self.nodes, self.triples)
        return dict(matches=len(r), rows=r.head(40).to_dict("records"))

    def trace(self, tag):
        t = self._tag(tag)
        a = dict(self.model["assets"][t])
        rel = [r for r in self.model["relations"] if t in (r["subject"], r["object"])]
        return dict(asset=a, relations=rel[:40], revision=self.model.get("revision"))

    def neighbours(self, tag, hops=1):
        return K.neighbours(self.KG, self._tag(tag), int(hops or 1)).head(40).to_dict("records")

    def path(self, start, end):
        a, b = self._tag(start), self._tag(end)
        try:
            return K.path(self.KG, a, b).to_dict("records")
        except Exception:
            return dict(note="No directed path; showing the connection ignoring direction.", steps=K.path(self.KG, a, b, directed=False).to_dict("records"))

    def failure(self, tag, mode=""):
        t = self._tag(tag)
        ms = F.modes(self.G, t)
        listing = [dict(code=c, description=d) for c, d in ms]
        if not mode:
            return dict(tag=t, modes=listing)
        pick = next((c for c, d in ms if mode.lower() in c.lower() or mode.lower() in d.lower()), None)
        if not pick:
            return dict(error=f"Mode '{mode}' not found", modes=listing)
        res = F.Impact(self.G, self.eq, self.lines).analyse(t, pick)
        recs = lambda df: df.head(20).to_dict("records") if isinstance(df, pd.DataFrame) else df
        return dict(tag=t, mode=pick, summary=res["summary"], notes=res["notes"], lines=recs(res["lines"]), equipment=recs(res["equipment"]),
                    energy=recs(res["energy"]), protections=res["protections"], basis=res["basis"])

    def _ai_pred(self):
        if self._ai is None:
            m = pid_ai.PIDModel.load()
            st = pd.read_csv(workflow.DATA / self.case, dtype={"no": str})
            eq, st, _ = workflow.apply(self.case, pd.read_csv(workflow.DATA / "equipment.csv"), st)
            self._ai = m.predict(eq, st)
        return self._ai

    def ai_suggest(self, element):
        P = self._ai_pred()
        e = str(element).strip()
        if e in self.model["assets"] and self.model["assets"][e]["kind"] == "line":
            e = str(self.model["assets"][e]["pfd_stream"])
        r = P[P.element.astype(str) == e]
        if not len(r):
            return dict(error=f"'{element}' is not a stream number or equipment tag the model knows.")
        return r.to_dict("records")[0] | dict(note="Model suggestions are hints from patterns, not engineering decisions.")

    def check_drawing(self):
        P = self._ai_pred()
        dis = P[~P.agrees].sort_values("confidence", ascending=False)
        rev = workflow.reviews(self.case)
        cor = workflow.corrections(self.case)
        return dict(model_disagrees=dis.head(15)[["kind", "element", "current", "predicted", "confidence"]].to_dict("records"),
                    open_remarks=rev[rev.status == "open"][["id", "target", "category", "remark"]].to_dict("records"),
                    proposed_corrections=cor[cor.status == "proposed"][["id", "action", "target", "value", "reason"]].to_dict("records"))

    def bom(self, filter=""):
        b = self.bom_df
        if b is None:
            b = self.bom_df = B.create(self.case, self.G, self.eq, self.lines, self.inst, self.valves)
        if filter:
            m = b.apply(lambda r: filter.lower() in " ".join(map(str, r.values)).lower(), axis=1)
            return b[m][["category", "item", "size_spec", "qty", "unit", "tags"]].head(30).to_dict("records")
        return b.groupby("category").qty.sum().to_dict()

    def search_knowledge(self, query):
        tags = [t for t in self.model["assets"] if t.lower() in query.lower()]
        return [dict(title=h["title"], source=h["source"], text=h["text"], score=h["score"]) for h in self.kb.search(query, k=4, tag_boost=tags)]

    def engineering(self, tag):
        t = self._tag(tag)
        out = dict(tag=t)
        E = self.eng
        for name, key in (("line", "line_no"), ("valve", "tag"), ("instrument", "tag")):
            df = {"line": E["line_designation"], "valve": E["valve_schedule"], "instrument": E["setpoints"]}[name]
            hit = df[df[key].astype(str) == t]
            if len(hit):
                out[name] = hit.iloc[0].dropna().to_dict()
        nz = E["nozzles"][E["nozzles"].equipment == t]
        if len(nz):
            out["nozzles"] = nz.drop(columns=["equipment"]).to_dict("records")
            out["instruments"] = E["setpoints"][E["setpoints"].location == t][["tag", "description", "normal", "unit", "set_point", "alarm_high", "trip_high"]].to_dict("records")
        dv = E["drains_vents"][E["drains_vents"]["item"] == t]
        if len(dv):
            out["drains_vents"] = dv.to_dict("records")
        if t in set(E["line_designation"].line_no):
            pass
        elif "line" not in out and "valve" not in out and "instrument" not in out:      # equipment: connected lines
            ld = E["line_designation"]
            out["connected_lines"] = ld[(ld["from"] == t) | (ld.to == t)][["line_no", "service_name", "moc", "insulation", "design_temp_C", "design_press_barg"]].to_dict("records")
        return out

    def balance(self):
        R = BAL.results(workflow.stem_of(self.case))
        if R is None:
            return dict(note="No Aspen stream results for this case.")
        en = workflow.DATA / f"{workflow.stem_of(self.case)}_energy.csv"
        cc = BAL.corrected_case(R, pd.read_csv(en) if en.exists() else None)
        bc = BAL.block_closure(R)
        return dict(corrected_case=cc.to_dict("records"), worst_block_mass_error_pct=float(bc.mass_error_pct.abs().max()),
                    plant_mass_balance=BAL.mass_balance_table(R).to_dict("records"))

    def add_remark(self, target, remark, category="other"):
        t = self._tag(target)
        rid = workflow.add_review(self.case, t, remark, "copilot", category or "other")
        return dict(added=rid, target=t, status="open")

    def propose_correction(self, action, target, value, reason):
        if action not in workflow.ACTIONS:
            return dict(error=f"action must be one of {list(workflow.ACTIONS)}")
        if action in ("line_items", "line_size"):
            t = str(target)
            if t in self.model["assets"]:
                t = str(self.model["assets"][t].get("pfd_stream", t))
        else:
            t = self._tag(target)
        cid = workflow.add_correction(self.case, action, t, value, reason, "copilot", status="proposed")
        return dict(proposed=cid, action=action, target=t, value=value, status="proposed - approve in the Corrections tab")

    def call(self, name, args):
        try:
            return getattr(self, name)(**(args or {}))
        except Exception as e:
            return dict(error=str(e))

    # ------------------------------------------------------------ no-key command mode
    def command(self, text):
        """Short commands without an AI key. Returns (answer_text, [(tool, args, result)])."""
        q = text.strip()
        ql = q.lower()
        tags = sorted((k for k in self.model["assets"] if re.search(r"(?<![\w-])" + re.escape(k.lower()) + r"(?![\w-])", ql)), key=len, reverse=True)
        steps = []

        def run(name, **a):
            res = self.call(name, a)
            steps.append((name, a, res))
            return res
        if q.startswith("("):
            res = run("query_graph", pattern=q)
            return (res.get("error") or f"{res['matches']} match(es)."), steps
        m = re.match(r"(?:remark|review)\s+(?:on\s+)?([^\s:]+)\s*:?\s*(.+)", q, re.I)
        if m:
            res = run("add_remark", target=m.group(1), remark=m.group(2))
            return res.get("error") or f"Remark {res['added']} added on {res['target']} (open).", steps
        m = re.match(r"propose\s+(\w+)\s+(\S+)\s+(\S+)\s*(?:because|:)?\s*(.*)", q, re.I)
        if m:
            res = run("propose_correction", action=m.group(1), target=m.group(2), value=m.group(3), reason=m.group(4) or "proposed via copilot")
            return res.get("error") or f"Correction {res['proposed']} proposed ({res['action']} {res['target']} = {res['value']}). Approve it in the Corrections tab.", steps
        return self._answer_by_intent(q, ql, tags, run, steps)

    def nlu(self):
        if self._nlu is None:
            self._nlu = NLU.load_or_train(list(self.model["assets"]))
        return self._nlu

    def _answer_by_intent(self, q, ql, tags, run, steps):
        """Trained intent model picks the tool; the answer is built from the tool results (no AI key needed)."""
        stream = re.search(r"\bstream\s+([A-Za-z0-9-]+)", q)
        pred = self.nlu().predict(q, 3)
        intent, conf = pred[0]
        steps.append(("intent", dict(question=q), dict(top=[(i, round(p_, 2)) for i, p_ in pred])))
        fmt = lambda v: "-" if v in (None, "") or (isinstance(v, float) and pd.isna(v)) else (f"{v:g}" if isinstance(v, float) else str(v))
        t0 = tags[0] if tags else None
        if intent == "trace" and t0:
            res = run("trace", tag=t0)
            a = res["asset"]
            bits = [f"{t0}: {a.get('title') or a.get('label') or a.get('kind')}", f"drawing zone {a.get('zone') or '-'}", f"created by {a.get('source') or '-'}"]
            if a.get("size"):
                bits.append(f"size {a.get('size')}")
            if a.get("fail_position"):
                bits.append(f"fail position {a.get('fail_position')}")
            if a.get("corrections"):
                bits.append("corrections " + ", ".join(c["id"] for c in a["corrections"]))
            if a.get("remarks"):
                bits.append("remarks " + ", ".join(r_["id"] for r_ in a["remarks"]))
            rel = res.get("relations", [])
            if rel:
                bits.append("relations: " + ", ".join(f"{x['subject']} {x['relation'].lower()} {x['object']}" for x in rel[:8]))
            return "; ".join(bits) + ".", steps
        if intent == "path" and len(tags) >= 2:
            pos = sorted(tags[:2], key=lambda t: ql.index(t.lower()))
            res = run("path", start=pos[0], end=pos[1])
            st_ = res if isinstance(res, list) else res.get("steps", [])
            return " → ".join([pos[0]] + [x["to"] for x in st_]) if st_ else str(res), steps
        if intent in ("failure", "hazop") and t0:
            res = run("failure", tag=t0)
            modes = [m_["code"] for m_ in res.get("modes", [])]
            words = [{"closed": "close", "open": "opens", "stops": "stop"}.get(w_, w_) for w_ in re.findall(r"[a-z]+", ql)
                     if w_ in ("closed", "closes", "close", "open", "opens", "spurious", "demand", "high", "low", "stop", "stops", "lost")]
            want = next((m_ for m_ in modes if any(w_ in m_ for w_ in words)), modes[0] if modes else "")
            res = run("failure", tag=t0, mode=want) if want else res
            kb = run("search_knowledge", query=q)
            extra = ("\n\nRelated notes: " + " | ".join(f"{h['title']}: {h['text'][:180]}" for h in kb[:2])) if kb else ""
            prot = res.get("protections") or []
            return (res.get("summary") or str(res)) + (f" Protections: {', '.join(map(str, prot))}." if prot else "") + extra, steps
        if intent == "neighbours" and t0:
            res = run("neighbours", tag=t0, hops=1 if re.search(r"feeds|upstream|downstream|connect", ql) else 2)
            return f"Connected to {t0}: " + ", ".join(f"{x['id']} ({x['kind']})" for x in res[:15]) + ".", steps
        if intent in ("setpoint", "moc", "fail_position", "nozzles") and t0:
            res = run("engineering", tag=t0)
            parts = []
            if intent == "setpoint" or "instrument" in res:
                i = res.get("instrument")
                if i:
                    parts.append(f"{t0} ({i.get('description')}): normal {fmt(i.get('normal'))} {i.get('unit', '')}, set point {fmt(i.get('set_point'))}, "
                                 f"alarm L {fmt(i.get('alarm_low'))} / H {fmt(i.get('alarm_high'))}, trip LL {fmt(i.get('trip_low'))} / HH {fmt(i.get('trip_high'))}, "
                                 f"range {fmt(i.get('range'))}. Basis: {i.get('basis')}.")
                for x in res.get("instruments", [])[:12]:
                    parts.append(f"{x['tag']} {x['description']}: normal {fmt(x['normal'])} {x['unit']}, SP {fmt(x['set_point'])}, H {fmt(x['alarm_high'])}, HH {fmt(x['trip_high'])}")
            if "valve" in res:
                v = res["valve"]
                parts.append(f"{t0} fails {v.get('fail_position')}: {v.get('reason')} Actuator: {v.get('actuator')}. Body {v.get('body_moc')}, {v.get('end_connection')}.")
            if "line" in res and intent in ("moc", "setpoint", "nozzles"):
                l_ = res["line"]
                parts.append(f"{t0}: {l_.get('service_name')}, class {l_.get('pipe_class')} = {l_.get('moc')} ({l_.get('material_spec')}, {l_.get('rating')}); "
                             f"insulation {l_.get('insulation')} {fmt(l_.get('insulation_mm'))} mm ({l_.get('insulation_purpose')}); operating {fmt(l_.get('op_temp_C'))} C / "
                             f"{fmt(l_.get('op_press_barg'))} bar g; design {fmt(l_.get('design_temp_C'))} C / {fmt(l_.get('design_press_barg'))} bar g; test "
                             f"{fmt(l_.get('test_press_barg'))} bar g; ends: {l_.get('end_connection')}; drains / vents: {l_.get('drains_vents')}.")
            if intent == "nozzles" and res.get("nozzles"):
                parts.append(f"{t0} nozzles: " + "; ".join(f"{n['nozzle']} {n['purpose']} {n['size']}" + (f" ({n['connects_to']})" if n.get('connects_to') else "")
                                                          for n in res["nozzles"]))
            if intent == "moc" and res.get("connected_lines"):
                parts.append(f"Lines at {t0}: " + "; ".join(f"{x['line_no']} {x['moc']} insulation {x['insulation']}" for x in res["connected_lines"]))
            if parts:
                return "\n".join(parts), steps
        if intent == "balance":
            res = run("balance")
            if "corrected_case" in res:
                C_ = {x["item"]: x for x in res["corrected_case"]}
                g = lambda k: C_.get(k, {})
                return (f"Aspen closes: worst block mass error {res['worst_block_mass_error_pct']:.3f} %. Cold gas efficiency Aspen {g('Cold gas efficiency').get('aspen'):.1f} % "
                        f"vs {g('Cold gas efficiency').get('corrected'):.1f} % estimated for a real two-stage downdraft (95 % char conversion, 4 % wall loss). "
                        f"Net electrical: Aspen basis {g('Net electrical output').get('aspen'):.0f} kWe, corrected {g('Net electrical output').get('corrected'):.0f} kWe. "
                        f"Engine air: Aspen {g('Engine combustion air').get('aspen'):.0f} kg/h (lambda {g('Engine lambda').get('aspen'):.1f}) vs "
                        f"{g('Engine combustion air').get('corrected'):.0f} kg/h at lambda 1.6. Details in the Mass and energy balance tab."), steps
            return res.get("note", str(res)), steps
        if intent == "bom":
            res = run("bom", filter="")
            return ", ".join(f"{k}: {v:g}" for k, v in res.items()) if isinstance(res, dict) else str(res), steps
        if intent == "check":
            res = run("check_drawing")
            return (f"{len(res['model_disagrees'])} model disagreement(s), {len(res['open_remarks'])} open remark(s), "
                    f"{len(res['proposed_corrections'])} proposed correction(s). See the Challenges and gaps tab for the full list."), steps
        if intent == "suggest" and (t0 or stream):
            res = run("ai_suggest", element=t0 or stream.group(1))
            if "error" in res:
                return res["error"], steps
            return f"Drawing has: {res['current'] or '(none)'}. Model suggests: {res['predicted'] or '(none)'} (confidence {res['confidence']}).", steps
        if intent == "remark" and t0:
            txt = q.split(":", 1)[1].strip() if ":" in q else re.sub(r".*?" + re.escape(t0), "", q, flags=re.I).strip(" :-") or q
            res = run("add_remark", target=t0, remark=txt)
            return res.get("error") or f"Remark {res['added']} added on {res['target']} (open).", steps
        if intent == "propose":
            return ("To propose a change without an AI key use: propose <action> <target> <value> because <reason>, e.g. "
                    "'propose line_size 15 6\" because nozzle size'. Actions: " + ", ".join(workflow.ACTIONS) + ". With an AI key, ask in plain words."), steps
        if intent == "count":
            pre = {"control valve": ("FV", "TV", "LV", "PV"), "shutdown valve": ("XV",), "valve": None, "trip": ("TSHH", "PSHH", "LSHH", "PSV"), "instrument": "inst",
                   "line": "line", "equipment": "equipment"}
            for k, v in pre.items():
                if k in ql:
                    A = self.model["assets"]
                    if v == "line" or v == "equipment":
                        n = sum(1 for a in A.values() if a["kind"] == v)
                    elif v == "inst":
                        n = len(self.inst)
                    elif v is None:
                        n = len(self.valves)
                    else:
                        n = sum(1 for t in list(self.valves.tag) + list(self.inst.tag) if re.match(r"[A-Z]+", t).group() in v)
                    area = re.search(r"area\s*(\d)00", ql)
                    return f"{n} {k}(s) on the P&ID" + ("" if not area else " (all areas; filter by area in the BOM tab)") + ".", steps
        # knowledge, pdm, hazop without a tag, or anything uncertain: search the knowledge base
        hits = run("search_knowledge", query=q)
        if hits:
            head = "" if conf > 0.35 else "Not sure which tool fits; closest notes:\n"
            return head + "\n".join(f"- {h['title']} ({h['source']}): {h['text'][:320]}" for h in hits[:3]), steps
        if t0:
            res = run("trace", tag=t0)
            a = res["asset"]
            return f"{t0}: {a.get('title') or a.get('label') or a.get('kind')}; zone {a.get('zone') or '-'}; from {a.get('source') or '-'}.", steps
        return HELP, steps

    # ------------------------------------------------------------ AI mode
    def ask(self, question, history=None, api_key=None, on_step=None):
        """Tool-use loop with Claude. Returns (answer_text, [(tool, args, result)])."""
        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return self.command(question)
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msgs = list(history or []) + [dict(role="user", content=question)]
        steps = []
        for _ in range(MAX_STEPS):
            rep = client.messages.create(model=MODEL, max_tokens=1500, system=SYSTEM + f"\nCase: {self.case}. Revision {self.model.get('revision')}.",
                                         tools=TOOLS, messages=msgs)
            msgs.append(dict(role="assistant", content=rep.content))
            uses = [b for b in rep.content if getattr(b, "type", "") == "tool_use"]
            if not uses:
                return "".join(b.text for b in rep.content if getattr(b, "type", "") == "text"), steps
            results = []
            for u in uses:
                res = self.call(u.name, u.input)
                steps.append((u.name, u.input, res))
                if on_step:
                    on_step(u.name, u.input, res)
                results.append(dict(type="tool_result", tool_use_id=u.id, content=json.dumps(res, default=str)[:12000]))
            msgs.append(dict(role="user", content=results))
        return "Stopped after the maximum number of tool calls. Ask a narrower question.", steps


HELP = """Commands (no AI key needed):
  R-401                                     trace a tag: zone, source, corrections, remarks
  path from PK-101 to ST-501                flow path between two items
  what if FV-4021 fails closed              failure what-if
  neighbours of XV-4022                     items within 2 hops
  suggest 15   /  suggest R-401             what the AI model suggests for a stream or equipment
  check drawing                             model disagreements, open remarks, proposed corrections
  bom   /  bom valve                        BOM totals or matching rows
  (a:instrument)-[TRIPS]->(b:valve)         graph pattern query
  remark R-401: confirm PSV set pressure    add a review remark
  propose line_items 4 ISO;TI because ...   propose a correction (needs approval)
With ANTHROPIC_API_KEY set, ask in plain words and the copilot picks the tools itself."""
