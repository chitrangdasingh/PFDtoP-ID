"""
make_symbols.py - P&ID symbol library.

Writes one SVG per symbol into symbols/, an index (symbols/index.csv) and a legend sheet (symbols/legend.html).
Outlines follow common practice (ISA-5.1 for instruments, valves, actuators and signal lines; PIP PIC001 /
ISO 10628-2 style for equipment). Check against your company standard before issuing as a project legend.
Run:  python make_symbols.py
"""
import csv
import re
from pathlib import Path

OUT = Path(__file__).parent / "symbols"
BT = '<path d="M30 28V56L50 42Z M70 28V56L50 42Z"/><path d="M8 42H30M70 42H92"/>'          # bow-tie valve with stubs
VES = '<path d="M35 22A15 8 0 0 1 65 22V62A15 8 0 0 1 35 62Z"/>'                           # vertical vessel
ZZ = lambda x0, x1, y, a=6, n=6: "M" + " L".join(f"{x0 + (x1 - x0) * i / n:.1f} {y + (0 if i in (0, n) else (a if i % 2 else -a))}" for i in range(n + 1))
BUB = '<circle cx="50" cy="42" r="18"/>'
TXT = lambda top, bot="101": f'<text x="50" y="39" class="t">{top}</text><text x="50" y="53" class="t">{bot}</text>'
SINE = lambda x: f'<path d="M{x} 42q4 -12 8 0t8 0"/>'

