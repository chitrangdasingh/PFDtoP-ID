"""
fetch_pid2graph.py - download the public PID2Graph dataset and use its graphs to pre-train the P&ID model.

PID2Graph (Stürmer, Graumann, Koch: "From Engineering Diagrams to Graphs: Digitizing P&IDs with Transformers",
arXiv 2411.13929) is published on Zenodo, record 14803338. It holds P&ID images with graph annotations (symbols,
nodes, connections), synthetic and real-world. This script:
  1. lists the record's files through the Zenodo REST API and downloads them into dataset/pid2graph/
  2. unpacks zip archives
  3. collects every .graphml file and runs train_pid_model.py with them as stage-1 pre-training data

Run on a PC with internet (it does not work inside Streamlit Cloud or a network-restricted sandbox):
    python fetch_pid2graph.py              # download + train
    python fetch_pid2graph.py --no-train   # download only
Check the dataset licence on the Zenodo page before using it commercially.
Only the graph structure (symbol classes and connections) is used; the images are not needed for this model.
"""
import argparse
import glob
import json
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
RECORD = "14803338"
OUT = HERE / "dataset" / "pid2graph"


def files():
    with urllib.request.urlopen(f"https://zenodo.org/api/records/{RECORD}") as r:
        rec = json.load(r)
    return [(f["key"], f["links"]["self"], f.get("size", 0)) for f in rec.get("files", [])]


def download():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, url, size in files():
        dest = OUT / name
        if dest.exists() and dest.stat().st_size == size:
            print("have", name)
            continue
        print(f"downloading {name} ({size / 1e6:.1f} MB)")
        urllib.request.urlretrieve(url, dest)
        if name.lower().endswith(".zip"):
            with zipfile.ZipFile(dest) as z:
                z.extractall(OUT / Path(name).stem)
    g = sorted(glob.glob(str(OUT / "**" / "*.graphml"), recursive=True))
    print(len(g), "GraphML files")
    return g


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-train", action="store_true")
    a = ap.parse_args()
    graphs = download()
    if graphs and not a.no_train:
        own = sorted(glob.glob(str(HERE / "data" / "synthetic_pid" / "*.graphml")))
        subprocess.run([sys.executable, str(HERE / "train_pid_model.py"), "--graphml", *own, *graphs], check=True)
