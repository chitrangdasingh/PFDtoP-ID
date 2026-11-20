"""
make_legend_sheet.py - P&ID legend sheet (drawing PID-000): every symbol in the library, the ISA-5.1 identification
letters, instrument bubble conventions, line types and tag conventions, on one sheet with a zone border and title block.

Symbols are drawn from general knowledge of ANSI/ISA-5.1 (instruments, valves, actuators, signal lines) and common
ISO 10628 / PIP PIC001 practice (equipment). Check against the company standard before issue.
Run:  python make_legend_sheet.py      -> symbols/legend_sheet.svg and symbols/legend_sheet.html (print to PDF)
"""
import datetime
import re
from pathlib import Path
from xml.sax.saxutils import escape

import pandas as pd

import make_pid_sheet as sheet
import make_symbols as ms

HERE = Path(__file__).parent
SW, SH = 1684, 1190          # A1-like proportions in points


def frame(title, drawing_no="", rev="A", company=""):
    M1, M2 = 10, 26
    z = [f'<rect x="{M1}" y="{M1}" width="{SW - 2 * M1}" height="{SH - 2 * M1}" style="stroke-width:1.2"/>',
         f'<rect x="{M2}" y="{M2}" width="{SW - 2 * M2}" height="{SH - 2 * M2}" style="stroke-width:2"/>']
    for i in range(12):
        xa = M2 + (SW - 2 * M2) * i / 12
        if i:
            z.append(f'<path d="M{xa:.1f} {M1}V{M2}M{xa:.1f} {SH - M2}V{SH - M1}"/>')
        z.append(f'<text class="z" x="{xa + (SW - 2 * M2) / 24:.1f}" y="{M1 + 11}">{12 - i}</text><text class="z" x="{xa + (SW - 2 * M2) / 24:.1f}" y="{SH - M1 - 5}">{12 - i}</text>')
    for j in range(8):
        ya = M2 + (SH - 2 * M2) * j / 8
        if j:
            z.append(f'<path d="M{M1} {ya:.1f}H{M2}M{SW - M2} {ya:.1f}H{SW - M1}"/>')
        L = "ABCDEFGH"[7 - j]
        z.append(f'<text class="z" x="{M1 + 8}" y="{ya + (SH - 2 * M2) / 16 + 3:.1f}">{L}</text><text class="z" x="{SW - M1 - 8}" y="{ya + (SH - 2 * M2) / 16 + 3:.1f}">{L}</text>')
    import make_pid_sheet as sheet
    drawing_no = drawing_no or (sheet.company_settings().get("drawing_no", "PID") + "-00")
    z.append(sheet.title_block(SW, SH, M2, "SYMBOLS AND LEGEND", rev, company=company, drawing_no=drawing_no, heading="P & I DIAGRAM"))
    tx, ty = SW - M2 - 514, SH - M2 - 150 - 69
    return "".join(z), (tx, ty)


def _wrap(text, width=19):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    lines.append(cur)
    return lines[:2]


def symbol_cell(name, tag, body, x, y, w=96, h=82):
    body = re.sub(r'stroke-width="([\d.]+)"', r'style="stroke-width:\1"', body)
    nm = _wrap(name.upper())
    txt = "".join(f'<text x="{x + w / 2}" y="{y + h - 21 + i * 7}" style="font-size:5.6px;font-weight:bold">{escape(t)}</text>' for i, t in enumerate(nm))
    return (f'<rect class="cell" x="{x}" y="{y}" width="{w}" height="{h}"/>'
            f'<svg class="sym" x="{x + 12}" y="{y + 3}" width="{w - 24}" height="{h - 30}" viewBox="0 0 100 84">{body}</svg>'
            f'{txt}<text x="{x + w / 2}" y="{y + h - 4}" style="font-size:5.4px">{escape(tag)}</text>')


