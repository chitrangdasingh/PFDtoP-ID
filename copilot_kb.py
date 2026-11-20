"""
copilot_kb.py - the copilot's knowledge base and search (no external services needed).

Knowledge comes from four places, all searchable with one TF-IDF index (NumPy only):
  1. Built-in engineering notes for this plant: MOC and corrosion rules, predictive-maintenance questions (simple to
     tough), PFD-to-P&ID review questions, HAZOP flow-deviation questions, live digital-twin gap rules. They use the
     tags of the generated P&ID (R-401, XV-4022, FV-4021, E-402, V-401, AI-4038 ...).
  2. The current case's engineering tables (line designation, valve fail positions, set points, nozzles) as text.
  3. The Aspen model idealisations (balance.IDEALISATIONS).
  4. Your documents: PDF / TXT / MD files uploaded in the Copilot tab (papers, vendor manuals, HAZOP reports) are split
     into chunks and stored in data/kb/ so they stay between sessions.
"""
import json
import math
import re
from collections import Counter
from pathlib import Path

import numpy as np

DATA = Path(__file__).parent / "data"
KB_DIR = DATA / "kb"

# (id, topic, title, text, related tags)
NOTES = [
    ("MOC-01", "moc", "Oxidant line material", "The oxidant (45 % O2 + 55 % CO2 by mass) is pre-heated to 450 C in E-401 and fed through FV-4021, XV-4022 and "
     "CK-4023 to R-401. Use pipe class SS1: SS316L cleaned for oxygen service, low velocity at tees and valves. Above 200 C consider Monel 400 or "
     "Inconel 625 trim at valves and impingement points. Gaskets and lubricants must be oxygen-compatible and certified grease-free.", "E-401 FV-4021 XV-4022 CK-4023"),
    ("MOC-02", "moc", "Hot syngas and pyrolysis gas", "Raw syngas at 800 C (R-401 to E-401) and 708 C (to CY-401 and E-402) and pyrolysis gas at 500 C use class SS3 "
     "(SS310S). Watch metal dusting / carburisation in CO-rich gas between 450 and 800 C, oxidation and erosion at bends from char dust. Keep pyrolysis "
     "gas lines insulated (heat conservation) so tars do not condense.", "R-401 E-401 CY-401 E-402 R-101"),
    ("MOC-03", "moc", "Wet syngas and condensate", "After the syngas cooler E-402 the gas is at 50 C and saturated; water condenses in V-401 with CO2 and "
     "traces of sulfur, so condensate is acidic (carbonic acid, possible H2S). Use class SS2 (SS316L). If chlorides are above about 50 ppm consider "
     "duplex 2205. No threaded joints in syngas or condensate service (toxic CO).", "E-402 V-401 TK-401"),
    ("MOC-04", "moc", "Engine exhaust", "Exhaust leaves the engine PK-501 at 686 C and the heat recovery exchanger E-501 at 588 C. Class SS3 (SS310S) for "
     "the hot duct; if exhaust is ever cooled below its acid dew point (about 140-160 C) use SS316L with condensate drains. Insulate for personnel "
     "protection.", "PK-501 E-501 ST-501"),
    ("MOC-05", "moc", "Gasifier vessel", "The gasifier R-401 sees about 1100 C in the partial-oxidation zone and 800 C in the char bed: refractory-lined "
     "carbon steel shell with high-alumina refractory, alloy (Inconel 625 / SS310S) oxidant nozzles and grate. Skin thermocouples help to find "
     "refractory damage.", "R-401"),
    ("PDM-L1-01", "pdm", "Daily / shift check: feed", "Is today's biomass dry and free-flowing (design 20 % moisture), or wet from outdoor storage? Wet feed lowers "
     "pyrolyser temperature (TIC-1007) and raises tar.", "H-101 SC-101 R-101 TIC-1007"),
    ("PDM-L1-02", "pdm", "Daily / shift check: condensate", "When V-401 drains (about 58 kg/h), is the water clear, or does it carry black tar or oil? Tar means "
     "tar slip from the char bed or a cold spot upstream.", "V-401 LIC-4014 TK-401"),
    ("PDM-L1-03", "pdm", "Daily / shift check: ash", "Is the ash from CY-401 / TK-403 a fine grey powder (about 4.5 kg/h) or fused lumps (clinker)? Clinker means "
     "local hot spots in the bed.", "CY-401 TK-403 RV-4029"),
    ("PDM-L1-04", "pdm", "Daily / shift check: seals", "Any gas smell, hissing or tar weeping at the rotary valves RV-1015, RV-1019, RV-4029 or the screw seals? "
     "Worn airlocks let air in (O2 in syngas, AI-4038) or let syngas out (CO hazard).", "RV-1015 RV-1019 RV-4029 AI-4038"),
    ("PDM-L2-01", "pdm", "Cooler fouling", "If E-402 outlet temperature (TI-4032) creeps above 50 C while cooling water valve TV-4008 is wide open, the "
     "cooler is fouling with tar / naphthalene. Plan a flush at the next stop.", "E-402 TI-4032 TV-4008 TIC-4008"),
    ("PDM-L2-02", "pdm", "Char bed pressure drop", "Rising char bed pressure drop (PDI-4004) at constant oxidant flow (FIC-4021) points to ash sintering or "
     "bridging. A sudden fall with O2 rising in the syngas (AI-4038) points to channelling.", "R-401 PDI-4004 FIC-4021 AI-4038"),
    ("PDM-L2-03", "pdm", "Cyclone", "Rising cyclone pressure drop (PDI-4013) means a blocked dip-leg or ash valve RV-4029 not turning; ash then carries "
     "over to E-402.", "CY-401 PDI-4013 RV-4029"),
    ("PDM-L2-04", "pdm", "Engine power drop", "Engine shaft power below 386 kW at normal syngas flow: check syngas analysis (AI-4038) calibration and moisture "
     "trap, knock-out V-401 level (LIC-4014) and liquid carry-over, and air-fuel ratio.", "PK-501 AI-4038 V-401"),
    ("PDM-L3-01", "safety", "Oxygen breakthrough", "O2 above 1 vol % in syngas (AI-4038) means the bed is channelling, the feed has bridged, or air leaks in. "
     "At 1.5 vol % trip: close XV-4022 (oxidant) and XV-4036 (syngas to engine), stop feed, purge with nitrogen.", "AI-4038 XV-4022 XV-4036 R-401"),
    ("PDM-L3-02", "safety", "Over-temperature", "High temperature in the gasifier (TSHH-4006, trip about 1210 C) closes XV-4022 and XV-1017. Cause is "
     "usually too much oxidant for the feed (FV-4021 failed open, feed interrupted). Refractory and alloy nozzles are at risk.", "TSHH-4006 XV-4022 XV-1017 FV-4021"),
    ("PDM-L3-03", "safety", "Liquid to engine", "If the knock-out drain LV-4014 sticks closed, V-401 fills and water reaches the engine (hydraulic lock). "
     "LSHH-4015 trips XV-4036 before that.", "V-401 LV-4014 LSHH-4015 XV-4036"),
    ("PDM-L3-04", "safety", "Flashback", "Engine backfire into the gas line is stopped by flame arrestor FA-4037 upstream of the engine. Check its pressure "
     "drop and element condition; a fouled element starves the engine.", "FA-4037 PK-501"),
    ("PDM-L4-01", "gap", "Alkali ash and slagging", "Aspen treats ash as inert, but biomass ash with potassium and sodium can melt at 850-950 C. If PDI-4004 "
     "rises faster than expected, ask for the ash analysis and consider a lower POX temperature.", "R-401 PDI-4004"),
    ("PDM-L4-02", "gap", "Tar cracking vs equilibrium", "Aspen destroys all benzene and naphthalene in OXID. With about 1-2 s gas residence time real "
     "cracking is incomplete; fouling in E-402 tells you how much tar slips.", "R-401 E-402"),
    ("PDM-L4-03", "gap", "Metal dusting in E-401", "E-401 cools CO-rich syngas from 800 to 708 C, inside the metal dusting window. Log hot hours and plan "
     "wall-thickness checks.", "E-401"),
    ("PDM-L4-04", "gap", "Engine lambda", "Aspen burns the syngas with about 2.8 times the stoichiometric air. A real gas engine runs at lambda 1.3-1.8, "
     "giving less exhaust and a different exhaust temperature; E-501 heat recovery must be re-checked with vendor data.", "PK-501 E-501"),
    ("PID-Q-01", "pfd2pid", "Startup and flare", "Where does gas go during start-up before the engine can take it, and on engine trip? A flare or vent "
     "branch with an automatic valve upstream of XV-4036 is not on the PFD and must be added.", "XV-4036 PK-501"),
    ("PID-Q-02", "pfd2pid", "Utilities", "Cooling water for E-402 (assumed 32 to 42 C, about 18 m3/h), nitrogen for purging R-401, CY-401, V-401, "
     "instrument air for the actuated valves, and power for the screws and blowers need headers on the P&ID.", "E-402 R-401"),
    ("PID-Q-03", "pfd2pid", "Isolation for maintenance", "Which items need double block and bleed or spectacle blinds for safe entry: the gasifier, "
     "cyclone, cooler and knock-out drum on syngas service do.", "R-401 CY-401 E-402 V-401"),
    ("PID-Q-04", "pfd2pid", "Spec breaks", "The pipe class changes from SS3 (SS310S) to SS2 (SS316L) at the outlet of E-402 where the gas falls below 425 C "
     "and becomes wet.", "E-402"),
    ("HAZOP-01", "hazop", "No oxidant flow", "If FV-4021 or XV-4022 closes, the bed cools quickly, tar rises and the pyrolysis gas is not converted. Stop "
     "feed on low oxidant flow (FIC-4021 low alarm) and divert gas to the flare.", "FV-4021 XV-4022 FIC-4021"),
    ("HAZOP-02", "hazop", "More oxidant flow", "If FV-4021 fails open the gasifier moves towards full combustion. TSHH-4006 must close XV-4022 before "
     "refractory damage.", "FV-4021 TSHH-4006 XV-4022"),
    ("HAZOP-03", "hazop", "Reverse flow", "If pressure rises in R-401 (blocked outlet), hot syngas could flow back into the oxidant line; CK-4023 and the "
     "oxidant supply pressure prevent it. PSV-4007 protects the vessel.", "CK-4023 PSV-4007 R-401"),
    ("HAZOP-04", "hazop", "Loss of cooling water", "If cooling water to E-402 is lost, hot gas reaches V-401 and the engine. A high temperature trip on "
     "the cooler outlet should divert gas to the flare.", "E-402 TV-4008 V-401"),
    ("HAZOP-05", "hazop", "Engine trip", "When the engine trips, XV-4036 closes in under a second; without a flare branch the gasifier pressure rises "
     "(PSHH / PSV-4007). The P&ID needs the flare or vent route.", "XV-4036 PSV-4007 PK-501"),
    ("TWIN-01", "twin", "Rule: heat recovery vs pyrolysis temperature", "If E-501 heat recovery is above 165 kW while the pyrolyser is below 460 C, ask: has "
     "feed moisture risen above 20 %, or is E-501 fouled?", "E-501 R-101 TIC-1007"),
    ("TWIN-02", "twin", "Rule: bed pressure drop and POX temperature", "If PDI-4004 is above 35 mbar and the POX zone (TI-4002) above 1130 C, ask whether to "
     "increase CO2 dilution in the oxidant to avoid clinker.", "PDI-4004 TI-4002"),
    ("TWIN-03", "twin", "Rule: cooler outlet", "If E-402 outlet (TI-4032) is above 55 C or its pressure drop is high, check cooling water and look for tar "
     "in the V-401 drain.", "TI-4032 E-402 V-401"),
]


