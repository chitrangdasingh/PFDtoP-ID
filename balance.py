"""
balance.py - mass, element and energy balance checks on the Aspen stream results, a corrected design case for a real
two-stage downdraft gasifier, and the feedback workbook for the Aspen engineer (kinetics / thermodynamics corrections).

1. block_closure()    every Aspen block: mass in, mass out, enthalpy in - out (= block duty), C/H/O/N/S element closure
2. idealisations()    what the Aspen model assumes that a real two-stage downdraft plant does not do, with the
                      correction recommended in Aspen (kinetic reactor, restricted equilibrium, heat loss, tar, ...)
3. corrected_case()   engineering estimate of the real plant from the Aspen case and editable assumptions
                      (char conversion, heat loss, tar slip, engine lambda and efficiencies). This is an estimate to
                      set expectations and size margins, NOT a replacement for re-running Aspen with the corrections.
4. feedback_workbook() Excel file for the Aspen engineer: closure, element balance, idealisations, data requests and
                      the corrected case side by side.
"""
from pathlib import Path

import pandas as pd

DATA = Path(__file__).parent / "data"
KW_PER_GCAL_H = 1162.2

# species -> element mass fractions (C, H, O, N, S); ASH and non-conventional biomass are left out of element sums
MW = {"C": 12.011, "H": 1.008, "O": 15.999, "N": 14.007, "S": 32.06}
FORMULA = {"C": {"C": 1}, "WATER": {"H": 2, "O": 1}, "S": {"S": 1}, "CO": {"C": 1, "O": 1}, "CO2": {"C": 1, "O": 2}, "N2": {"N": 2},
           "O2": {"O": 2}, "METHANE": {"C": 1, "H": 4}, "H2": {"H": 2}, "C6H6": {"C": 6, "H": 6}, "C7H8": {"C": 7, "H": 8},
           "C10H8": {"C": 10, "H": 8}}
LHV_MJKG = {"CO": 10.10, "H2": 119.96, "METHANE": 50.0, "C6H6": 40.1, "C7H8": 40.5, "C10H8": 38.9, "C": 32.8}
NONCONV = {"WET-BIO", "DRY-BIO"}


def results(stem="aspen_10tpd"):
    f = DATA / f"{stem}_stream_results.csv"
    return pd.read_csv(f, dtype={"stream": str}).fillna({"from_block": "", "to_block": ""}) if f.exists() else None


def _elements(row):
    out = dict.fromkeys(MW, 0.0)
    for sp, form in FORMULA.items():
        m = row.get(f"kgh_{sp}", 0) or 0
        if not m:
            continue
        mw = sum(MW[e] * n for e, n in form.items())
        for e, n in form.items():
            out[e] += m * MW[e] * n / mw
    return out


def block_closure(R):
    blocks = sorted((set(R.from_block) | set(R.to_block)) - {""})
    rows = []
    for b in blocks:
        i, o = R[R.to_block == b], R[R.from_block == b]
        nc = any((x[[f"kgh_{n}" for n in NONCONV if f"kgh_{n}" in R]].fillna(0).values.sum() > 0) for x in (i, o))
        row = dict(block=b, streams_in=", ".join(i.stream), streams_out=", ".join(o.stream), mass_in_kgh=round(i.mass_kgh.sum(), 3),
                   mass_out_kgh=round(o.mass_kgh.sum(), 3), mass_error_pct=round(100 * (o.mass_kgh.sum() - i.mass_kgh.sum()) / max(i.mass_kgh.sum(), 1e-9), 4) if len(i) else None,
                   enthalpy_in_kW=round(i.enthalpy_Gcal_h.sum() * KW_PER_GCAL_H, 1), enthalpy_out_kW=round(o.enthalpy_Gcal_h.sum() * KW_PER_GCAL_H, 1))
        row["duty_kW (out - in)"] = round(row["enthalpy_out_kW"] - row["enthalpy_in_kW"], 1)
        if not nc and len(i) and len(o):
            ei = pd.DataFrame([_elements(r) for r in i.to_dict("records")]).sum()
            eo = pd.DataFrame([_elements(r) for r in o.to_dict("records")]).sum()
            for e in ("C", "H", "O", "N"):
                row[f"{e}_error_pct"] = round(100 * (eo[e] - ei[e]) / ei[e], 3) if ei[e] > 1e-6 else None
        else:
            row["note"] = "biomass is a non-conventional solid: element balance needs the ultimate analysis" if nc else "boundary block"
        rows.append(row)
    return pd.DataFrame(rows)