S = [  # (category, name, tag / note, standard, svg body)
 # ---------------- equipment
 ("Equipment", "Centrifugal pump", "P", "PIP PIC001 / ISO 10628-2", '<circle cx="45" cy="45" r="22"/><path d="M45 23H82V33H63.4M5 45H23M33 64L27 75H63L57 64"/>'),
 ("Equipment", "Positive displacement pump", "P", "PIP PIC001 / ISO 10628-2", '<circle cx="50" cy="42" r="22"/><circle cx="43" cy="42" r="7"/><circle cx="57" cy="42" r="7"/><path d="M6 42H28M72 42H94"/>'),
 ("Equipment", "Centrifugal compressor", "C / K", "PIP PIC001 / ISO 10628-2", '<path d="M22 18L78 32V52L22 66Z"/><path d="M6 42H22M78 42H94"/>'),
 ("Equipment", "Blower / fan", "B / K", "PIP PIC001 / ISO 10628-2", '<circle cx="45" cy="47" r="20"/><path d="M45 27H82V37H62.3M5 47H25"/><path d="M45 47L45 33M45 47L57 54M45 47L33 54"/>'),
 ("Equipment", "Roots blower", "B", "PIP PIC001 / ISO 10628-2", '<rect x="24" y="22" width="52" height="40" rx="16"/><ellipse cx="41" cy="42" rx="7" ry="14"/><ellipse cx="60" cy="42" rx="14" ry="7"/><path d="M6 42H24M76 42H94"/>'),
 ("Equipment", "Vertical vessel", "V / D", "PIP PIC001; ASME VIII design", VES),
 ("Equipment", "Horizontal vessel", "V / D", "PIP PIC001; ASME VIII design", '<path d="M22 28H78A8 14 0 0 1 78 56H22A8 14 0 0 1 22 28Z"/>'),
 ("Equipment", "Column with trays", "T / C", "PIP PIC001 / ISO 10628-2", '<path d="M38 14A12 6 0 0 1 62 14V68A12 6 0 0 1 38 68Z"/><path d="M38 26H62M38 36H62M38 46H62M38 56H62" stroke-dasharray="4 3"/>'),
 ("Equipment", "Reactor (packed bed)", "R", "PIP PIC001 / ISO 10628-2", VES + '<path d="M35 30H65M35 54H65M35 30L65 54M65 30L35 54"/>'),
 ("Equipment", "Agitated vessel", "R / V", "PIP PIC001 / ISO 10628-2", VES + '<circle cx="50" cy="8" r="5"/><path d="M50 13V52M40 52H60"/>'),
 ("Equipment", "Atmospheric tank", "TK", "PIP PIC001 / ISO 10628-2", '<path d="M25 30L50 18L75 30V68H25Z"/><path d="M25 30H75"/>'),
 ("Equipment", "Hopper / bin", "H / TK", "PIP PIC001 / ISO 10628-2", '<path d="M30 14H70V44L55 70H45L30 44Z"/>'),
 ("Equipment", "Shell and tube exchanger", "E", "PIP PIC001 / ISO 10628-2", '<circle cx="50" cy="42" r="24"/><path d="M10 42H34L42 30L58 54L66 42H90"/>'),
 ("Equipment", "Plate heat exchanger", "E", "PIP PIC001 / ISO 10628-2", '<rect x="30" y="20" width="40" height="44"/><path d="M38 20V64M46 20V64M54 20V64M62 20V64"/>'),
 ("Equipment", "Air cooler (fin-fan)", "E / AC", "PIP PIC001 / ISO 10628-2", '<rect x="18" y="20" width="64" height="16"/><path d="' + ZZ(18, 82, 28, 5, 8) + '"/><path d="M50 36V52"/><ellipse cx="39" cy="54" rx="11" ry="4"/><ellipse cx="61" cy="54" rx="11" ry="4"/>'),
 ("Equipment", "Fired heater", "H / F", "PIP PIC001 / ISO 10628-2", '<path d="M28 72V40L42 28V8H58V28L72 40V72Z"/><path d="M12 54H34L40 46L46 62L52 46L58 62L64 54H88"/>'),
 ("Equipment", "Electric heater", "E / H", "PIP PIC001 / ISO 10628-2", '<path d="M26 28H78A8 14 0 0 1 78 56H26Z"/><path d="' + ZZ(26, 74, 42, 7, 8) + '"/><path d="M14 36H26M14 48H26"/>'),
 ("Equipment", "Cyclone", "CY", "PIP PIC001 / ISO 10628-2", '<path d="M35 18H65V40L53 72H47L35 40Z"/><path d="M12 25H35M50 18V5"/>'),
 ("Equipment", "Bag house filter", "F", "PIP PIC001 / ISO 10628-2", '<path d="M28 12H72V46L54 72H46L28 46Z"/><path d="M28 46H72"/><path d="M36 18V40H42V18M47 18V40H53V18M58 18V40H64V18" stroke-dasharray="3 2"/>'),
 ("Equipment", "Filter / strainer", "F", "PIP PIC001 / ISO 10628-2", '<rect x="30" y="24" width="40" height="36"/><path d="M30 60L70 24" stroke-dasharray="4 3"/><path d="M8 42H30M70 42H92"/>'),
 ("Equipment", "Screw conveyor", "SC", "PIP PIC001 / ISO 10628-2", '<rect x="24" y="34" width="68" height="16"/><path d="' + ZZ(24, 92, 42, 8, 10) + '"/><circle cx="13" cy="42" r="7"/><path d="M20 42H24"/>'),
 ("Equipment", "Rotary airlock valve", "RV", "Common practice", '<circle cx="50" cy="42" r="16"/><path d="M34 42H66M42 28.1L58 55.9M58 28.1L42 55.9M50 8V26M50 58V76"/>'),
 ("Equipment", "Ejector", "J", "PIP PIC001 / ISO 10628-2", '<path d="M12 30H38L60 37H88V47H60L38 54H12Z"/><path d="M30 54V72"/>'),
 ("Equipment", "Flare", "FL", "PIP PIC001 / ISO 10628-2", '<path d="M46 76V28H54V76M34 76H66"/><path d="M50 26C40 18 48 12 50 3C52 12 60 18 50 26Z"/>'),
 ("Equipment", "Stack", "ST", "PIP PIC001 / ISO 10628-2", '<path d="M42 76L46 10H54L58 76Z"/><path d="M32 76H68"/>'),
 ("Equipment", "Cooling tower", "CT", "PIP PIC001 / ISO 10628-2", '<path d="M25 68L33 28H67L75 68M18 68H82M50 28V18"/><ellipse cx="41" cy="16" rx="9" ry="3"/><ellipse cx="59" cy="16" rx="9" ry="3"/>'),
 ("Equipment", "Electric motor", "M", "PIP PIC001 / ISO 10628-2", '<circle cx="50" cy="42" r="17"/><text x="50" y="48" class="b">M</text>'),
 ("Equipment", "Vendor package", "PK", "PIP PIC001", '<rect x="16" y="18" width="68" height="48" stroke-dasharray="10 3 2 3"/><text x="50" y="46" class="t">PK</text>'),
 # ---------------- valves and fittings
 ("Valves", "Gate valve", "HV", "ISA-5.1 / ISO 10628-2", BT),
 ("Valves", "Globe valve", "HV", "ISA-5.1 / ISO 10628-2", BT + '<circle cx="50" cy="42" r="5" class="f"/>'),
 ("Valves", "Ball valve", "HV", "ISA-5.1 / ISO 10628-2", BT + '<circle cx="50" cy="42" r="6" class="w"/>'),
 ("Valves", "Butterfly valve", "HV", "ISA-5.1 / ISO 10628-2", '<path d="M36 28V56M64 28V56M36 54L64 30M8 42H36M64 42H92"/><circle cx="50" cy="42" r="3" class="f"/>'),
 ("Valves", "Plug valve", "HV", "ISA-5.1 / ISO 10628-2", BT + '<path d="M50 34L56 42L50 50L44 42Z" class="f"/>'),
 ("Valves", "Diaphragm valve", "HV", "ISA-5.1 / ISO 10628-2", BT + '<path d="M36 30Q50 14 64 30"/>'),
 ("Valves", "Check valve", "CK", "ISA-5.1 / ISO 10628-2", '<path d="M30 28V56L50 42Z" class="f"/><path d="M70 28V56L50 42Z"/><path d="M8 42H30M70 42H92"/>'),
 ("Valves", "Three-way valve", "HV", "ISA-5.1", BT + '<path d="M36 62H64L50 42Z"/><path d="M50 62V78"/>'),
 ("Valves", "Angle valve", "HV", "ISA-5.1", '<path d="M30 28V56L50 42Z M36 62H64L50 42Z"/><path d="M8 42H30M50 62V78"/>'),
 ("Valves", "Control valve, diaphragm actuator", "FV / TV / PV / LV", "ISA-5.1", BT + '<path d="M50 42V24M34 24H66A16 13 0 0 0 34 24Z"/>'),
 ("Valves", "On/off valve, piston actuator", "XV", "ISA-5.1", BT + '<path d="M50 42V24"/><rect x="40" y="6" width="20" height="18"/><path d="M40 15H60"/>'),
 ("Valves", "Motor operated valve", "MOV", "ISA-5.1", BT + '<path d="M50 42V25"/><circle cx="50" cy="15" r="10"/><text x="50" y="19" class="s">M</text>'),
 ("Valves", "Solenoid valve", "SV", "ISA-5.1", BT + '<path d="M50 42V25"/><rect x="41" y="7" width="18" height="18"/><text x="50" y="20" class="s">S</text>'),
 ("Valves", "Hand operated (stem shown)", "HV", "ISA-5.1", BT + '<path d="M50 42V24M39 24H61"/>'),
 ("Valves", "Pressure safety valve", "PSV", "ISA-5.1; ASME BPVC VIII", '<path d="M30 30V54L50 42Z M38 62H62L50 42Z"/><path d="M8 42H30M50 62V78"/><path d="M50 42L50 36L43 33L57 29L43 25L57 21L43 17L50 14V8"/>'),
 ("Valves", "Pressure regulator, self-acting", "PCV", "ISA-5.1", BT + '<path d="M50 42V24M34 24H66A16 13 0 0 0 34 24Z"/><path d="M66 18H82V42" stroke-dasharray="4 3"/>'),
 ("Valves", "Rupture disc", "PSE", "ISA-5.1", '<path d="M8 42H45M55 42H92M45 28V56M55 28V56"/><path d="M55 30Q43 42 55 54"/>'),
 ("Valves", "Flame arrestor", "FA", "Common practice", '<rect x="34" y="28" width="32" height="28"/><text x="50" y="47" class="s">F</text><path d="M8 42H34M66 42H92"/>'),
 ("Valves", "Reducer", "", "ISO 10628-2 / PIP PIC001", '<path d="M38 28L62 36V48L38 56Z"/><path d="M8 42H38M62 42H92"/>'),
 ("Valves", "Spectacle blind", "", "ISO 10628-2 / PIP PIC001", '<path d="M8 42H43M57 42H92M50 33V35"/><circle cx="50" cy="42" r="7"/><circle cx="50" cy="26" r="7" class="f"/>'),
 ("Valves", "Y-strainer", "", "ISO 10628-2 / PIP PIC001", '<path d="M8 42H92M44 42L58 64L68 57L58 42"/><path d="M51 42L63 60" stroke-dasharray="3 2"/>'),
 # ---------------- instruments
 ("Instruments", "Field mounted instrument", "e.g. PI", "ISA-5.1", BUB + TXT("PI")),
 ("Instruments", "Control room / main panel", "e.g. TI", "ISA-5.1", BUB + '<path d="M32 42H68"/>' + TXT("TI")),
 ("Instruments", "Behind panel (not accessible)", "e.g. TY", "ISA-5.1", BUB + '<path d="M32 42H68" stroke-dasharray="4 3"/>' + TXT("TY")),
 ("Instruments", "Local (auxiliary) panel", "e.g. HS", "ISA-5.1", BUB + '<path d="M32.5 39H67.5M32.5 45H67.5"/>' + '<text x="50" y="36" class="t">HS</text><text x="50" y="56" class="t">101</text>'),
 ("Instruments", "Shared display / control (DCS)", "e.g. FIC", "ISA-5.1", '<rect x="32" y="24" width="36" height="36"/>' + BUB + '<path d="M32 42H68"/>' + TXT("FIC")),
 ("Instruments", "Computer function", "e.g. FY", "ISA-5.1", '<path d="M30 42L40 24H60L70 42L60 60H40Z"/><path d="M30 42H70"/>' + TXT("FY")),
 ("Instruments", "Programmable logic control (PLC)", "e.g. UC", "ISA-5.1", '<rect x="32" y="24" width="36" height="36"/><path d="M32 42L50 24L68 42L50 60Z"/>' + '<text x="50" y="46" class="s">UC</text>'),
 ("Instruments", "Interlock", "I", "ISA-5.1 / ISA-5.2", '<path d="M30 42L50 24L70 42L50 60Z"/><text x="50" y="47" class="b">I</text>'),
 ("Instruments", "Pressure indicating transmitter", "PIT", "ISA-5.1", BUB + TXT("PIT") + '<path d="M50 60V76"/>'),
 ("Instruments", "Flow element (orifice plate)", "FE", "ISA-5.1", '<path d="M8 42H92M46 26V58M54 26V58"/>'),
 ("Instruments", "Thermowell and element", "TW / TE", "ISA-5.1", '<path d="M8 60H92M50 60V34"/><circle cx="50" cy="26" r="8"/>'),
 # ---------------- lines
 ("Lines", "Major process line", "thick solid", "PIP PIC001 / ISO 10628-2", '<path d="M6 42H94" stroke-width="5"/>'),
 ("Lines", "Minor process / utility line", "thin solid", "PIP PIC001 / ISO 10628-2", '<path d="M6 42H94" stroke-width="2"/>'),
 ("Lines", "Flow direction", "arrow on line", "PIP PIC001", '<path d="M6 42H80" stroke-width="5"/><path d="M76 32L94 42L76 52Z" class="f"/>'),
 ("Lines", "Instrument supply / process connection", "thin solid", "ISA-5.1", '<path d="M6 42H94" stroke-width="1"/>'),
 ("Lines", "Undefined signal", "single slashes", "ISA-5.1", '<path d="M6 42H94M28 48L34 36M50 48L56 36M72 48L78 36" stroke-width="1.2"/>'),
 ("Lines", "Pneumatic signal", "double slashes", "ISA-5.1", '<path d="M6 42H94M24 48L30 36M29 48L35 36M47 48L53 36M52 48L58 36M70 48L76 36M75 48L81 36" stroke-width="1.2"/>'),
 ("Lines", "Electrical signal", "dashed", "ISA-5.1", '<path d="M6 42H94" stroke-width="1.5" stroke-dasharray="8 5"/>'),
 ("Lines", "Capillary tube", "crosses", "ISA-5.1", '<path d="M6 42H94M26 37L34 47M34 37L26 47M46 37L54 47M54 37L46 47M66 37L74 47M74 37L66 47" stroke-width="1.2"/>'),
 ("Lines", "Hydraulic signal", "L marks", "ISA-5.1", '<path d="M6 42H94M28 42V34H34M50 42V34H56M72 42V34H78" stroke-width="1.2"/>'),
 ("Lines", "Electromagnetic / sonic, guided", "sine on line", "ISA-5.1", '<g stroke-width="1.2"><path d="M6 42H94"/>' + SINE(18) + SINE(42) + SINE(66) + '</g>'),
 ("Lines", "Electromagnetic / sonic, unguided", "sine, no line", "ISA-5.1", '<g stroke-width="1.2">' + SINE(18) + SINE(42) + SINE(66) + '</g>'),
 ("Lines", "Software / data link", "open circles", "ISA-5.1", '<path d="M6 42H26M34 42H46M54 42H66M74 42H94" stroke-width="1.2"/><circle cx="30" cy="42" r="4" stroke-width="1.2"/><circle cx="50" cy="42" r="4" stroke-width="1.2"/><circle cx="70" cy="42" r="4" stroke-width="1.2"/>'),
 ("Lines", "Mechanical link", "solid circles", "ISA-5.1", '<path d="M6 42H94" stroke-width="1.2"/><circle cx="30" cy="42" r="4" class="f"/><circle cx="50" cy="42" r="4" class="f"/><circle cx="70" cy="42" r="4" class="f"/>'),
 ("Lines", "Heat-traced line", "parallel dashed", "PIP PIC001", '<path d="M6 40H94" stroke-width="4"/><path d="M6 50H94" stroke-width="1.2" stroke-dasharray="6 4"/>'),
 ("Lines", "Jacketed line", "double outline", "PIP PIC001", '<path d="M6 42H94" stroke-width="4"/><path d="M18 34H82V50H18Z" stroke-width="1.2"/>'),
 ("Lines", "Crossing, no connection", "vertical breaks", "PIP PIC001", '<path d="M6 42H94" stroke-width="3"/><path d="M50 10V35M50 49V74" stroke-width="3"/>'),
 ("Lines", "Junction", "dot", "PIP PIC001", '<path d="M6 42H94M50 42V74" stroke-width="3"/><circle cx="50" cy="42" r="4.5" class="f"/>'),
 ("Lines", "Off-page connector", "to / from drawing", "PIP PIC001", '<path d="M6 42H30" stroke-width="3"/><path d="M30 30H76L92 42L76 54H30Z"/><text x="56" y="46" class="s">DWG</text>'),
 ("Lines", "Specification break", "spec A | spec B", "PIP PIC001", '<path d="M6 42H94" stroke-width="3"/><path d="M50 28V56"/><text x="34" y="30" class="s">A1</text><text x="66" y="30" class="s">B2</text>'),
 ("Lines", "Battery limit", "dash-dot", "PIP PIC001", '<path d="M50 8V76" stroke-dasharray="12 3 2 3"/><path d="M6 42H94" stroke-width="3"/><text x="68" y="20" class="s">B.L.</text>'),
]