def section(title, items, x, y, cols, w=96, h=82):
    out = [f'<text class="hd" x="{x}" y="{y - 6}">{title}</text>']
    for i, (name, tag, body) in enumerate(items):
        out.append(symbol_cell(name, tag, body, x + (i % cols) * w, y + (i // cols) * h, w, h))
    rows = (len(items) + cols - 1) // cols
    return "".join(out), y + rows * h


def build(company=""):
    S = ms.S
    by_cat = {}
    for cat, name, tag, std, body in S:
        by_cat.setdefault(cat, []).append((name, tag, body))
    parts = []
    x0, y = 46, 52
    s, y = section("EQUIPMENT (ISO 10628 / PIP PIC001 PRACTICE)", by_cat["Equipment"], x0, y, cols=14)
    parts.append(s)
    s, y2 = section("VALVES, ACTUATORS AND FITTINGS (ISA-5.1)", by_cat["Valves"], x0, y + 20, cols=11)
    parts.append(s)
    s, y2b = section("INSTRUMENTS (ISA-5.1)", by_cat["Instruments"], x0 + 11 * 96 + 24, y + 20, cols=5)
    parts.append(s)
    s, y4 = section("LINES AND SIGNALS (ISA-5.1)", by_cat["Lines"], x0, max(y2, y2b) + 20, cols=10, h=66)
    parts.append(s)

    # ISA-5.1 identification letters
    L = pd.read_csv(HERE / "data" / "glossary_isa_letters.csv").fillna("")
    first, succ = L[L.position == "First letter"], L[L.position == "Succeeding letter"]
    tx, ty, rh = x0, y4 + 26, 9.0
    t = [f'<text class="hd" x="{tx}" y="{ty - 6}">ISA-5.1 IDENTIFICATION LETTERS</text>',
         f'<rect x="{tx}" y="{ty}" width="560" height="{(max(len(first), len(succ)) + 1) * rh + 4:.0f}"/>',
         f'<path class="thin" d="M{tx + 280} {ty}v{(max(len(first), len(succ)) + 1) * rh + 4:.0f}"/>']
    for col, (df, head) in enumerate([(first, "FIRST LETTER: MEASURED / INITIATING VARIABLE (MODIFIER)"), (succ, "SUCCEEDING LETTERS: FUNCTION (MODIFIER)")]):
        cx = tx + col * 280
        t.append(f'<text class="hd" x="{cx + 4}" y="{ty + rh}" style="font-size:6.2px">{head}</text>')
        for i, r in enumerate(df.itertuples()):
            mod = f"  ({r._4})" if r._4 else ""
            t.append(f'<text class="l" x="{cx + 4}" y="{ty + rh * (i + 2):.1f}" style="font-size:6px"><tspan style="font-weight:bold">{r.letter}</tspan>  {escape((r.meaning + mod)[:58])}</text>')
    parts.append("".join(t))

    # tag conventions and bubble locations
    cv = pd.read_csv(HERE / "data" / "glossary_tag_conventions.csv").fillna("")
    cv = cv[~cv["item"].eq("Equipment prefix")]
    kx, ky = tx + 590, ty
    k = [f'<text class="hd" x="{kx}" y="{ky - 6}">TAG AND CONTROL CONVENTIONS</text>']
    yy = ky + 8
    for r in cv.itertuples():
        k.append(f'<text class="l" x="{kx}" y="{yy}" style="font-size:6.2px;font-weight:bold">{escape(r.item.upper())}: {escape(r.example)}</text>')
        k.append(f'<text class="l" x="{kx}" y="{yy + 8}" style="font-size:5.6px">{escape(r.rule[:130])}</text>')
        yy += 20
    eqp = pd.read_csv(HERE / "data" / "glossary_tag_conventions.csv").fillna("")
    eqp = eqp[eqp["item"].eq("Equipment prefix")]
    k.append(f'<text class="l" x="{kx}" y="{yy + 4}" style="font-size:6.2px;font-weight:bold">EQUIPMENT PREFIXES</text>')
    pref = [f"{r.format} = {r.rule.split(' (')[0].split(',')[0]}" for r in eqp.itertuples()]
    for i in range(0, len(pref), 7):
        k.append(f'<text class="l" x="{kx}" y="{yy + 13 + (i // 7) * 8}" style="font-size:5.8px">{escape("   ".join(pref[i:i + 7]))}</text>')
    parts.append("".join(k))

    fr, (tbx, tby) = frame("P & I DIAGRAM LEGEND SHEET", company=company)
    notes = ["NOTES", "1. SYMBOLS DRAWN FROM GENERAL KNOWLEDGE OF ANSI/ISA-5.1 AND COMMON ISO 10628 / PIP PIC001 PRACTICE;",
             "    NOT COPIED FROM THE STANDARDS. CHECK AGAINST THE COMPANY LEGEND BEFORE ISSUE.",
             "2. ASME DOES NOT DEFINE P&ID SYMBOLS; ASME B31.3 / B16 / BPVC VIII GOVERN THE PIPING, VALVE AND VESSEL DESIGN BEHIND THEM.",
             "3. ON THE P&ID: FIELD INSTRUMENT = CIRCLE; DCS FUNCTION = CIRCLE IN SQUARE; SAFETY FUNCTION = DIAMOND IN SQUARE."]
    nt = "".join(f'<text class="{"hd" if i == 0 else "l"}" x="{x0}" y="{tby + 12 + i * 11}" style="font-size:{7.5 if i == 0 else 6}px">{escape(n) if i else n}</text>' for i, n in enumerate(notes))
    css = ('*{fill:none;stroke:#000;stroke-width:1;stroke-linejoin:round;stroke-linecap:round}'
           'text{stroke:none;fill:#000;font-family:Arial,Helvetica,sans-serif;text-anchor:middle;font-size:6px}tspan{stroke:none;fill:#000}'
           '.w{fill:#fff}.f{fill:#000}.logo .f{stroke-width:.2}.logo-l{stroke-width:.3}.cell{stroke:#999;stroke-width:.5}.thin{stroke-width:.6}'
           '.sym *{stroke-width:2}.sym .f{fill:#000}.sym .w{fill:#fff}.sym text{stroke:none;fill:#000}.sym .t{font-size:11px}.sym .s{font-size:10px}.sym .b{font-size:17px;font-weight:bold}'
           '.l{font-size:6.4px;text-anchor:start}.c{text-anchor:middle}.hd{font-size:7.5px;font-weight:bold;text-anchor:start}.tt{font-size:11px;font-weight:bold}.z{font-size:8px}')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SW} {SH}" width="{SW}" height="{SH}"><style>{css}</style>'
           f'<rect width="{SW}" height="{SH}" style="fill:#fff;stroke:none"/>{fr}{"".join(parts)}{nt}</svg>')
    return svg


def main():
    svg = build()
    out = HERE / "symbols"
    (out / "legend_sheet.svg").write_text(svg, encoding="utf-8")
    html = (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>P&amp;ID legend sheet</title>'
            f'<style>@page{{size:{SW + 2}pt {SH + 2}pt;margin:0}}html,body{{margin:0;background:#fff}}svg{{display:block;width:100%;height:auto}}</style></head><body>{svg}</body></html>')
    (out / "legend_sheet.html").write_text(html, encoding="utf-8")
    print("wrote symbols/legend_sheet.svg and symbols/legend_sheet.html")


if __name__ == "__main__":
    main()