def _tokens(text):
    t = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", str(text).lower())
    return t + [a + " " + b for a, b in zip(t, t[1:])]


class Index:
    def __init__(self, docs):
        """docs: list of dicts with id, title, text, source, tags."""
        self.docs = docs
        tf = [Counter(_tokens(d["title"] + " " + d["text"] + " " + d.get("tags", ""))) for d in docs]
        df = Counter(w for c in tf for w in c)
        self.vocab = {w: i for i, w in enumerate(df)}
        n = len(docs)
        self.idf = np.array([math.log((1 + n) / (1 + df[w])) + 1 for w in self.vocab])
        M = np.zeros((n, len(self.vocab)))
        for i, c in enumerate(tf):
            for w, k in c.items():
                M[i, self.vocab[w]] = (1 + math.log(k)) * self.idf[self.vocab[w]]
        self.M = M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-12)

    def search(self, query, k=4, tag_boost=None):
        q = np.zeros(len(self.vocab))
        for w, c in Counter(_tokens(query)).items():
            if w in self.vocab:
                q[self.vocab[w]] = (1 + math.log(c)) * self.idf[self.vocab[w]]
        if not q.any():
            return []
        q /= np.linalg.norm(q)
        s = self.M @ q
        if tag_boost:
            for i, d in enumerate(self.docs):
                if any(t.lower() in (d.get("tags", "") + " " + d["text"]).lower() for t in tag_boost):
                    s[i] += 0.15
        order = np.argsort(-s)[:k]
        return [dict(score=round(float(s[i]), 3), **self.docs[i]) for i in order if s[i] > 0.05]