STYLE = ('<style>*{fill:none;stroke:#000;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}'
         'text{stroke:none;fill:#000;font-family:Arial,sans-serif;text-anchor:middle}.t{font-size:11px}.s{font-size:10px}.b{font-size:17px;font-weight:bold}'
         '.f{fill:#000}.w{fill:#fff}</style>')


def svg(body, size=""):
    body = re.sub(r'stroke-width="([\d.]+)"', r'style="stroke-width:\1"', body)   # CSS would override the attribute
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 84" {size}>{STYLE}{body}</svg>'


def main():
    OUT.mkdir(exist_ok=True)
    rows, cards, cat = [], [], None
    for c, name, tag, std, body in S:
        fn = f"{c.lower()}_{name.lower()}".replace(" / ", "_").replace(", ", "_").replace(" ", "_").replace("(", "").replace(")", "").replace("/", "_") + ".svg"
        (OUT / fn).write_text(svg(body, 'width="200" height="168"'), encoding="utf-8")
        rows.append((c, name, tag, std, fn))
        if c != cat:
            cards.append(f'</div><h2>{c}</h2><div class="grid">' if cat else f'<h2>{c}</h2><div class="grid">')
            cat = c
        cards.append(f'<div class="card">{svg(body)}<div class="n">{name}</div><div class="g">{tag}</div><div class="d">{std}</div></div>')
    with open(OUT / "index.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["category", "symbol", "tag_or_note", "standard_or_practice", "file"])
        w.writerows(rows)
    html = ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>P&amp;ID symbol legend</title><style>'
            '@page{size:A3 landscape;margin:10mm}body{font-family:Arial,sans-serif;margin:16px;color:#111;background:#fff}'
            'h1{font-size:20px;margin:0 0 2px}p{font-size:11px;color:#444;margin:0 0 8px}h2{font-size:14px;margin:14px 0 6px;border-bottom:2px solid #1f3864;color:#1f3864;break-after:avoid}'
            '.grid{display:grid;grid-template-columns:repeat(9,1fr);gap:6px}.card{border:1px solid #bbb;padding:4px 4px 6px;text-align:center;break-inside:avoid}'
            '.card svg{width:100%;height:auto;max-height:92px}.n{font-size:10.5px;font-weight:bold;line-height:1.2}.g{font-size:10px}.d{font-size:8.5px;color:#555}'
            '</style></head><body><h1>P&amp;ID symbol legend</h1>'
            '<p>Instruments, valves, actuators and signal lines follow ANSI/ISA-5.1 conventions. Equipment outlines follow common PIP PIC001 / ISO 10628-2 practice. '
            'Drawn from general knowledge of these standards; verify against your company standard before issue.</p>'
            + "".join(cards) + "</div></body></html>")
    (OUT / "legend.html").write_text(html, encoding="utf-8")
    print(len(S), "symbols ->", OUT)


if __name__ == "__main__":
    main()