IDEALISATIONS = [
    ("OXID / RED (gasifier)", "RGibbs equilibrium at fixed temperatures (1100 C POX, 800 C reduction).",
     "Real char beds do not reach equilibrium: methane and tar stay higher, char conversion is 90-98 %, CO2 is higher.",
     "Model the char bed as RPlug / RCSTR with Boudouard (C + CO2 -> 2 CO) and water-gas (C + H2O -> CO + H2) kinetics, or "
     "RGibbs with temperature approach (restricted equilibrium) for CH4 and the shift reaction; fit to measured gas analysis."),
    ("OXID (tar cracking)", "Benzene 3.8 kg/h and naphthalene 8.8 kg/h from the pyrolyser are fully destroyed.",
     "Two-stage downdraft gasifiers still leave about 10-100 mg/Nm3 tar after the char bed (more at low bed temperature).",
     "Keep C6H6, C7H8, C10H8 as products with a specified conversion (RStoic) or kinetic tar cracking; report tar in SYNGAS."),
    ("Reactors (heat loss)", "Adiabatic: the energy balance closes with heat loss of about -1.6 kW (zero).",
     "Refractory-lined small gasifiers lose about 3-8 % of fuel energy through the walls; this lowers cold gas efficiency.",
     "Set a heat duty (negative Q) on OXID / RED and the pyrolyser equal to the estimated wall loss, and let temperature or oxidant float."),
    ("Char and ash", "All carbon converts; ash is inert ASH only; cyclone B1 separates 100 % of ash.",
     "Unconverted char leaves with the ash (2-10 % of char carbon); fine dust passes the cyclone (cut size ~5-10 um).",
     "Add a char conversion spec, ash composition (K, Na, Ca, Si for slagging), particle size distribution and cyclone efficiency (Cyclone model)."),
    ("Trace species", "Sulfur as elemental S; nitrogen only as N2; no chlorine species in the products.",
     "Engines and SS316L need H2S, COS, NH3, HCN and HCl levels (corrosion, emissions, catalyst / oil life).",
     "Add H2S, COS, NH3, HCN, HCl components; feed the biomass ultimate analysis (N, S, Cl) and let RGibbs / RStoic distribute them."),
    ("Pressures", "All streams at 1.0 bar; no pressure drop.",
     "Cyclone, char bed, coolers, knock-out and flame arrestor each take 5-50 mbar; oxidant supply is at higher pressure.",
     "Add pressure drops per block (or sized equipment) so blower / suction duty and PSV set points can be checked."),
    ("Engine", "Compressor B6 + turbine B7 (Brayton cycle); 386 kW net shaft, lambda about 2.8 (air 3992 kg/h for 632 kg/h syngas).",
     "A gas engine genset runs at lambda 1.3-1.8 with 25-35 % shaft efficiency; exhaust flow and O2 are lower than modelled.",
     "Model the engine as RGibbs combustion at specified lambda + heat-to-shaft efficiency (or vendor data); set AIR by a design spec on exhaust O2."),
    ("Oxidant", "AIRBLOWN 45:55 O2:CO2 is an external feed at 30 C, 1 bar.",
     "If the CO2 comes from recycled exhaust it carries N2, H2O and O2; inerts accumulate without a purge.",
     "Add the recycle loop with a purge split and a design spec on gasifier temperature; include the CO2 source conditioning."),
    ("Property method / solids", "Check property method and non-conventional enthalpy models for the biomass (HCOALGEN / DCOALIGT).",
     "The biomass LHV used for efficiency (13.85 MJ/kg as received) must match the lab value.",
     "Confirm proximate / ultimate analysis and heat of combustion inputs; report them with the stream results."),
]

DATA_REQUESTS = [
    "Biomass ultimate analysis (C, H, O, N, S, Cl), proximate analysis, moisture range, ash composition and fusion temperatures",
    "Measured or vendor gas analysis after the char bed (CO, H2, CH4, CO2, tar g/Nm3) to calibrate kinetics or temperature approach",
    "Gasifier wall heat loss estimate (refractory thickness, shell temperature) or measured value",
    "Pressure profile through the gas train (blower or ejector duty), oxidant supply pressure",
    "Engine make / model, rated power on syngas, lambda, efficiency curve, exhaust temperature and flow at design and part load",
    "Source of CO2 for the oxidant (bottled, captured, recycled exhaust) and its composition",
    "Cooling water supply / return temperatures and available pressure",
]