def builtin_docs():
    return [dict(id=i, topic=t, title=ti, text=tx, tags=tg, source="built-in engineering notes") for i, t, ti, tx, tg in NOTES]


def case_docs(tables, idealisations=None):
    docs = []
    for r in tables["line_designation"].itertuples():
        docs.append(dict(id=r.line_no, topic="line", title=f"Line {r.line_no}", tags=f"{r.line_no} {getattr(r, 'valve_tags', '')}",
                         text=f"Line {r.line_no}: {r.service_name} from {tables['line_designation'].loc[r.Index, 'from']} to {r.to}, size {r.size}, pipe class "
                              f"{r.pipe_class}, MOC {r.moc} ({r.material_spec}), insulation {r.insulation} {r.insulation_mm} mm, operating {r.op_temp_C} C / "
                              f"{r.op_press_barg} bar g, design {r.design_temp_C} C / {r.design_press_barg} bar g, end connection {r.end_connection}, "
                              f"valves {r.valves_on_line}, drains and vents {r.drains_vents}.", source="line designation table"))
    for r in tables["valve_schedule"].itertuples():
        docs.append(dict(id=r.tag, topic="valve", title=f"Valve {r.tag}", tags=r.tag, source="valve schedule",
                         text=f"{r.tag} {r.type} on {r.line}, size {r.size}, fail position {r.fail_position}: {r.reason}. Actuator {r.actuator}. Body {r.body_moc}."))
    for r in tables["setpoints"].itertuples():
        docs.append(dict(id=r.tag, topic="setpoint", title=f"Instrument {r.tag}", tags=f"{r.tag} {r.location}", source="set point schedule",
                         text=f"{r.tag} {r.description} at {r.location}: normal {r.normal} {r.unit}, set point {r.set_point}, alarms low {r.alarm_low} high "
                              f"{r.alarm_high}, trips low {r.trip_low} high {r.trip_high}, range {r.range}. Basis: {r.basis}."))
    for area, now, real, fix in (idealisations or []):
        docs.append(dict(id="ASPEN-" + area[:12], topic="aspen", title=f"Aspen model: {area}", tags=area, source="Aspen review",
                         text=f"{now} Real plant: {real} Recommended: {fix}"))
    return docs


