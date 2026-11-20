"""
dexpi_bridge.py - optional link to pyDEXPI (the open-source Python implementation of the DEXPI P&ID standard).

What this does: takes a DEXPI XML P&ID (exported from a P&ID tool that supports DEXPI) and uses the documented
pyDEXPI command-line tool to turn it into a graph, which the dashboard and the assistant can then read.

What this does NOT do yet: write our generated P&ID out as a DEXPI file. That needs the pyDEXPI class API,
which changed between versions; do it against the version you install, following https://pypi.org/project/pyDEXPI/.

Install:  pip install "pydexpi[cli]"
NOTE: written from the pyDEXPI documentation; not run by the author of this file. Test before relying on it.
"""
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import networkx as nx
import pandas as pd

TAG_ATTRS = ("TagNameAssignmentClass", "TagName", "ProcessInstrumentationFunctionNumberAssignmentClass", "LineNumberAssignmentClass",
             "PipingNetworkSystemLineNumberAssignmentClass", "ItemTagAssignmentClass")


def _local(tag):
    return tag.split("}")[-1]


def _tag_of(el):
    if el.get("TagName"):
        return el.get("TagName")
    for ga in el.iter():
        if _local(ga.tag) == "GenericAttribute" and ga.get("Name") in TAG_ATTRS and ga.get("Value"):
            return ga.get("Value")
    return None


def parse_proteus(xml_bytes):
    """Read a DEXPI (Proteus schema) XML P&ID directly - no pyDEXPI needed. Returns (nodes DataFrame, triples DataFrame).
    Equipment, nozzles, piping network systems / segments, piping components (valves, fittings) and instrumentation
    functions are recognised; connections between nozzles and components become FEEDS / INSTALLED_ON relations.
    Works on the DEXPI 1.x Proteus files that P&ID tools export and on this project's own export; files using
    unusual vendor extensions may lose some links (check the counts)."""
    root = ET.fromstring(xml_bytes)
    owner, nodes, rel = {}, {}, set()
    kind_of = {"Equipment": "equipment", "PipingComponent": "valve", "ProcessInstrumentationFunction": "instrument",
               "ProcessSignalGeneratingFunction": "instrument", "InstrumentComponent": "instrument", "PipingNetworkSystem": "line"}

    def walk(el, eq_tag=None, line_tag=None):
        name = _local(el.tag)
        cur_eq, cur_line = eq_tag, line_tag
        if name in kind_of:
            tag = _tag_of(el) or el.get("ID")
            nodes[tag] = dict(id=tag, kind=kind_of[name], dexpi_class=el.get("ComponentClass", name), xml_id=el.get("ID"))
            if el.get("ID"):
                owner[el.get("ID")] = tag
            if name == "Equipment":
                cur_eq = tag
            if name == "PipingNetworkSystem":
                cur_line = tag
            if name == "PipingComponent" and line_tag:
                rel.add((tag, "INSTALLED_ON", line_tag))
        elif name == "Nozzle" and el.get("ID") and eq_tag:
            owner[el.get("ID")] = eq_tag
        for ch in el:
            walk(ch, cur_eq, cur_line)
    walk(root)
    links = []                                   # (from xml id, to xml id, line)

    def walk2(el, line_tag=None):
        name = _local(el.tag)
        if name == "PipingNetworkSystem":
            line_tag = _tag_of(el) or el.get("ID")
        if name == "Connection" and (el.get("FromID") or el.get("ToID")):
            links.append((el.get("FromID"), el.get("ToID"), line_tag))
        if name in ("Association",) and el.get("ItemID") and el.get("Type") in ("is located in", "is logical start of", "is logical end of", "refers to"):
            pass
        for ch in el:
            walk2(ch, line_tag)
    walk2(root)
    for a, b, line in links:
        fa, fb = owner.get(a), owner.get(b)
        if line and line in nodes:
            if fa and nodes.get(fa, {}).get("kind") == "equipment":
                rel.add((fa, "FEEDS", line))
            if fb and nodes.get(fb, {}).get("kind") == "equipment":
                rel.add((line, "FEEDS", fb))
        elif fa and fb:
            rel.add((fa, "FEEDS", fb))
    for el in root.iter():                       # instrument -> measured item (DEXPI associations / information flows)
        if _local(el.tag) in ("ProcessInstrumentationFunction", "ProcessSignalGeneratingFunction"):
            me = _tag_of(el) or el.get("ID")
            for a in el.iter():
                if _local(a.tag) == "Association" and a.get("ItemID") in owner and owner[a.get("ItemID")] != me:
                    rel.add((me, "MEASURES", owner[a.get("ItemID")]))
    N = pd.DataFrame(nodes.values())
    T = pd.DataFrame(sorted(rel), columns=["subject", "relation", "object"])
    return N, T


def available():
    return shutil.which("pydexpi") is not None


def dexpi_xml_to_graph(xml_path, level="process"):
    """Run `pydexpi export-graph <xml> -o <graphml> --level process` and load the result with NetworkX."""
    xml_path = Path(xml_path)
    graphml = xml_path.with_suffix(".graphml")
    subprocess.run(["pydexpi", "export-graph", str(xml_path), "-o", str(graphml), "--level", level], check=True)
    return nx.read_graphml(graphml)


def graph_to_triples(G):
    """Flatten any graph into subject, relation, object rows (same shape as knowledge_graph_triples.csv)."""
    rows = []
    for a, b, d in G.edges(data=True):
        rows.append((str(a), str(d.get("label") or d.get("type") or d.get("kind") or "CONNECTED_TO"), str(b)))
    return rows


def dexpi_xml_to_svg(xml_path):
    """Run `pydexpi svg <xml> -o <svg>` and return the SVG path."""
    xml_path = Path(xml_path)
    svg = xml_path.with_suffix(".svg")
    subprocess.run(["pydexpi", "svg", str(xml_path), "-o", str(svg)], check=True)
    return svg