def corrected_case(R, energy=None, char_conversion=0.95, heat_loss_pct=4.0, tar_mg_nm3=50.0, engine_lambda=1.6, shaft_eff=0.30, gen_eff=0.95,
                   parasitic_kW=45.0):
    """Engineering estimate of the real plant from the Aspen results. Returns a DataFrame item / Aspen / corrected / unit / basis."""
    S = R.set_index("stream")
    syn = S.loc["SYNGAS"]
    bio = S.loc["1"]
    char_c = float(S.loc["14"].get("kgh_C", 0) or 0)
    e = {} if energy is None else dict(zip(energy["item"], energy["value"]))
    lhv_bio = next((v for k, v in e.items() if str(k).startswith("Biomass energy in (LHV, from")), None) or 1603.9
    lhv_syn_aspen = sum((syn.get(f"kgh_{k}", 0) or 0) * v for k, v in LHV_MJKG.items() if k != "C") / 3.6
    # 1 unconverted char: the carbon would have made CO by Boudouard (C + CO2 -> 2 CO)
    dC = (1 - char_conversion) * char_c
    dCO = dC * 2 * 28.01 / 12.011
    dCO2 = dC * 44.01 / 12.011
    # 2 wall heat loss: made up by burning syngas (more oxidant at the same bed temperature)
    q_loss = heat_loss_pct / 100 * lhv_bio
    # 3 tar slip
    nm3 = sum((syn.get(f"kgh_{k}", 0) or 0) / mw * 22.414 for k, mw in [("CO", 28.01), ("CO2", 44.01), ("H2", 2.016), ("METHANE", 16.04), ("N2", 28.01), ("O2", 32.0)])
    tar_kgh = tar_mg_nm3 * nm3 / 1e6
    lhv_syn = lhv_syn_aspen - dCO * LHV_MJKG["CO"] / 3.6 - q_loss
    o2_extra = q_loss * 3.6 / 14.0          # ~14 MJ released per kg O2 burning CO / H2
    co = (syn.get("kgh_CO", 0) or 0) - dCO - (q_loss * 3.6 * 0.75) / LHV_MJKG["CO"]
    h2 = (syn.get("kgh_H2", 0) or 0) - (q_loss * 3.6 * 0.25) / LHV_MJKG["H2"]
    ch4 = syn.get("kgh_METHANE", 0) or 0
    o2_stoich = co * 16 / 28.01 + h2 * 8 + ch4 * 64 / 16.04
    air_stoich = o2_stoich / 0.232
    air = engine_lambda * air_stoich
    syngas_mass = syn.mass_kgh - dCO + dCO2 + o2_extra
    exhaust = syngas_mass + air
    shaft = shaft_eff * lhv_syn
    elec = shaft * gen_eff
    air_aspen = S.loc["AIR"].mass_kgh
    lam_aspen = air_aspen / (((syn.get("kgh_CO", 0) or 0) * 16 / 28.01 + (syn.get("kgh_H2", 0) or 0) * 8 + ch4 * 64 / 16.04) / 0.232)
    rows = [
        ("Biomass feed (as received)", bio.mass_kgh, bio.mass_kgh, "kg/h", "10 t/day design"),
        ("Biomass energy in (LHV)", lhv_bio, lhv_bio, "kW", "Aspen"),
        ("Char carbon to gasifier", char_c, char_c, "kg/h", "Aspen stream 14"),
        ("Char conversion", 100.0, char_conversion * 100, "%", "typical 90-98 % for downdraft char beds"),
        ("Unconverted char to ash bin", 0.0, dC, "kg/h", "leaves with the ash; ash bin sizing"),
        ("Ash + char to ash bin", S.loc["ASH"].mass_kgh, S.loc["ASH"].mass_kgh + dC, "kg/h", ""),
        ("Wall heat loss (gasifier + pyrolyser)", 0.0, q_loss, "kW", f"{heat_loss_pct:g} % of biomass LHV (typical 3-8 %)"),
        ("Extra oxygen to hold bed temperature", 0.0, o2_extra, "kg/h", "covers the wall loss by burning syngas"),
        ("Tar in syngas after char bed", 0.0, tar_kgh, "kg/h", f"{tar_mg_nm3:g} mg/Nm3 (two-stage downdraft 10-100)"),
        ("Dry syngas flow", syn.mass_kgh, syngas_mass, "kg/h", ""),
        ("Syngas flow (normal)", nm3, nm3, "Nm3/h", "dry, from Aspen composition"),
        ("Syngas chemical energy (LHV)", lhv_syn_aspen, lhv_syn, "kW", "less unconverted char and heat loss"),
        ("Cold gas efficiency", 100 * lhv_syn_aspen / lhv_bio, 100 * lhv_syn / lhv_bio, "%", "downdraft gasifiers typically 65-80 %"),
        ("Engine lambda", lam_aspen, engine_lambda, "-", "gas engines 1.3-1.8"),
        ("Engine combustion air", air_aspen, air, "kg/h", "lambda x stoichiometric air"),
        ("Engine exhaust", S.loc["S8"].mass_kgh, exhaust, "kg/h", ""),
        ("Shaft power", 386.0, shaft, "kW", f"{shaft_eff * 100:g} % of syngas LHV"),
        ("Electrical output (gross)", 386.0 * gen_eff, elec, "kWe", f"generator {gen_eff * 100:g} %"),
        ("Parasitic load (blowers, pumps, screws, heaters)", 0.0, parasitic_kW, "kWe", "estimate, replace with load list"),
        ("Net electrical output", 386.0 * gen_eff, elec - parasitic_kW, "kWe", ""),
        ("Net electrical efficiency", 100 * 386.0 * gen_eff / lhv_bio, 100 * (elec - parasitic_kW) / lhv_bio, "%", "small biomass gensets typically 18-25 %"),
    ]
    df = pd.DataFrame(rows, columns=["item", "aspen", "corrected", "unit", "basis"])
    df[["aspen", "corrected"]] = df[["aspen", "corrected"]].astype(float).round(2)
    return df