def user_docs():
    KB_DIR.mkdir(parents=True, exist_ok=True)
    f = KB_DIR / "chunks.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []


def add_document(name, data_bytes, chunk=900):
    """Add an uploaded PDF / TXT / MD to the knowledge base. Returns the number of chunks added."""
    if name.lower().endswith(".pdf"):
        import io

        from pypdf import PdfReader
        pages = [(i + 1, p.extract_text() or "") for i, p in enumerate(PdfReader(io.BytesIO(data_bytes)).pages)]
    else:
        pages = [(1, data_bytes.decode("utf-8", errors="ignore"))]
    docs = [d for d in user_docs() if d["source"] != name]
    n = 0
    for pg, text in pages:
        text = re.sub(r"\s+", " ", text).strip()
        for j in range(0, len(text), chunk):
            part = text[j:j + chunk]
            if len(part) > 80:
                docs.append(dict(id=f"{name}#p{pg}.{j // chunk}", topic="document", title=f"{name}, page {pg}", text=part, tags="", source=name))
                n += 1
    (KB_DIR / "chunks.json").write_text(json.dumps(docs), encoding="utf-8")
    return n


def remove_document(name):
    docs = [d for d in user_docs() if d["source"] != name]
    (KB_DIR / "chunks.json").write_text(json.dumps(docs), encoding="utf-8")


def build_index(tables=None, idealisations=None):
    return Index(builtin_docs() + (case_docs(tables, idealisations) if tables else []) + user_docs())