def mass_balance_table(R, corrected=None):
    """Overall plant mass balance from Aspen (and the corrected case)."""
    S = R.set_index("stream")
    ins = [("Biomass", "1"), ("Oxidant O2 + CO2", "AIRBLOWN"), ("Engine air", "AIR")]
    outs = [("Ash", "ASH"), ("Condensate", "WAT"), ("Exhaust to stack", "S15")]
    rows = [dict(side="in", item=n, stream=s, kgh=round(S.loc[s].mass_kgh, 2)) for n, s in ins] + \
           [dict(side="out", item=n, stream=s, kgh=round(S.loc[s].mass_kgh, 2)) for n, s in outs]
    df = pd.DataFrame(rows)
    tin, tout = df[df.side == "in"].kgh.sum(), df[df.side == "out"].kgh.sum()
    df.loc[len(df)] = dict(side="check", item="in - out", stream="", kgh=round(tin - tout, 3))
    return df


def feedback_workbook(stem, path, **assumptions):
    R = results(stem)
    en = DATA / f"{stem}_energy.csv"
    energy = pd.read_csv(en) if en.exists() else None
    with pd.ExcelWriter(path) as xw:
        pd.DataFrame({"Read me": [
            "Feedback on the Aspen Plus model of the 10 t/day two-stage downdraft gasifier + engine, from the P&ID work.",
            "Sheet 'Block closure': mass, enthalpy and element closure of every Aspen block (the model itself closes).",
            "Sheet 'Idealisations': what the model assumes that the real plant will not do, and the change recommended in Aspen.",
            "Sheet 'Corrected case': engineering estimate of the real plant with the assumptions listed; please replace it by re-running Aspen.",
            "Sheet 'Data requests': information needed to make the model match the plant.",
            "Sheet 'Plant mass balance': overall in / out from the Aspen streams."]}).to_excel(xw, sheet_name="Read me", index=False)
        block_closure(R).to_excel(xw, sheet_name="Block closure", index=False)
        pd.DataFrame(IDEALISATIONS, columns=["area", "Aspen model now", "real two-stage downdraft plant", "recommended correction in Aspen"]).to_excel(
            xw, sheet_name="Idealisations", index=False)
        corrected_case(R, energy, **assumptions).to_excel(xw, sheet_name="Corrected case", index=False)
        pd.DataFrame({"request": DATA_REQUESTS}).to_excel(xw, sheet_name="Data requests", index=False)
        mass_balance_table(R).to_excel(xw, sheet_name="Plant mass balance", index=False)
    return path
